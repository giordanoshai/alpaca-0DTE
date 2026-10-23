"""入场筛选二期：afternoon 午后禁入（2026-07-17 标定立项）。

规则位置：afternoon 在 blackout(3) 之后、whitelist(4) 之前。
（同期的 wall_room 距墙拒绝已随 GEX 于 2026-08-11 整体下线。）
"""
from __future__ import annotations

import dataclasses
from decimal import Decimal as D

from app.validator import (
    AccountState,
    Contract,
    RuleConfig,
    TickerSignal,
    TradeRequest,
    ValidationInput,
    validate,
)

_REQ = TradeRequest("QQQ", "long")
_SIG = TickerSignal(price=D("100"), vwap=D("99.8"), atr=D("1.0"), vwap_sigma=D("1.0"))
_ACC = AccountState(daily_pnl=D("-20"), weekly_pnl=D("-50"), consecutive_losses=0,
                    cooldown_remaining_s=0, minutes_since_open=D("60"), buying_power=D("1000"))
_CFG = RuleConfig(whitelist=("QQQ", "IWM", "SPY"))
_CON = Contract(delta=D("0.43"), spread_pct=D("0.021"), mid=D("2.00"))


def make(request=_REQ, signal=_SIG, account=_ACC, config=_CFG, contract=_CON):
    return ValidationInput(request=request, signal=signal, account=account,
                           config=config, contract=contract)


def sig(**o):
    return dataclasses.replace(_SIG, **o)


def acc(**o):
    return dataclasses.replace(_ACC, **o)


def cfg(**o):
    return dataclasses.replace(_CFG, **o)


# ============================================================
# ============================================================

# ============================================================
# afternoon 午后禁入（默认 150 分钟 = 12:00 ET；休市哨兵 >390 跳过）
# ============================================================

def test_afternoon_at_cutoff_rejects():
    r = validate(make(account=acc(minutes_since_open=D("150"))))
    assert r.allowed is False
    assert r.rule == "afternoon"


def test_afternoon_just_before_cutoff_passes():
    r = validate(make(account=acc(minutes_since_open=D("149.9"))))
    assert r.allowed is True


def test_afternoon_late_session_rejects():
    r = validate(make(account=acc(minutes_since_open=D("300"))))
    assert r.allowed is False
    assert r.rule == "afternoon"


def test_afternoon_market_closed_sentinel_skips():
    # 休市哨兵 9999（>390）不适用午后禁入
    r = validate(make(account=acc(minutes_since_open=D("9999"))))
    assert r.allowed is True


def test_afternoon_disabled_by_config():
    r = validate(make(account=acc(minutes_since_open=D("200")),
                      config=cfg(entry_cutoff_mins=None)))
    assert r.allowed is True


def test_afternoon_before_whitelist_priority():
    r = validate(make(request=TradeRequest("AAPL", "long"),
                      account=acc(minutes_since_open=D("200"))))
    assert r.rule == "afternoon"


def test_blackout_before_afternoon_priority():
    r = validate(make(account=acc(minutes_since_open=D("10"))))
    assert r.rule == "blackout"


# ============================================================
# 模板透传
# ============================================================

def test_template_carries_new_thresholds():
    from app.templates import Template
    tpl = Template(
        name="测试",
        max_daily_loss=D("150"), max_weekly_loss=D("300"), max_position_size=D("500"),
        opening_blackout_min=D("10"), min_delta=D("0.40"), max_spread_pct=D("0.03"),
        stop_pct=D("0.20"), max_loss_per_trade=D("80"), max_hold_mins=D("15"),
        cooldown_seconds=600,
    )
    rc = tpl.to_rule_config()
    assert rc.entry_cutoff_mins == D("150")


# ============================================================
# ============================================================

