"""validate() —— 按优先级检查，第一条命中即拒绝，返回 (allowed, reason)。

规则顺序即 README「Validator 规则清单」：
  1 熔断 · 2 冷却 · 3 开盘禁入 · 3b 午后禁入 · 4 非白名单 · 8 资金 · 9 合约
（3b 为 2026-07-17 入场筛选二期，1211 笔实盘+模拟回放标定）

**空出来的规则号一律不重编号** —— 拦截日志与前端按 rule 名分类统计，重编号会断
历史口径。已删除的规则及其空位：

  · 5「震荡」（效率比）      2026-09-01 删除
  · 6「反向」（趋势分方向）  2026-08-08 删除
  · 6「方向闸」（1min/5min 均线共振）2026-08-21 复位、2026-09-01 再次删除
  · 7「追高」（VWAP ±kσ）    2026-09-01 删除
  · 7b「距墙」（GEX）        2026-08-11 随 GEX 整体下线删除
  · stall「动能闸」（5min MACD 柱收缩）2026-08-25 加、2026-09-01 删除

2026-09-01 那一批四条是用户决策：这些闸依赖的行情数据不可靠，实际效果是把绝大
多数行情都拦住了。买入方向从此完全由用户自己在面板上选 CALL / PUT，系统不再对
方向、位置、动能做任何判断。

纯函数，无副作用；拦截日志由调用方按 result 落库（阶段0 不接外部）。
"""
from __future__ import annotations

import math
from decimal import Decimal

from app.validator.constants import CONTRACT_MULTIPLIER
from app.validator.models import ValidationInput, ValidationResult


def position_ceiling(cfg) -> Decimal:
    """单标的仓位金额上限 = 最大仓位 × (1+容差)。规则 8 与加仓闸门共用，口径必须一致。"""
    return cfg.max_position_size * (Decimal(1) + cfg.position_tolerance)


def validate_market(inp: ValidationInput) -> ValidationResult | None:
    """规则 1–4：只看市场/账户/纪律，**不需要 Contract**。命中→拒绝结果；全过→None。

    单独拆出来是为了让前端能每票每秒干跑一次闸门（审查 2.5/2.6：用户只看得到
    "盯盘方向不明确"，看不到午后禁入 / 次数用尽这些真实原因）。
    完整 validate 的规则 8/9 需要 Contract，而取 Contract = 一次 Alpaca 期权链调用，
    33 票 × 每秒一次会打爆券商；这部分的输入全在 watcher 内存里，纯计算随便跑。

    **2026-09-01 起本函数与方向无关**：震荡/追高/均线共振/MACD 动能四条闸删除后，
    剩下的规则没有一条读 `req.direction`，CALL 与 PUT 两侧结果恒等。调用方
    （feed_snapshot._dry_gates）仍按两个方向各跑一遍 —— 那条通道不动，方向相关性
    以后要是回来，那边不必再改。
    """
    req = inp.request
    acc = inp.account
    cfg = inp.config

    # 1 · 熔断（硬刹车，模板可配）：日亏/周亏止损 · 日止盈锁利 · 今日交易次数（⑨）
    if -acc.daily_pnl >= cfg.max_daily_loss or -acc.weekly_pnl >= cfg.max_weekly_loss:
        return ValidationResult(False, "熔断 · 日亏/周亏触及硬刹车", "breaker")
    if acc.daily_pnl >= cfg.daily_profit_target:
        return ValidationResult(False, f"止盈锁利 · 今日 +${acc.daily_pnl:.0f} 收工", "breaker")
    if acc.trades_today >= cfg.max_trades_per_day:
        return ValidationResult(False, f"今日交易次数用尽 {acc.trades_today}/{cfg.max_trades_per_day}", "breaker")

    # 2 · 冷却：2 连亏后 600 秒内
    if acc.cooldown_remaining_s > 0:
        return ValidationResult(False, f"冷却中 剩{acc.cooldown_remaining_s}秒", "cooldown")

    # 3 · 开盘禁入：开盘后 N 分钟内（模板可配）
    if acc.minutes_since_open < cfg.opening_blackout_min:
        rem_min = math.ceil(cfg.opening_blackout_min - acc.minutes_since_open)
        return ValidationResult(False, f"开盘禁入 剩{rem_min}分", "blackout")

    # 3b · 午后禁入：开盘 N 分钟(默认150=12:00 ET)后禁新开仓（休市哨兵 >390 不适用）
    if (cfg.entry_cutoff_mins is not None
            and cfg.entry_cutoff_mins <= acc.minutes_since_open <= Decimal(390)):
        return ValidationResult(False, "午后禁入(≥12:00 ET)", "afternoon")

    # 4 · 非白名单：ticker 不在白名单
    if req.ticker.upper() not in {t.upper() for t in cfg.whitelist}:
        return ValidationResult(False, "非白名单标的", "whitelist")

    return None   # 市场/账户层面全过；仓位与合约规则见 validate()


