# -*- coding: utf-8 -*-
"""cancel_close：撤在途平仓挂单（券商侧撤单 + 回退 open + 订单流卖单 cancelled）。
仅对处于 closing 且有 close_order_id 的持仓可撤；否则 404。"""
from datetime import datetime, timezone
from decimal import Decimal as D
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import app.exec_close as order_exec
from app.order_state import JOURNAL, REGISTRY, ManagedPosition

_CODE = "QQQ260716C00500000"


def _src():
    return SimpleNamespace(name="watcher", alpaca=SimpleNamespace())


def _seed(status="closing", close_order_id="s1"):
    REGISTRY._pos.clear()
    JOURNAL.clear()
    p = ManagedPosition(option_code=_CODE, ticker="QQQ", direction="long",
                        qty=2, entry_mid=D("1.00"), entry_time=datetime.now(timezone.utc),
                        order_id="e1", status=status)
    if close_order_id:
        p.close_order_id = close_order_id
        p.close_qty = 2
        p.close_reason = "手动平仓"
        JOURNAL.submitted(close_order_id, "QQQ", _CODE, "sell", 2, reason="手动平仓")
    REGISTRY.record(p)
    return p


def test_cancel_close_reverts_open_and_cancels_broker(monkeypatch):
    p = _seed()
    cancelled = []
    state = {"status": "new"}
    cancel_intent_seen = {}

    def do_cancel(oid):
        cancel_intent_seen["cause"] = JOURNAL.get(oid).cancel_cause
        cancel_intent_seen["origin"] = JOURNAL.get(oid).cancel_origin
        cancelled.append(oid)
        state["status"] = "canceled"

    monkeypatch.setattr(order_exec, "AlpacaTrading",
                        lambda: SimpleNamespace(
                            cancel=do_cancel,
                            get_order=lambda oid: {
                                "status": state["status"], "filled_qty": "0",
                                "filled_avg_price": None,
                            }))
    res = order_exec.cancel_close(_src(), _CODE)
    assert res["cancelled"] is True and res["order_id"] == "s1"
    assert p.status == "open"
    assert p.close_order_id is None and p.close_qty == 0
    assert "s1" in cancelled
    assert cancel_intent_seen == {"cause": "manual", "origin": "user"}
    ev = JOURNAL._events.get("s1")
    assert ev is not None and ev.status == "cancelled"
    assert ev.cancel_cause == "manual"
    REGISTRY._pos.clear(); JOURNAL.clear()


def test_cancel_close_broker_error_keeps_order_pending(monkeypatch):
    """券商撤单失败时不得谎报成功或丢掉仍在券商排队的订单。"""
    p = _seed()

    def boom(oid):
        raise RuntimeError("券商挂了")

    monkeypatch.setattr(order_exec, "AlpacaTrading",
                        lambda: SimpleNamespace(
                            cancel=boom,
                            get_order=lambda oid: {"status": "new", "filled_qty": "0"}))
    with pytest.raises(HTTPException) as exc:
        order_exec.cancel_close(_src(), _CODE)
    assert exc.value.status_code == 502
    assert p.status == "closing" and p.close_order_id == "s1"
    assert p.cancel_requested is False
    assert JOURNAL.get("s1").status == "pending"
    assert JOURNAL.get("s1").cancel_cause is None
    assert JOURNAL.get("s1").cancel_origin is None
    REGISTRY._pos.clear(); JOURNAL.clear()


def test_cancel_close_partial_fill_deducts_sold(monkeypatch):
    """撤单时券商侧已部分成交：已卖张数必须扣减并落库，否则持仓高估、后续全量平仓超卖。"""
    p = _seed()
    monkeypatch.setattr(order_exec, "AlpacaTrading",
                        lambda: SimpleNamespace(
                            cancel=lambda oid: None,
                            get_order=lambda oid: {"status": "canceled", "filled_qty": "1",
                                                   "filled_avg_price": "1.05"}))
    rec = {}
    monkeypatch.setattr(order_exec.trade_recorder, "record_closed",
                        lambda oid, mid, q, r: rec.update(oid=oid, q=q))
    res = order_exec.cancel_close(_src(), _CODE)
    assert res["cancelled"] is True
    assert p.status == "open" and p.qty == 1
    assert rec == {"oid": "e1", "q": 1}
    REGISTRY._pos.clear(); JOURNAL.clear()


