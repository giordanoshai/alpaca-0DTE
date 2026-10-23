# -*- coding: utf-8 -*-
"""趋势键消失后闸门必须照常放行（2026-08-26 趋势分下线）。

watcher_manager 里原有一行注释：「漏 trend_direction 会让 market_context
fail-closed 全天拦死」。本次正是要删掉那个键 —— 这条用例守的就是「删完之后
第二天开盘不会所有单被拦」。**这是整次改造最容易炸的地方。**
"""
from decimal import Decimal as D

from app.services.market_context import signal_from_payload


def _payload(**over):
    """一份**不含任何趋势键**的 watcher state。"""
    state = {
        "trend_score": {"data_quality_ok": True},   # 只剩硬闸这一个键
        "ema_dir": {"m1": "up", "m5": "up"},
        "er": 0.55,
        "current_bar": {"last_price": 100.0},
        "vwap": 99.0,
        "atr": 1.0,
        "vwap_sigma": 1.0,
        "indicators": {},
    }
    state.update(over)
    return {"kind": "watcher", "state": state}


def test_没有趋势键也能构造出信号():
    sig = signal_from_payload(_payload(), require_tradable=True)
    assert sig.price == D("100.0")
    assert sig.vwap == D("99.0")


def test_没有趋势键闸门照常放行():
    """核心断言：删掉四个趋势键之后，能买的还能买。"""
    from app.validator.core import validate_market
    from app.validator.models import (
        AccountState, RuleConfig, TradeRequest, ValidationInput,
    )

    sig = signal_from_payload(_payload(), require_tradable=True)
    result = validate_market(ValidationInput(
        request=TradeRequest(ticker="QQQ", direction="long", qty=1),
        signal=sig,
        # 账户侧全部给「什么都不拦」的值：本用例只想看趋势键缺失会不会拦单，
        # 熔断/冷却/开盘禁入/午后禁入任何一条命中都会把结论污染成假绿。
        account=AccountState(
            daily_pnl=D("0"), weekly_pnl=D("0"), consecutive_losses=0,
            cooldown_remaining_s=0, minutes_since_open=D("60"),
            buying_power=D("100000"), trades_today=0),
        config=RuleConfig(whitelist=("QQQ",),
                          opening_blackout_min=D("10"), entry_cutoff_mins=None),
    ))
    assert result is None, f"不该被拦，却拦了：{result.reason if result else ''}"


def test_数据质量不合格仍然拦住():
    """搬家后的硬闸必须还在生效。"""
    import pytest
    with pytest.raises(RuntimeError, match="不可交易"):
        signal_from_payload(
            _payload(trend_score={"data_quality_ok": False}), require_tradable=True)
