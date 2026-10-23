# -*- coding: utf-8 -*-
"""流动性 / 报价地板 —— 这票现在能不能碰。

**这是硬闸，不是展示。** `market_context.signal_from_payload` 拿它 raise
「watcher 指示当前不可交易」，`feed_snapshot._dry_gates` 拿它把 CALL/PUT 两侧
一起置成 blocked。

2026-08-26 从 `app/services/trend_score.py` 整块搬来（趋势分下线）。它跟趋势
毫无关系，只是当初住错了地方 —— 原文件里的注释自己就写着「与趋势无关,
market_context 里是独立硬闸」。

⚠️ **搬家时一个字没改，包括 `_atr`。** 它是 Wilder 平滑
（`tr.ewm(alpha=1/period, adjust=False).mean()`），而 `app/market_state.py::atr()`
是简单均值（SMA-of-TR）且返回 `Decimal`。两者不是一回事 —— 顺手「统一」成后者，
这道闸的行为会在流动性边缘的票上翻号，而且翻了看不出来。
"""
from __future__ import annotations

import pandas as pd

# 只保留硬闸要的三项。趋势分那些参数(window/smooth_span/persist/strictness)
# 随趋势分一起删除，不要搬过来。
DEFAULT_CONFIG = {
    "lookback": 30,          # 主观察窗口 (bar 数)
    "atr_period": 14,
    "liquidity": {
        "min_atr_pct": 0.0001,   # ATR / close 最低比例
        "min_volume_med": 50,    # lookback 窗口内成交量中位数最低值
    },
}


def _merge_config(config: dict | None) -> dict:
    """把外部 config 合并进 DEFAULT_CONFIG, 子字典按键合并。"""
    cfg = dict(DEFAULT_CONFIG)
    for key, value in (config or {}).items():
        base = cfg.get(key)
        cfg[key] = {**base, **value} if isinstance(value, dict) and isinstance(base, dict) else value
    return cfg


def _prepare_df(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out.columns = out.columns.str.lower()
    return out


def _atr(df: pd.DataFrame, period: int) -> pd.Series:
    """Wilder 平滑的 ATR。**别换成 market_state.atr()**（那是简单均值），见模块注释。"""
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def _liquidity_ok(win: pd.DataFrame, atr_now: float, cfg: dict) -> bool:
    """跨标的排序地板: ATR% 或成交量过低视为报价失真/流动性枯竭。"""
    liq = cfg["liquidity"]
    close_now = float(win["close"].iloc[-1])
    if close_now <= 0 or atr_now <= 0:
        return False
    atr_pct = atr_now / close_now

    volume_window = win["volume"]
    index = win.index
    if isinstance(index, pd.DatetimeIndex) and len(index):
        session_index = index
        if index.tz is not None:
            session_index = index.tz_convert("America/New_York")
        latest = session_index[-1]
        latest_minute = latest.hour * 60 + latest.minute
        rth_open = 9 * 60 + 30
        rth_close = 16 * 60
        if rth_open <= latest_minute < rth_close:
            minutes = session_index.hour * 60 + session_index.minute
            rth_mask = (
                (session_index.date == latest.date())
                & (minutes >= rth_open)
                & (minutes < rth_close)
            )
            if rth_mask.any():
                volume_window = win.loc[rth_mask, "volume"]

    vol_med = float(volume_window.median())
    return atr_pct >= liq["min_atr_pct"] and vol_med >= liq["min_volume_med"]


def data_quality_ok(df: pd.DataFrame, config: dict | None = None) -> bool:
    """ATR% 或 RTH 成交量中位数过低 -> 这票现在不能碰。

    数据不足时抛 `ValueError` —— 调用方（`watcher_manager`）正靠
    `except ValueError: pass` 跳过预热期，**不要改成返回 False**：那会让预热期
    从「跳过不下发」变成「明确说不可交易」，前端红绿灯的含义跟着变。
    """
    cfg = _merge_config(config)
    prepared = _prepare_df(df)

    min_bars = cfg["lookback"] + cfg["atr_period"] + 5
    if len(prepared) < min_bars:
        raise ValueError(f"数据不足: 需要至少 {min_bars} 根 1min K线, 实际 {len(prepared)}")

    atr_now = float(_atr(prepared, cfg["atr_period"]).iat[-1])
    win = prepared.iloc[-cfg["lookback"]:]
    return _liquidity_ok(win, atr_now, cfg)
