# -*- coding: utf-8 -*-
"""券商侧已无此仓时，锁存的风险退出必须收尾 —— 否则永远重试一个不存在的仓。

2026-08-14 实盘（`INTC260814P00104000`）：

  22:32:18  挂 a5abd120 qty=2 追价卖
  22:32:19  追价撤单重挂 → cancel_requested；**撤单落地前成交了 1 张**
  22:32:20  `atomic_close` 被 429 抛断（`{"message": "too many requests."}`），
            那 1 张成交永远没记账 → 本地 2、券商 1
  22:32:23  下一拍卖掉最后 1 张 → 券商 0、本地 1
  22:32:24  起每秒一条 `trade_watchdog_result`「Alpaca 当前无可平仓数量」，
            46 条直到重启

429 只是制造了不一致，**让它永久卡住的是另一处**：`watchdog.py` 的

    if pos.risk_exit_state is not None:
        execute_latched_risk_exit(...)
        continue          # ← 无条件

这条 `continue` 在「券商侧已无此仓」(`watchdog.py:590`) 和「张数以 Alpaca 为准」
(`watchdog.py:610`) 两段对账**之前**。一旦进入保护态，这个仓就再也不跟券商对账了。

修在 `execute_latched_risk_exit` 而不是放开那条 `continue`，因为：

  - 依据是执行器自己那次 **实时 REST 读数**（`order_pricing` 里的
    `ec.position_qty()`），比 `_watchdog_tick` 手上的持仓快照新；
  - 执行器刚刚**拒绝提交**卖单，此刻不可能有本程序的在途平仓单在跑，
    不会与 `close_run` 抢（`watchdog.py:594` 那条 `pos.status == "closing"`
    守卫正是为了避让它，放开 `continue` 就得再处理一遍这个竞态）；
  - `order_pricing` 已先用 `confirmed_position_qty` 兜过成交结算窗口
    （`POSITION_SETTLE_GRACE_S`），能拿到 `broker_position_qty == 0`
    就不是「刚成交、券商快照还没追上」。

**不伪造成交价**：那几张确实卖掉了，但价格已经丢了。落库 `exit_mid=None`，
让 PnL 缺口显式可见，绝不拿盘口 mid 猜进已实现盈亏。
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

import app.watchdog_exit as wx
from app.order_state import REGISTRY, ManagedPosition

_CODE = "INTC260814P00104000"


class _EC:
    """只需要 `_open_sell_conflicts` 那一个能力。"""

    def open_sell_orders_for(self, option_code):
        return []


@pytest.fixture()
def recorded(monkeypatch):
    calls = []
    monkeypatch.setattr(wx, "trade_recorder", SimpleNamespace(
        audit=lambda event, **kw: calls.append(("audit", event, kw)),
        record_closed=lambda *a, **kw: calls.append(("record_closed", a, kw)),
    ))
    return calls


@pytest.fixture()
def pos():
    p = ManagedPosition(
        option_code=_CODE,
        ticker="INTC",
        direction="short",
        qty=1,
        entry_mid=D("0.82"),
        entry_time=datetime.now(timezone.utc) - timedelta(seconds=900),
        order_id="entry-1",
        status="closing",
    )
    p.risk_exit_state = "closing_remaining"
    p.risk_exit_reason = "超时 15分"
    p.last_owned_buy_fill_at = datetime.now(timezone.utc) - timedelta(seconds=900)
    REGISTRY.record(p)
    yield p
    REGISTRY.drop(_CODE) if hasattr(REGISTRY, "drop") else None


def _run(pos, result, calls):
    """跑一拍锁存风险退出，返回执行器被调用的次数。"""
    calls.clear()
    hits = []

    def _executor(*_a, **_kw):
        hits.append(1)
        return dict(result)

    wx.execute_latched_risk_exit(
        pos, _EC(), None, source_name="watcher", close_executor=_executor,
    )
    return len(hits)


_NO_POSITION = {
    "closed": False,
    "reason": "超时 15分",
    "contract": _CODE,
    "error": "Alpaca 当前无可平仓数量，已禁止提交卖单并等待持仓对账",
    "broker_position_qty": 0,
    "reconcile_required": True,
}


def test_券商已无此仓时保护态被清掉(pos, recorded):
    _run(pos, _NO_POSITION, recorded)

    assert pos.risk_exit_state is None, "保护态没清 → 下一拍还会重试"
    assert pos.status not in ("open", "closing", "pending"), (
        f"status={pos.status} 仍在 REGISTRY.active()，看门狗每拍都会再捞到它"
    )


def test_清掉之后不再调用执行器(pos, recorded):
    """46 条「无可平仓数量」的直接成因就是这里每拍都在调。"""
    assert _run(pos, _NO_POSITION, recorded) == 1

    assert _run(pos, _NO_POSITION, recorded) == 0, "保护态已清，不该再提交卖单"


def test_落库不伪造成交价(pos, recorded):
    _run(pos, _NO_POSITION, recorded)

    closed = [c for c in recorded if c[0] == "record_closed"]
    assert len(closed) == 1, "券商侧已平的事实必须落库一次"
    assert closed[0][1][1] is None, "成交价已丢失，绝不能拿盘口 mid 猜进已实现 PnL"


def test_留下可排查的审计事件(pos, recorded):
    """静默自愈 = 下次同样的事故照样查不出来。"""
    _run(pos, _NO_POSITION, recorded)

    events = [c[1] for c in recorded if c[0] == "audit"]
    assert any("desync" in e or "phantom" in e for e in events), events


def test_券商仍有仓时保持保护态(pos, recorded):
    """只对「券商确认 0 张」自愈。别的 reconcile_required 一律继续重试。"""
    still_there = {**_NO_POSITION, "broker_position_qty": 2,
                   "error": "清理旧挂单期间券商持仓数量发生变化"}

    _run(pos, still_there, recorded)

    assert pos.risk_exit_state is not None, "券商还有仓就必须继续退出"


def test_没有broker_position_qty时不自愈(pos, recorded):
    """拿不到实时读数就不能断言仓位没了 —— 宁可多重试一拍。"""
    unknown = {"closed": False, "reconcile_required": True,
               "error": "止损结果缺少 Alpaca 成交数量，已停止本地结算并等待对账"}

    _run(pos, unknown, recorded)

    assert pos.risk_exit_state is not None


def test_没有reconcile_required时不自愈(pos, recorded):
    """`broker_position_qty: 0` 单独出现不足以自愈，必须是执行器明确拒单那条路。"""
    odd = {"closed": False, "broker_position_qty": 0, "filled_qty": 0,
           "error": "别的什么错误"}

    _run(pos, odd, recorded)

    assert pos.risk_exit_state is not None


def test_本拍有成交时走正常结算(pos, recorded):
    """有 filled_qty 就是正常成交路径，不能被自愈分支截胡。"""
    filled = {"closed": True, "filled_qty": 1, "fill": "0.71",
              "order": {"id": "sell-1"}}

    _run(pos, filled, recorded)

    assert pos.qty == 0
    assert pos.risk_exit_state is None


# ── ④ 审计去重：一个卡住的状态不该被打成每秒一条 ────────────────────────────
# `_audit_block_once`（watchdog_exit.py:320）早就有去重，`trade_watchdog_result`
# 这条没有 —— 08-14 那 46 条里，除去第 1 条全是同一份内容的复读。
_STUCK = {
    "closed": False,
    "reason": "超时 15分",
    "contract": _CODE,
    "error": "清理旧挂单期间券商持仓数量发生变化；已停止新平仓单，等待订单流对账后重试",
    "broker_position_qty": 2,
    "reconcile_required": True,
}


def _results(calls):
    return [kw for tag, ev, kw in calls if tag == "audit" and ev == "trade_watchdog_result"]


def test_同一结果连续复读只记一条(pos, recorded):
    all_calls = []
    for _ in range(4):
        _run(pos, _STUCK, recorded)
        all_calls += recorded

    assert len(_results(all_calls)) == 1, "卡住的状态被打成了每拍一条"


def test_结果变化时重新记录并带上被吞条数(pos, recorded):
    """静默去重会让人以为「只失败了一次」——吞掉多少必须说出来。"""
    all_calls = []
    for _ in range(3):
        _run(pos, _STUCK, recorded)
        all_calls += recorded
    _run(pos, {**_STUCK, "error": "换了个错误"}, recorded)
    all_calls += recorded

    rows = _results(all_calls)
    assert len(rows) == 2
    assert rows[0].get("suppressed") is None, "首条没有前序被吞，不该带这个字段"
    assert rows[1]["suppressed"] == 2


def test_成交进展必须每次都记(pos, recorded):
    """部分成交是真进展，绝不能被去重吞掉。"""
    pos.qty = 3
    all_calls = []
    for filled in (1, 1):
        _run(pos, {"closed": False, "filled_qty": filled, "fill": "0.70",
                   "resting": False, "order": {"id": "sell-x"}}, recorded)
        all_calls += recorded

    assert len(_results(all_calls)) == 2, "两次真实成交被合并成一条"
