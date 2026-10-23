# -*- coding: utf-8 -*-
"""规则 8（仓位守卫）必须把**同标的已有持仓**算进去（审查 2.4）。

此前 `cost = con.mid × 100 × qty` 只看本单，与 REGISTRY 里同标的已持仓无关 ——
先买 $500 再买 $500 两单各自都放行，实际敞口 $1000、翻倍于 T1 铁律上限。
用户在 pine_des 里写「完全是靠我自觉性来控制」，实情比这更松：规则根本没算这笔账。
"""
from decimal import Decimal as D

import pytest

from app.validator import (
    AccountState, Contract, RuleConfig, TickerSignal, TradeRequest, ValidationInput, validate,
)


def _cfg(**kw):
    base = dict(whitelist=("QQQ",),
                max_position_size=D("500"), position_tolerance=D("0.20"))
    base.update(kw)
    return RuleConfig(**base)


def _inp(*, qty=1, mid=D("1.00"), ask=None, existing=D("0"), cfg=None):
    return ValidationInput(
        request=TradeRequest(ticker="QQQ", direction="long", qty=qty),
        signal=TickerSignal(price=D("500"),
                            vwap=D("500"), atr=D("1"), vwap_sigma=D("0")),
        account=AccountState(daily_pnl=D("0"), weekly_pnl=D("0"), consecutive_losses=0,
                             cooldown_remaining_s=0, minutes_since_open=D("60"),
                             buying_power=D("100000")),
        config=cfg or _cfg(),
        contract=Contract(delta=D("0.45"), spread_pct=D("0.02"), mid=mid, ask=ask),
        existing_exposure=existing,
    )


# 上限 = 500 × 1.20 = $600；单张成本 = mid × 100

def test_allows_first_entry_within_ceiling():
    """无已有持仓、单笔 $400 → 放行（既有行为不变）。"""
    r = validate(_inp(qty=4, mid=D("1.00")))     # 4 × 100 = $400
    assert r.allowed is True and r.qty == 4


def test_blocks_when_existing_plus_new_exceeds_ceiling():
    """已持 $500 + 再买 $400 = $900 > $600 上限 → 拦。这正是用户描述的场景。"""
    r = validate(_inp(qty=4, mid=D("1.00"), existing=D("500")))
    assert r.allowed is False
    assert r.rule == "position"
    assert r.qty == 0


def test_allows_when_existing_plus_new_within_ceiling():
    """已持 $200 + 再买 $300 = $500 ≤ $600 → 放行（加仓不是一律禁止）。"""
    r = validate(_inp(qty=3, mid=D("1.00"), existing=D("200")))
    assert r.allowed is True and r.qty == 3


def test_ceiling_is_inclusive():
    """正好等于上限 → 放行（用 > 判定，不是 >=）。"""
    r = validate(_inp(qty=1, mid=D("1.00"), existing=D("500")))   # 500+100 = 600
    assert r.allowed is True


def test_reason_reports_combined_exposure():
    """拒单理由要说清是**合计**超限，否则用户看到"数量超最大持仓金额"会以为是本单太大。"""
    r = validate(_inp(qty=4, mid=D("1.00"), existing=D("500")))
    assert "900" in r.reason        # 合计敞口
    assert "600" in r.reason        # 上限


def test_existing_exposure_defaults_to_zero():
    """不传 existing_exposure 时按 0 处理（老调用方无需改造）。"""
    inp = ValidationInput(
        request=TradeRequest(ticker="QQQ", direction="long", qty=4),
        signal=TickerSignal(price=D("500"),
                            vwap=D("500"), atr=D("1"), vwap_sigma=D("0")),
        account=AccountState(daily_pnl=D("0"), weekly_pnl=D("0"), consecutive_losses=0,
                             cooldown_remaining_s=0, minutes_since_open=D("60"),
                             buying_power=D("100000")),
        config=_cfg(),
        contract=Contract(delta=D("0.45"), spread_pct=D("0.02"), mid=D("1.00")),
    )
    assert validate(inp).allowed is True


def test_funds_check_still_uses_new_order_cost_only():
    """资金够不够只看这一单要花多少钱（已有持仓的钱早就付过了）。"""
    cfg = _cfg(max_position_size=D("100000"))    # 放开仓位上限，单独验资金规则
    inp = _inp(qty=1, mid=D("1.00"), existing=D("50000"), cfg=cfg)
    assert validate(inp).allowed is True


def test_entry_cost_uses_executable_ask_instead_of_mid():
    """买入需要支付 ask；mid=$1.00 但 ask=$1.30 时，5 张实际 $650 应超过 $600 上限。"""
    result = validate(_inp(qty=5, mid=D("1.00"), ask=D("1.30")))
    assert result.allowed is False
    assert result.rule == "position"
    assert "650" in result.reason
