# -*- coding: utf-8 -*-
"""加仓 `/api/add` 必须过闸门（审查 2.3 —— 目前系统最大的风控窟窿）。

`add_position` 原本只校验一件事：这个合约在 REGISTRY 里。然后直接 submit。
不查熔断、不查冷却、不查开盘/午后禁入、不查震荡/反向/追高/距墙、不查资金、
不查仓位上限，连「盯盘离线 → 下单硬锁」这条铁律对它都无效。
而它**会**写 JOURNAL buy → 计入今日交易次数，于是加仓「算次数、却不受次数上限约束」。

两条规则不适用于加仓，显式放行：
  - contract（③合约选择）：合约已经持有，闸门另选一张 ATM 来验没有意义
  - position/funds：闸门算的是**新选那张**的成本，不是我们真正要买的这张
    → 由本函数用持仓合约的真实报价重算，口径才对
"""
from datetime import datetime, timezone
from decimal import Decimal as D
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import app.exec_entry as order_exec
from app.order_gate import MarketContext
from app.order_state import JOURNAL, REGISTRY, ManagedPosition
from app.validator import AccountState, Contract, TickerSignal

_CODE = "QQQ260716C00500000"


def _ctx(daily_pnl=D("0"), buying_power=D("100000"), mins=D("60"), ticker="QQQ"):
    return MarketContext(
        signal=TickerSignal(price=D("500"),
                            vwap=D("500"), atr=D("1"), vwap_sigma=D("0")),
        account=AccountState(daily_pnl=daily_pnl, weekly_pnl=D("0"), consecutive_losses=0,
                             cooldown_remaining_s=0, minutes_since_open=mins,
                             buying_power=buying_power),
        contract=Contract(delta=D("0.45"), spread_pct=D("0.02"), mid=D("1.00")))


def _src(ctx_fn=None, quote=(D("0.98"), D("1.02"))):
    def market_context(ticker, direction):
        return (ctx_fn or _ctx)()
    return SimpleNamespace(
        name="watcher",
        alpaca=SimpleNamespace(option_quote=lambda code, fresh=False: quote),
        _test_market_context=market_context)


def _req(qty=1):
    return SimpleNamespace(option_code=_CODE, qty=qty, price_type=None, limit_price=None)


def _seed(qty=1, entry_mid="1.00"):
    REGISTRY._pos.clear(); JOURNAL.clear()
    p = ManagedPosition(option_code=_CODE, ticker="QQQ", direction="long", qty=qty,
                        entry_mid=D(entry_mid), entry_time=datetime.now(timezone.utc),
                        order_id="e1", status="open")
    REGISTRY.record(p)
    return p


class _SpyTrading:
    def __init__(self):
        self.submitted = []

    def submit(self, req):
        self.submitted.append(req)
        return {"id": "add1"}


@pytest.fixture
def spy(monkeypatch):
    t = _SpyTrading()
    monkeypatch.setattr(order_exec, "AlpacaTrading", lambda: t)
    monkeypatch.setattr(order_exec.trade_recorder, "record_add", lambda *a, **k: None)
    return t


@pytest.fixture(autouse=True)
def _stub_market_context(monkeypatch):
    monkeypatch.setattr(
        order_exec,
        "build_market_context",
        lambda source, ticker, direction: source._test_market_context(
            ticker,
            direction,
        ),
    )


# ── 闸门拦截 ──────────────────────────────────────────────────────────────────
def test_add_blocked_by_afternoon_cutoff(spy):
    """午后禁入不许开新敞口——加仓也是开敞口。

    2026-09-01 之前这条用的是震荡闸（效率比 0.05）。震荡闸删除后换成午后禁入 ——
    本用例要证的是「加仓走的是同一条闸门链」，用哪条规则触发不重要。
    """
    _seed(qty=1)
    with pytest.raises(HTTPException) as e:
        order_exec.add_position(_src(lambda: _ctx(mins=D("200"))), _req(qty=1))
    assert e.value.status_code == 400
    assert "午后禁入" in e.value.detail
    assert spy.submitted == []          # 绝不能已经发出去了


def test_add_blocked_by_daily_breaker(spy):
    """日亏熔断后一张都不许再加。"""
    _seed(qty=1)
    with pytest.raises(HTTPException) as e:
        order_exec.add_position(_src(lambda: _ctx(daily_pnl=D("-999"))), _req(qty=1))
    assert e.value.status_code == 400
    assert spy.submitted == []


