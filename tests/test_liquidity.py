# -*- coding: utf-8 -*-
"""流动性硬闸：ATR% 或成交量过低 -> 这票现在不能碰。

**这是硬闸不是展示**：market_context 拿它 raise「暂不可交易」。
2026-08-26 从 trend_score.py 搬来，搬家时行为必须零变化。
"""
import pandas as pd
import pytest

from app.services.liquidity import DEFAULT_CONFIG, data_quality_ok


def _df(n=60, close=100.0, volume=1000, atr_span=1.0):
    """造一份最小可用的 1min K 线。atr_span 控制高低差 -> 控制 ATR%。"""
    idx = pd.date_range("2026-08-26 10:00", periods=n, freq="1min", tz="America/New_York")
    return pd.DataFrame({
        "open": [close] * n,
        "high": [close + atr_span / 2] * n,
        "low": [close - atr_span / 2] * n,
        "close": [close] * n,
        "volume": [volume] * n,
    }, index=idx)


def test_正常票放行():
    assert data_quality_ok(_df()) is True


def test_成交量过低判不可交易():
    """成交量中位数低于地板 -> 报价失真，不许碰。"""
    assert data_quality_ok(_df(volume=1)) is False


def test_波动过低判不可交易():
    """ATR 占价格比例低于 0.01% -> 盘口没在动，不许碰。"""
    assert data_quality_ok(_df(atr_span=0.0)) is False


def test_数据不足抛ValueError():
    """调用方(watcher_manager)正靠 except ValueError 跳过预热期，不能改成返回 False。"""
    with pytest.raises(ValueError, match="数据不足"):
        data_quality_ok(_df(n=10))


def test_默认配置只剩硬闸要的三项():
    """趋势分那些参数(window/smooth_span/persist/strictness)不该跟过来。"""
    assert set(DEFAULT_CONFIG) == {"lookback", "atr_period", "liquidity"}
