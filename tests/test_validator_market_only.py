# -*- coding: utf-8 -*-
"""`validate_market` —— 只跑**不需要合约**的规则 1–7b（审查 2.5/2.6 的地基）。

为什么要拆：前端要在卡片上显示"这票为什么不让买"，就得每票每秒干跑一次闸门。
但完整 validate 的规则 8/9 需要 Contract，而拿 Contract = 一次 Alpaca 期权链调用 ——
33 票 × 每秒一次，直接打爆券商。规则 1–7b 的输入（signal/account/config）全部
已在 watcher 内存里，纯计算，可以随便跑。

返回 None = 市场/账户层面全部通过；返回 ValidationResult = 第一条命中的拒绝。
"""
from decimal import Decimal as D

from app.validator import (
    AccountState, Contract, RuleConfig, TickerSignal, TradeRequest, ValidationInput, validate,
)
from app.validator.core import validate_market


def _cfg(**kw):
    base = dict(whitelist=("QQQ",))
    base.update(kw)
    return RuleConfig(**base)


def _inp(*, direction="long", daily_pnl=D("0"), cooldown=0,
         mins=D("60"), price=D("500"), vwap=D("500"), sigma=D("0"),
         trades_today=0, cfg=None, contract=None, ticker="QQQ"):
    return ValidationInput(
        request=TradeRequest(ticker=ticker, direction=direction, qty=1),
        signal=TickerSignal(price=price, vwap=vwap,
                            atr=D("1"), vwap_sigma=sigma),
        account=AccountState(daily_pnl=daily_pnl, weekly_pnl=D("0"), consecutive_losses=0,
                             cooldown_remaining_s=cooldown, minutes_since_open=mins,
                             buying_power=D("100000"), trades_today=trades_today),
        config=cfg or _cfg(),
        contract=contract)


# ── 通过 ──────────────────────────────────────────────────────────────────────
def test_returns_none_when_all_market_rules_pass():
    assert validate_market(_inp()) is None


def test_passes_without_any_contract():
    """没有 Contract 也能跑完 —— 这正是拆出来的意义（不必调期权链）。"""
    assert validate_market(_inp(contract=None)) is None


# ── 各条规则都要能命中并给出可读原因 ──────────────────────────────────────────
def test_blocks_on_breaker():
    r = validate_market(_inp(daily_pnl=D("-999")))
    assert r is not None and r.rule == "breaker"


def test_blocks_on_trade_count():
    r = validate_market(_inp(cfg=_cfg(max_trades_per_day=2), trades_today=2))
    assert r is not None and r.rule == "breaker" and "次数" in r.reason


def test_blocks_on_cooldown():
    r = validate_market(_inp(cooldown=300))
    assert r is not None and r.rule == "cooldown" and "300" in r.reason


def test_blocks_on_opening_blackout():
    r = validate_market(_inp(mins=D("2"), cfg=_cfg(opening_blackout_min=D("10"))))
    assert r is not None and r.rule == "blackout"


def test_blocks_on_afternoon_cutoff():
    r = validate_market(_inp(mins=D("200"), cfg=_cfg(entry_cutoff_mins=D("150"))))
    assert r is not None and r.rule == "afternoon"


def test_blocks_on_whitelist():
    r = validate_market(_inp(ticker="TSLA"))
    assert r is not None and r.rule == "whitelist"


def test_闸门与方向无关_两侧结论恒等():
    """2026-09-01 起 validate_market 里没有一条规则读 request.direction。

    震荡（效率比）、追高（VWAP ±kσ）、方向闸（1min/5min 均线共振）、动能闸
    （5min MACD 柱）四条同日删除。这条一旦红了，说明有人往这里加回了与方向
    相关的判据 —— 卡片就会重新出现「一边亮一边灰」。
    """
    for kw in ({}, {"price": D("506"), "vwap": D("500"), "sigma": D("2")},
               {"price": D("494"), "vwap": D("500"), "sigma": D("2")}):
        long_r = validate_market(_inp(direction="long", **kw))
        short_r = validate_market(_inp(direction="short", **kw))
        assert long_r is None and short_r is None, (kw, long_r, short_r)


def test_价格远离VWAP不再拦():
    """追高闸删除：±3σ 都放行。"""
    assert validate_market(_inp(price=D("506"), vwap=D("500"), sigma=D("2"))) is None
    assert validate_market(_inp(price=D("494"), vwap=D("500"), sigma=D("2"))) is None


# ── 与完整 validate 的一致性 ──────────────────────────────────────────────────
def test_full_validate_still_enforces_market_rules():
    """拆分不得改变完整 validate 的行为：市场规则仍先于合约规则命中。"""
    r = validate(_inp(ticker="TSLA", contract=Contract(delta=D("0.45"),
                                                       spread_pct=D("0.02"), mid=D("1.00"))))
    assert r.allowed is False and r.rule == "whitelist"


def test_full_validate_reaches_contract_rules_when_market_ok():
    """市场规则全过 → 继续走规则 8/9（合约为 None → 无合格合约）。"""
    r = validate(_inp(contract=None))
    assert r.allowed is False and r.rule == "contract"