def validate(inp: ValidationInput) -> ValidationResult:
    """完整闸门：市场规则（1–4，见 validate_market）→ 仓位/资金（8）→ 合约（9）。"""
    market_reject = validate_market(inp)
    if market_reject is not None:
        return market_reject

    req = inp.request
    acc = inp.account
    cfg = inp.config
    con = inp.contract

    # 8 · 手数守卫（⑥ 2026-07-16 起，数量由用户输入）：
    #     校验 数量×单手成本 ≤ 最大仓位×(1+容差)（超上限拦）且 ≤ 购买力（买不起拦）。
    #     （合约为空时无法评估成本，qty 保持 0，交给规则 9 拦截）
    qty = 0
    if con is not None:
        qty = req.qty
        if qty < 1:
            return ValidationResult(False, "下单数量非法", "position", 0)
        entry_price = con.ask if con.ask is not None else con.mid
        cost = entry_price * CONTRACT_MULTIPLIER * Decimal(qty)
        ceiling = position_ceiling(cfg)
        # 同标的已占用敞口 + 本单成本 ≤ 上限。只看本单的话，分两次各买 $500 会各自放行、
        # 实际敞口翻倍（审查 2.4；用户 pine_des 里"靠自觉性控制"说的就是这个）。
        total = inp.existing_exposure + cost
        if total > ceiling:
            return ValidationResult(
                False, f"同标的持仓合计 ${total:.0f} 超上限（约 ${ceiling:.0f}）", "position", 0)
        if cost > acc.buying_power:
            return ValidationResult(False, "资金不足", "funds", 0)

    # 9 · 合约：③ 选不出 delta≥阈值 且 spread≤阈值 的合约（模板可配）。
    # 把真实失败项返回前端，避免所有情况都折叠成无法排查的“无合格合约”。
    if con is None:
        return ValidationResult(False, "无可用期权合约", "contract", 0)
    # delta 为 None = 行情档不提供 greeks（Alpaca 免费 indicative 档实测 23/23 全缺）。
    # 这道闸此时**无从评估**：按 fail-open 放行，但记进 skipped 明示「没判过」。
    # 2026-08-27 前的做法是在 contract_for_validation 里兜底 0.50，恒 ≥0.40 恒判通过，
    # 于是日志和前端一路显示「delta 通过」，看不出这道闸从来没真正拦过东西。
    # 点差是真数，不受 greeks 缺失影响，照常评估。
    skipped: tuple[str, ...] = ()
    if con.delta is None:
        skipped = ("delta",)
    elif con.delta < cfg.min_delta:
        return ValidationResult(
            False,
            f"合约 delta {con.delta:.2f} 低于最低 {cfg.min_delta:.2f}",
            "contract",
            0,
        )
    if con.spread_pct > cfg.max_spread_pct:
        return ValidationResult(
            False,
            f"合约点差 {con.spread_pct * Decimal(100):.1f}% "
            f"超过上限 {cfg.max_spread_pct * Decimal(100):.1f}%",
            "contract",
            0,
        )

    return ValidationResult(True, "", "", qty, skipped=skipped)
