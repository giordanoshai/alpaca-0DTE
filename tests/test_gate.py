"""下单闸门 gate.evaluate + T2 配置默认值 测试。"""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal as D

import pytest

from app.config import default_rule_config
from app.order_gate import MarketContext, evaluate, normalize_direction
from app.validator import AccountState, Contract, TickerSignal

_WATCHER_CFG = replace(default_rule_config(), whitelist=("QQQ", "IWM", "SPY"))


def ctx(**o):
    base = dict(
        signal=TickerSignal(price=D("100"), vwap=D("99.9"), atr=D("1.0")),
        account=AccountState(daily_pnl=D("-10"), weekly_pnl=D("-20"), consecutive_losses=0,
                             cooldown_remaining_s=0, minutes_since_open=D("60"), buying_power=D("1000")),
        contract=Contract(delta=D("0.45"), spread_pct=D("0.02"), mid=D("2.00")),
    )
    base.update(o)
    return MarketContext(**base)


def provider_of(context):
    return lambda ticker, direction: context


# ---- T2 配置 ----

def test_default_config_fails_closed_without_watcher_pool():
    assert default_rule_config().whitelist == ()


# ---- 方向归一化 ----

def test_normalize_direction_aliases():
    assert normalize_direction("LONG") == "long"
    assert normalize_direction("call") == "long"
    assert normalize_direction("做多") == "long"
    assert normalize_direction("S") == "short"
    assert normalize_direction("put") == "short"


def test_normalize_direction_invalid_raises():
    with pytest.raises(ValueError):
        normalize_direction("sideways")


# ---- evaluate 闸门 ----

def test_evaluate_allows_valid_long_default_qty_one():
    r = evaluate("QQQ", "long", provider=provider_of(ctx()), config=_WATCHER_CFG)
    assert r.allowed is True
    assert r.qty == 1


def test_evaluate_passes_user_qty_through():
    # mid 2.00 × 3 张 = $600 ≤ 上限 $600（$500×1.2）→ 放行回显 3
    r = evaluate("QQQ", "long", provider=provider_of(ctx()), config=_WATCHER_CFG, qty=3)
    assert r.allowed is True
    assert r.qty == 3


def test_evaluate_rejects_qty_over_position_ceiling():
    # mid 2.00 × 4 张 = $800 > 上限 $600 → position 拦
    r = evaluate("QQQ", "long", provider=provider_of(ctx()), config=_WATCHER_CFG, qty=4)
    assert r.allowed is False
    assert r.rule == "position"


def test_evaluate_normalizes_uppercase_direction():
    r = evaluate("QQQ", "LONG", provider=provider_of(ctx()), config=_WATCHER_CFG)
    assert r.allowed is True


def test_evaluate_rejects_off_whitelist():
    r = evaluate("TSLA", "long", provider=provider_of(ctx()), config=_WATCHER_CFG)
    assert r.allowed is False
    assert r.rule == "whitelist"


def test_evaluate_不再按距VWAP拦单():
    """追高闸 2026-09-01 删除：距 VWAP 5σ 也照样放行。"""
    far = ctx(signal=TickerSignal(price=D("105"), vwap=D("100"), atr=D("1.0"), vwap_sigma=D("1.0")))
    r = evaluate("QQQ", "long", provider=provider_of(far), config=_WATCHER_CFG)
    assert r.allowed is True, r.reason


def test_evaluate_两个方向结论恒等():
    """闸门不看方向：同一行情买 CALL 与买 PUT 必须一样。"""
    near = ctx(signal=TickerSignal(price=D("101.9"), vwap=D("100"), atr=D("1.0"), vwap_sigma=D("1.0")))
    long_r = evaluate("QQQ", "long", provider=provider_of(near), config=_WATCHER_CFG)
    short_r = evaluate("QQQ", "short", provider=provider_of(near), config=_WATCHER_CFG)
    assert long_r.allowed is True and short_r.allowed is True
