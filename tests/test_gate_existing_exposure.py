# -*- coding: utf-8 -*-
"""闸门要把 REGISTRY 里同标的已占用敞口喂给 validator 规则 8（审查 2.4 接线部分）。

validator 保持纯函数、不认识持仓登记；由 order_gate（交易层，本来就该知道持仓）
算好 existing_exposure 传进去 —— 而不是让 datasource 去 import order_state
（那是审查第四节点名的反向依赖，不能再加深）。
"""
from datetime import datetime, timezone
from decimal import Decimal as D

from app.order_gate import MarketContext, evaluate_with_context, existing_exposure
from app.order_state import REGISTRY, ManagedPosition
from app.validator import AccountState, Contract, RuleConfig, TickerSignal


def _seed(ticker, qty, entry_mid, status="open", code=None):
    p = ManagedPosition(option_code=code or f"{ticker}260716C00500000", ticker=ticker,
                        direction="long", qty=qty, entry_mid=D(entry_mid),
                        entry_time=datetime.now(timezone.utc), order_id=f"o-{ticker}-{code}",
                        status=status)
    REGISTRY.record(p)
    return p


def _cfg():
    return RuleConfig(whitelist=("QQQ",),
                      max_position_size=D("500"), position_tolerance=D("0.20"))


def _provider(ticker, direction):
    return MarketContext(
        signal=TickerSignal(price=D("500"),
                            vwap=D("500"), atr=D("1"), vwap_sigma=D("0")),
        account=AccountState(daily_pnl=D("0"), weekly_pnl=D("0"), consecutive_losses=0,
                             cooldown_remaining_s=0, minutes_since_open=D("60"),
                             buying_power=D("100000")),
        contract=Contract(delta=D("0.45"), spread_pct=D("0.02"), mid=D("1.00")))


# ── existing_exposure 纯计算 ──────────────────────────────────────────────────
def test_exposure_sums_same_ticker_entry_cost():
    REGISTRY._pos.clear()
    _seed("QQQ", qty=3, entry_mid="1.00", code="A")      # $300
    _seed("QQQ", qty=2, entry_mid="1.00", code="B")      # $200
    assert existing_exposure("QQQ") == D("500")
    REGISTRY._pos.clear()


def test_exposure_ignores_other_tickers():
    REGISTRY._pos.clear()
    _seed("QQQ", qty=3, entry_mid="1.00", code="A")
    _seed("SPY", qty=9, entry_mid="1.00", code="C")
    assert existing_exposure("QQQ") == D("300")
    REGISTRY._pos.clear()


def test_exposure_counts_both_directions_of_same_underlying():
    """同标的的 CALL + PUT 都是这只票上的真金白银（跨式也是 $1000 敞口）。"""
    REGISTRY._pos.clear()
    _seed("QQQ", qty=2, entry_mid="1.00", code="QQQ260716C00500000")
    p = _seed("QQQ", qty=2, entry_mid="1.00", code="QQQ260716P00500000")
    p.direction = "short"
    assert existing_exposure("QQQ") == D("400")
    REGISTRY._pos.clear()


def test_exposure_counts_pending_orders():
    """pending 是已提交未成交，这笔钱已经承诺出去了，必须计入。"""
    REGISTRY._pos.clear()
    _seed("QQQ", qty=4, entry_mid="1.00", status="pending", code="A")
    assert existing_exposure("QQQ") == D("400")
    REGISTRY._pos.clear()


def test_exposure_excludes_closed_positions():
    REGISTRY._pos.clear()
    _seed("QQQ", qty=4, entry_mid="1.00", status="closed", code="A")
    assert existing_exposure("QQQ") == D("0")
    REGISTRY._pos.clear()


def test_exposure_is_case_insensitive():
    REGISTRY._pos.clear()
    _seed("QQQ", qty=1, entry_mid="1.00", code="A")
    assert existing_exposure("qqq") == D("100")
    REGISTRY._pos.clear()


# ── 接线：闸门把敞口带进 validator ────────────────────────────────────────────
def test_gate_blocks_second_entry_that_busts_ceiling():
    """已持 $500（上限 $600），再买 4 张 × $100 → 合计 $900，闸门必须拦。"""
    REGISTRY._pos.clear()
    _seed("QQQ", qty=5, entry_mid="1.00", code="A")       # $500
    result, _ = evaluate_with_context("QQQ", "long", provider=_provider, config=_cfg(), qty=4)
    assert result.allowed is False
    assert result.rule == "position"
    REGISTRY._pos.clear()


def test_gate_allows_when_no_existing_position():
    REGISTRY._pos.clear()
    result, _ = evaluate_with_context("QQQ", "long", provider=_provider, config=_cfg(), qty=4)
    assert result.allowed is True and result.qty == 4
    REGISTRY._pos.clear()
