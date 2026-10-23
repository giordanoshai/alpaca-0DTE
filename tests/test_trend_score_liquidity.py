"""trend_score 流动性地板的交易时段边界回归测试。"""

import pandas as pd

from app.services.liquidity import DEFAULT_CONFIG, _liquidity_ok


def _window(index: pd.DatetimeIndex, volumes: list[int]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open": [100.0] * len(index),
            "high": [101.0] * len(index),
            "low": [99.0] * len(index),
            "close": [100.0] * len(index),
            "volume": volumes,
        },
        index=index,
    )


def test_rth_liquidity_ignores_premarket_zero_volume_bars_at_blackout_boundary():
    """09:40 ET 已过 10 分钟；盘前 Yahoo 零量线不得再制造隐藏禁入窗口。"""
    index = pd.date_range(
        "2026-07-27 09:11",
        "2026-07-27 09:40",
        freq="1min",
        tz="America/New_York",
    )
    volumes = [0] * 19 + [10_000] * 11

    assert _liquidity_ok(_window(index, volumes), atr_now=1.0, cfg=DEFAULT_CONFIG)


def test_rth_zero_volume_bars_still_count_as_illiquid():
    """常规盘内的真实零成交 bar 仍参与中位数，稀疏成交不能被放行。"""
    index = pd.date_range(
        "2026-07-27 09:30",
        periods=30,
        freq="1min",
        tz="America/New_York",
    )
    volumes = [0] * 20 + [10_000] * 10

    assert not _liquidity_ok(_window(index, volumes), atr_now=1.0, cfg=DEFAULT_CONFIG)


def test_utc_index_is_converted_to_et_before_session_filtering():
    index_et = pd.date_range(
        "2026-07-27 09:11",
        "2026-07-27 09:40",
        freq="1min",
        tz="America/New_York",
    )
    volumes = [0] * 19 + [10_000] * 11

    assert _liquidity_ok(
        _window(index_et.tz_convert("UTC"), volumes),
        atr_now=1.0,
        cfg=DEFAULT_CONFIG,
    )