def test_cancel_close_race_fully_filled_settles_closed(monkeypatch):
    """撤单与成交竞态：撤到手时券商已全部成交 → 按成交结算置 closed，绝不回退 open。"""
    p = _seed()
    monkeypatch.setattr(order_exec, "AlpacaTrading",
                        lambda: SimpleNamespace(
                            cancel=lambda oid: None,
                            get_order=lambda oid: {"status": "filled", "filled_qty": "2",
                                                   "filled_avg_price": "1.05"}))
    monkeypatch.setattr(order_exec.trade_recorder, "record_closed", lambda *a, **k: None)
    res = order_exec.cancel_close(_src(), _CODE)
    assert res["cancelled"] is False and res["status"] == "filled"
    assert p.status == "closed" and p.qty == 0
    REGISTRY._pos.clear(); JOURNAL.clear()


def test_cancel_close_no_resting_raises_404(monkeypatch):
    _seed(status="open", close_order_id=None)
    with pytest.raises(HTTPException) as ei:
        order_exec.cancel_close(_src(), _CODE)
    assert ei.value.status_code == 404
    REGISTRY._pos.clear(); JOURNAL.clear()


def _broker_cancelled(monkeypatch):
    """券商侧已确认取消、未成交。"""
    monkeypatch.setattr(order_exec, "AlpacaTrading",
                        lambda: SimpleNamespace(
                            cancel=lambda oid: None,
                            get_order=lambda oid: {"status": "canceled", "filled_qty": "0",
                                                   "filled_avg_price": None}))


def test_cancel_close_duplicate_settle_still_repairs_status(monkeypatch):
    """别人先结算了同一张卖单时，状态位也必须修回来 —— 否则按钮永久灰死。

    现场（2026-09-03 NVDA C227.5，美东 09:49:57）：用户挂出目标限价卖单后手动撤单，
    Alpaca 已确认取消（订单流里 cancel_origin=user、broker_status=canceled 都写全了），
    但持仓 `status` 一直停在 `closing`、`close_order_id` 却已是 None。前端
    `app-sync.js` 的 `backendClosing = p.status === 'closing'` 于是恒真，
    `render.js` 把卖出 / 1/3 / 半仓 / 全平 一并置成 pointer-events:none —— 灰死，
    刷新也回不来（closing 是从后端状态算的），用户只能去 Alpaca App 手动市价清仓。

    根因：`_settle_cancelled_order` 的 duplicate 分支为了「不把已 closed 的持仓复活成
    open」，把整段账目连 `set_status` 一起跳过；而下面的 `_clear_close_tracking`
    是无条件执行的。于是留下 `status='closing'` + `close_order_id=None` 这个
    自相矛盾的死态：既没有在途单可撤，也永远回不到 open。
    """
    p = _seed()
    # 另一路（TradingStream 推送 / watchdog 巡检）先拿到终态结算令牌
    assert JOURNAL.mark_cancelled("s1", fail_reason="用户手动撤单",
                                  cancel_origin="user", cancel_cause="manual") is True
    _broker_cancelled(monkeypatch)

    res = order_exec.cancel_close(_src(), _CODE)

    assert res["cancelled"] is True
    assert res.get("already_settled") is True
    assert p.close_order_id is None and p.close_qty == 0
    # 跟踪位清了，状态位就必须跟着回来，不能留 closing 的死态
    assert p.status == "open"
    REGISTRY._pos.clear(); JOURNAL.clear()


def test_cancel_close_duplicate_settle_zero_qty_marks_closed(monkeypatch):
    """duplicate 且仓位已被别人减到 0：修成 closed，不是 open。"""
    p = _seed()
    assert JOURNAL.mark_cancelled("s1", fail_reason="用户手动撤单") is True
    p.qty = 0                      # 赢家已按成交减完仓，但状态位还没落
    _broker_cancelled(monkeypatch)

    order_exec.cancel_close(_src(), _CODE)

    assert p.status == "closed"
    REGISTRY._pos.clear(); JOURNAL.clear()


def test_cancel_close_duplicate_settle_never_revives_closed():
    """duplicate 且赢家已标 closed：绝不复活成 open（这正是当初跳过整段账目的初衷）。

    这个竞态进不了 `cancel_close`（它自己就要求 status=='closing'），只能发生在
    `cancel()` 顶部查完 is_pending、正在发券商请求的那几百毫秒里 —— 赢家在此期间
    把仓位标成了 closed。所以直接测结算函数。
    """
    p = _seed()
    assert JOURNAL.mark_cancelled("s1", fail_reason="用户手动撤单") is True
    p.qty = 0
    REGISTRY.set_status(_CODE, "closed")      # 赢家在券商请求往返期间已结清

    order_exec._settle_cancelled_order(
        "s1", {"status": "canceled", "filled_qty": "0", "filled_avg_price": None},
        None, p, reason="用户手动撤单", cancel_origin="user", cancel_cause="manual")

    assert p.status == "closed"
    REGISTRY._pos.clear(); JOURNAL.clear()
