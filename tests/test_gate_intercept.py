"""gate_order 拒单 → record_intercept 钩子 + evaluate_with_context 契约。"""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal as D

import pytest

from app.config import default_rule_config
from app.order_gate import MarketContext, evaluate, evaluate_with_context, gate_order
from app.validator import AccountState, Contract, TickerSignal

_WATCHER_CFG = replace(default_rule_config(), whitelist=("QQQ", "IWM", "SPY"))


def _ctx(**o):
    base = dict(
        signal=TickerSignal(price=D("100"),
                            vwap=D("99.9"), atr=D("1.0")),
        account=AccountState(daily_pnl=D("-10"), weekly_pnl=D("-20"), consecutive_losses=0,
                             cooldown_remaining_s=0, minutes_since_open=D("60"),
                             buying_power=D("1000")),
        contract=Contract(delta=D("0.45"), spread_pct=D("0.02"), mid=D("2.00")))
    base.update(o)
    return MarketContext(**base)


class FakeSource:
    def __init__(self, ctx=None, raises=False):
        self.name = "watcher"
        self._ctx = ctx
        self._raises = raises


@pytest.fixture(autouse=True)
def _stub_market_context(monkeypatch):
    def build(source, _ticker, _direction):
        if source._raises:
            raise RuntimeError("数据源不可用")
        return source._ctx

    monkeypatch.setattr("app.order_gate.build_market_context", build)


def test_evaluate_with_context_returns_result_and_ctx():
    ctx = _ctx()
    result, got = evaluate_with_context("QQQ", "long", provider=lambda t, d: ctx, config=_WATCHER_CFG)
    assert result.allowed is True
    assert got is ctx


def test_evaluate_still_returns_result_only():
    ctx = _ctx()
    r = evaluate("QQQ", "long", provider=lambda t, d: ctx, config=_WATCHER_CFG)
    assert r.allowed is True


def test_gate_order_reject_records_intercept(monkeypatch):
    calls = []
    monkeypatch.setattr("app.trade_recorder.record_intercept",
                        lambda **k: calls.append(k))
    # 白名单已动态化（user_watchlist 直读，TSLA 实际在库内，同 test_api.py 既有做法）→ 此处固定白名单测规则本身
    from app import order_gate
    monkeypatch.setattr(order_gate, "_get_dynamic_whitelist", lambda: ("QQQ", "IWM", "SPY"))
    src = FakeSource(ctx=_ctx())              # TSLA 不在白名单 → whitelist 拒
    out = gate_order(src, "TSLA", "long", 1)
    assert out["allowed"] is False
    assert out["rule"] == "whitelist"
    assert len(calls) == 1
    assert calls[0]["rule"] == "whitelist"
    assert calls[0]["market_snapshot"] is not None   # 有 ctx → 有快照


def test_gate_order_allow_records_nothing(monkeypatch):
    calls = []
    monkeypatch.setattr("app.trade_recorder.record_intercept",
                        lambda **k: calls.append(k))
    gate_order(FakeSource(ctx=_ctx()), "QQQ", "long", 1)
    assert calls == []                        # 放行不留痕


def test_gate_order_datasource_degrade_records_null_snapshot(monkeypatch):
    calls = []
    monkeypatch.setattr("app.trade_recorder.record_intercept",
                        lambda **k: calls.append(k))
    out = gate_order(FakeSource(raises=True), "QQQ", "long", 1)
    assert out["rule"] == "datasource"
    assert calls[0]["rule"] == "datasource"
    assert calls[0]["market_snapshot"] is None       # 降级无 ctx