def test_add_blocked_when_signal_offline(spy):
    """盯盘离线 → 下单硬锁，加仓同样适用（此前完全绕过这条铁律）。"""
    _seed(qty=1)

    def boom(ticker, direction):
        raise RuntimeError("盯盘引擎未启动 — 信号离线，下单已锁")

    src = _src()
    src._test_market_context = boom
    with pytest.raises(HTTPException) as e:
        order_exec.add_position(src, _req(qty=1))
    assert e.value.status_code == 400
    assert spy.submitted == []


def test_add_blocked_by_afternoon_cutoff(spy):
    """午后禁入线之后不许新开敞口。"""
    _seed(qty=1)
    with pytest.raises(HTTPException) as e:
        order_exec.add_position(_src(lambda: _ctx(mins=D("200"))), _req(qty=1))
    assert e.value.status_code == 400
    assert spy.submitted == []


# ── 仓位上限用**持仓合约**真实报价重算 ────────────────────────────────────────
def test_add_blocked_when_combined_exposure_exceeds_ceiling(spy):
    """已持 5 张 $500，再加 2 张（mid 1.00 → $200）= $700 > $600 上限 → 拦。"""
    _seed(qty=5, entry_mid="1.00")
    with pytest.raises(HTTPException) as e:
        order_exec.add_position(_src(), _req(qty=2))
    assert e.value.status_code == 400
    assert "700" in e.value.detail and "600" in e.value.detail
    assert spy.submitted == []


def test_add_allowed_within_ceiling(spy):
    """放行只代表已提交；未收到真实成交前不得乐观增加本地仓位。"""
    p = _seed(qty=3, entry_mid="1.00")
    res = order_exec.add_position(_src(), _req(qty=2))
    assert res["executed"] is True
    assert len(spy.submitted) == 1
    assert p.qty == 3
    assert JOURNAL.get("add1").status == "pending"


def test_add_immediate_fill_moves_order_out_of_pending(spy):
    """券商 submit 已返回 FILLED 时，加仓单不能永久挂在订单面板“待成交”。

    Alpaca SDK 的字符串形态是 ``OrderStatus.FILLED``，Mock 服务器会即时返回这一状态。
    """
    p = _seed(qty=1, entry_mid="1.00")
    spy.submit = lambda req: {
        "id": "add-filled",
        "status": "OrderStatus.FILLED",
        "filled_qty": "1",
        "filled_avg_price": "1.01",
    }

    order_exec.add_position(_src(), _req(qty=1))

    event = JOURNAL._events["add-filled"]
    assert event.status == "filled"
    assert event.price == D("1.01")
    assert event.applied_qty == 1
    assert p.qty == 2
    assert p.entry_mid == D("1.005")


def test_add_uses_held_contract_price_not_gate_selection(spy):
    """上限判定必须用**持仓合约**的真实 mid，而不是闸门另选那张 ATM 的 mid。

    持仓合约 mid=3.00（贵），闸门选中的 ATM mid=1.00（便宜）。加 1 张 = $300，
    已持 1 张 $100 → 合计 $400 ≤ $600 放行；但若误用闸门的 1.00 算，两种结果都放行，
    区分不出来 —— 故这里加 2 张：真实 $600+$100=$700 应拦，用闸门口径算只有 $300 会放行。
    """
    _seed(qty=1, entry_mid="1.00")
    src = _src(quote=(D("2.98"), D("3.02")))     # 持仓合约 mid = 3.00
    with pytest.raises(HTTPException) as e:
        order_exec.add_position(src, _req(qty=2))
    assert "700" in e.value.detail


# ── 不适用于加仓的规则要显式放行 ──────────────────────────────────────────────
def test_add_not_blocked_by_contract_rule(spy):
    """闸门选不出合格合约（rule=contract）与加仓无关——我们加的是已持有的那张。"""
    _seed(qty=1)

    def no_contract(ticker, direction):
        c = _ctx()
        return MarketContext(signal=c.signal, account=c.account, contract=None)

    src = _src()
    src._test_market_context = no_contract
    res = order_exec.add_position(src, _req(qty=1))
    assert res["executed"] is True
    assert len(spy.submitted) == 1


def test_add_still_rejects_unregistered_position(spy):
    """未登记的持仓仍然拒绝加仓（既有行为不变）。"""
    REGISTRY._pos.clear()
    with pytest.raises(HTTPException) as e:
        order_exec.add_position(_src(), _req(qty=1))
    assert e.value.status_code == 404
    assert spy.submitted == []
