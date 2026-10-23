"""④ Pre-trade validator 单元测试。

README 工程约定：validator 每条规则至少 3 个用例（通过/拒绝/边界）。
规则优先级（第一条命中即拒绝）：
  1 breaker 熔断 · 2 cooldown 冷却 · 3 blackout 开盘禁入 · 4 whitelist 非白名单
  8 funds 资金 · 9 contract 合约

2026-09-01：规则 5（震荡）、6（方向闸）、7（追高）与 stall（5min MACD 动能）删除，
空出来的规则号不重编号（拦截日志按 rule 名分类，重编号会断历史口径）。
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

# ---- 默认「全部通过」的输入，各测试只改动触发单条规则的字段 ----

_REQ = TradeRequest("QQQ", "long")
_SIG = TickerSignal(price=D("100"), vwap=D("99.8"), atr=D("1.0"), vwap_sigma=D("1.0"))
_ACC = AccountState(daily_pnl=D("-20"), weekly_pnl=D("-50"), consecutive_losses=0,
                    cooldown_remaining_s=0, minutes_since_open=D("60"), buying_power=D("1000"))
_CFG = RuleConfig(whitelist=("QQQ", "IWM", "SPY"))
_CON = Contract(delta=D("0.43"), spread_pct=D("0.021"), mid=D("2.00"))


def sig(**o):
    return dataclasses.replace(_SIG, **o)


def acc(**o):
    return dataclasses.replace(_ACC, **o)


def cfg(**o):
    return dataclasses.replace(_CFG, **o)


def con(**o):
    return dataclasses.replace(_CON, **o)


def make(request=_REQ, signal=_SIG, account=_ACC, config=_CFG, contract=_CON):
    return ValidationInput(request=request, signal=signal, account=account,
                           config=config, contract=contract)


# ============================================================
# 基线：默认输入（qty 默认 1）应放行，回显 1 张
# ============================================================

def test_baseline_allows_and_echoes_qty_one():
    r = validate(make())
    assert r.allowed is True
    assert r.reason == ""
    assert r.rule == ""
    assert r.qty == 1


# ============================================================
# 规则 1 · breaker 熔断（日亏≥$150 或 周亏≥$300）
# ============================================================

def test_breaker_daily_loss_at_threshold_rejects():
    r = validate(make(account=acc(daily_pnl=D("-150"))))
    assert r.allowed is False
    assert r.rule == "breaker"


def test_breaker_daily_loss_just_below_passes():
    r = validate(make(account=acc(daily_pnl=D("-149.99"))))
    assert r.allowed is True


def test_breaker_weekly_loss_at_threshold_rejects():
    r = validate(make(account=acc(weekly_pnl=D("-300"))))
    assert r.allowed is False
    assert r.rule == "breaker"


def test_breaker_weekly_loss_just_below_passes():
    r = validate(make(account=acc(weekly_pnl=D("-299.99"))))
    assert r.allowed is True


# ============================================================
# 规则 2 · cooldown 冷却（2 连亏后 600 秒内）
# ============================================================

def test_cooldown_remaining_rejects_with_seconds_in_reason():
    r = validate(make(account=acc(consecutive_losses=2, cooldown_remaining_s=300)))
    assert r.allowed is False
    assert r.rule == "cooldown"
    assert "300" in r.reason


def test_cooldown_zero_remaining_passes():
    r = validate(make(account=acc(consecutive_losses=2, cooldown_remaining_s=0)))
    assert r.allowed is True


def test_cooldown_one_second_remaining_rejects():
    r = validate(make(account=acc(consecutive_losses=2, cooldown_remaining_s=1)))
    assert r.allowed is False
    assert r.rule == "cooldown"


# ============================================================
# 规则 3 · blackout 开盘 45 分钟禁入
# ============================================================

def test_blackout_within_window_rejects():
    r = validate(make(account=acc(minutes_since_open=D("30"))))
    assert r.allowed is False
    assert r.rule == "blackout"


def test_blackout_at_45_min_passes():
    r = validate(make(account=acc(minutes_since_open=D("45"))))
    assert r.allowed is True


def test_blackout_just_under_45_rejects():
    r = validate(make(account=acc(minutes_since_open=D("44.9"))))
    assert r.allowed is False
    assert r.rule == "blackout"


# ============================================================
# 规则 4 · whitelist 非白名单
# ============================================================

def test_whitelist_off_list_rejects():
    r = validate(make(request=TradeRequest("TSLA", "long")))
    assert r.allowed is False
    assert r.rule == "whitelist"


def test_whitelist_on_list_passes():
    r = validate(make(request=TradeRequest("SPY", "short")))
    assert r.allowed is True


def test_whitelist_is_case_insensitive():
    r = validate(make(request=TradeRequest("qqq", "long")))
    assert r.allowed is True


# ============================================================
# 规则 5（震荡 / 效率比）、规则 6（1min-5min 均线共振方向闸）、规则 7（追高 /
# VWAP ±kσ）与 stall 动能闸 —— **2026-09-01 全部删除**，对应的三大段用例随之移除。
# 依赖的行情数据不可靠，实际效果是把绝大多数行情都拦住了（用户决策）。
# 买入方向从此由用户在下单面板上自己选 CALL / PUT，闸门不再看方向、位置、动能。
#
# 下面这条是**反向断言**：守住「不许把方向硬拦截加回来」。


def test_闸门不看方向_同一行情两个方向都放行():
    """CALL 与 PUT 只有 direction 不同，其余完全一样 —— 必须同放行。

    这条一旦红了，说明有人往 validate_market 里加回了与方向相关的判据。
    """
    long_r = validate(make(request=TradeRequest("QQQ", "long")))
    short_r = validate(make(request=TradeRequest("QQQ", "short")))
    assert long_r.allowed is True, long_r.reason
    assert short_r.allowed is True, short_r.reason


def test_价格远离VWAP也不再拦():
    """追高闸删除：+5σ 处买 CALL 照样放行。"""
    s = sig(price=D("105"), vwap=D("100"), vwap_sigma=D("1.0"))
    r = validate(make(request=TradeRequest("QQQ", "long"), signal=s))
    assert r.allowed is True, r.reason


# ============================================================
# 规则 8 · 手数守卫（⑥：数量由用户给，校验 ≤ 最大仓位×(1+容差) 且 ≤ 购买力）
# 容差默认 20% → MAX_POSITION_SIZE $500 的上限 = $600
# ============================================================

def req(**o):
    return dataclasses.replace(_REQ, **o)


def test_qty_default_one_allows():
    r = validate(make())  # qty 默认 1，mid 2.00 → $200 ≤ 上限 $600
    assert r.allowed is True
    assert r.qty == 1


def test_qty_within_ceiling_allows():
    r = validate(make(request=req(qty=2)))  # $400 ≤ 上限 $600
    assert r.allowed is True
    assert r.qty == 2


def test_qty_at_ceiling_boundary_allows():
    # mid 3.00 × 2 张 = $600，恰好等于上限（$500×1.2）→ 放行
    r = validate(make(request=req(qty=2), contract=con(mid=D("3.00")), account=acc(buying_power=D("5000"))))
    assert r.allowed is True
    assert r.qty == 2


def test_qty_over_ceiling_rejects():
    # mid 3.50 × 2 张 = $700 > 上限 $600 → 拦（position）
    r = validate(make(request=req(qty=2), contract=con(mid=D("3.50")), account=acc(buying_power=D("5000"))))
    assert r.allowed is False
    assert r.rule == "position"
    assert r.qty == 0


def test_qty_exceeds_buying_power_rejects():
    # $200 在上限内，但购买力仅 $100 → funds（买不起）
    r = validate(make(account=acc(buying_power=D("100"))))
    assert r.allowed is False
    assert r.rule == "funds"
    assert r.qty == 0


def test_qty_custom_tolerance_widens_ceiling():
    # 容差 50% → 上限 $750；mid 3.50 × 2 = $700 ≤ $750 → 放行
    r = validate(make(request=req(qty=2), contract=con(mid=D("3.50")),
                      config=cfg(position_tolerance=D("0.50")), account=acc(buying_power=D("5000"))))
    assert r.allowed is True
    assert r.qty == 2


def test_qty_zero_rejects():
    r = validate(make(request=req(qty=0)))
    assert r.allowed is False
    assert r.rule == "position"


# ============================================================
# 规则 9 · contract 合约（delta≥0.40 且 spread≤3%）
# ============================================================

def test_contract_none_rejects():
    r = validate(make(contract=None))
    assert r.allowed is False
    assert r.rule == "contract"
    assert r.reason == "无可用期权合约"


def test_contract_low_delta_rejects():
    r = validate(make(contract=con(delta=D("0.39"))))
    assert r.allowed is False
    assert r.rule == "contract"
    assert "delta 0.39 低于最低 0.40" in r.reason


def test_contract_delta_unknown_skips_gate_without_faking_pass():
    """行情档不提供 greeks 时 delta 为 None，这道闸无从评估。

    按本项目 fail-open 惯例放行，但必须在 skipped 里写明「没判过」。
    此前的做法是在 contract_for_validation 里兜底 0.50，恒 ≥0.40 恒判通过，
    于是日志和前端都看不出这道闸其实一直在空转（2026-08-27 修）。
    """
    r = validate(make(contract=con(delta=None)))
    assert r.allowed is True
    assert r.skipped == ("delta",)


def test_contract_real_delta_marks_gate_evaluated():
    """有真 delta 时 skipped 必须为空，否则「判过」和「没判」又混成一潭。"""
    r = validate(make(contract=con(delta=D("0.43"))))
    assert r.allowed is True
    assert r.skipped == ()


def test_contract_unknown_delta_still_checks_spread():
    """delta 判不了不等于整条规则 9 失效——点差是真数，照拦。"""
    r = validate(make(contract=con(delta=None, spread_pct=D("0.031"))))
    assert r.allowed is False
    assert r.rule == "contract"
    assert "点差 3.1% 超过上限 3.0%" in r.reason


def test_contract_wide_spread_rejects():
    r = validate(make(contract=con(spread_pct=D("0.031"))))
    assert r.allowed is False
    assert r.rule == "contract"
    assert "点差 3.1% 超过上限 3.0%" in r.reason


def test_contract_at_boundaries_passes():
    r = validate(make(contract=con(delta=D("0.40"), spread_pct=D("0.03"))))
    assert r.allowed is True


# ============================================================
# 优先级：第一条命中即拒绝
# ============================================================

def test_priority_breaker_beats_all():
    r = validate(make(
        request=TradeRequest("TSLA", "long"),          # 会触发 whitelist
        account=acc(daily_pnl=D("-200"), cooldown_remaining_s=300, minutes_since_open=D("5")),
    ))
    assert r.rule == "breaker"


def test_profit_target_locks_in():
    # ⑨ 日盈达止盈线（默认 $300）→ 锁利收工（breaker）
    r = validate(make(account=acc(daily_pnl=D("300"))))
    assert r.allowed is False
    assert r.rule == "breaker"
    assert "止盈" in r.reason


def test_below_profit_target_allows():
    r = validate(make(account=acc(daily_pnl=D("299"))))
    assert r.allowed is True


def test_trade_count_breaker_at_limit():
    # ⑨ 今日交易次数达上限（默认 6）→ 熔断
    r = validate(make(account=acc(trades_today=6)))
    assert r.allowed is False
    assert r.rule == "breaker"
    assert "交易次数" in r.reason


def test_trade_count_below_limit_allows():
    r = validate(make(account=acc(trades_today=5)))
    assert r.allowed is True


def test_priority_cooldown_beats_blackout():
    r = validate(make(account=acc(consecutive_losses=2, cooldown_remaining_s=300,
                                  minutes_since_open=D("10"))))
    assert r.rule == "cooldown"


def test_priority_whitelist_beats_position():
    # 非白名单 + 数量非法都成立 → 先命中 whitelist(4) 而非 position(8)
    r = validate(make(request=TradeRequest("TSLA", "long", qty=0)))
    assert r.rule == "whitelist"


def test_priority_contract_when_none_defers_funds():
    # 无合格合约且资金不足 → funds 无法评估，命中 contract(9)
    r = validate(make(contract=None, account=acc(buying_power=D("10"))))
    assert r.rule == "contract"
