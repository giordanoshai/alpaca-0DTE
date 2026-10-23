# -*- coding: utf-8 -*-
"""
Realtime market regime calculator.

This module is used by the live watcher only. It does not read from or write to
Supabase. The input is the current in-memory 5m OHLCV DataFrame for one ticker,
and the output is a compact trend/regime payload for WebSocket/UI display.
"""

from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd


def ema(series: pd.Series, length: int) -> pd.Series:
    """Exponential moving average."""
    return series.ewm(span=length, adjust=False).mean()


def rsi(series: pd.Series, length: int = 9) -> pd.Series:
    """Wilder RSI."""
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(alpha=1 / length, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / length, adjust=False).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def macd(
    series: pd.Series,
    fast: int = 8,
    slow: int = 17,
    signal: int = 5,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """MACD line, signal line, and histogram."""
    macd_line = ema(series, fast) - ema(series, slow)
    signal_line = ema(macd_line, signal)
    hist = macd_line - signal_line
    return macd_line, signal_line, hist


def add_5m_indicators(df_5m: pd.DataFrame) -> pd.DataFrame:
    """Add the 5m indicators required for regime scoring."""
    df = df_5m.copy()

    df["ema9"] = ema(df["close"], 9)
    df["ema20"] = ema(df["close"], 20)
    df["ema9_slope_3"] = (df["ema9"] - df["ema9"].shift(3)) / df["close"]

    df["macd"], df["macd_signal"], df["macd_hist"] = macd(df["close"])
    df["macd_hist_delta"] = df["macd_hist"] - df["macd_hist"].shift(1)

    df["rsi9"] = rsi(df["close"], 9)
    df["rsi9_delta"] = df["rsi9"] - df["rsi9"].shift(1)

    return df


def classify_5m_gate(row: pd.Series) -> dict:
    """Score one 5m bar using EMA, MACD, and RSI direction checks."""
    bull_score = 0
    bear_score = 0

    if row["ema9"] > row["ema20"]:
        bull_score += 1
    elif row["ema9"] < row["ema20"]:
        bear_score += 1

    if row["ema9_slope_3"] > 0:
        bull_score += 1
    elif row["ema9_slope_3"] < 0:
        bear_score += 1

    if row["macd"] > row["macd_signal"]:
        bull_score += 1
    elif row["macd"] < row["macd_signal"]:
        bear_score += 1

    if row["macd_hist"] > 0:
        bull_score += 1
    elif row["macd_hist"] < 0:
        bear_score += 1

    if row["macd_hist_delta"] > 0:
        bull_score += 1
    elif row["macd_hist_delta"] < 0:
        bear_score += 1

    if row["rsi9"] >= 55:
        bull_score += 1
    elif row["rsi9"] <= 45:
        bear_score += 1

    if row["rsi9_delta"] > 0:
        bull_score += 1
    elif row["rsi9_delta"] < 0:
        bear_score += 1

    # 强度维度（与 TradingView market_regime.pine 对齐）
    if row["close"] > row["ema20"]:
        bull_score += 1
    elif row["close"] < row["ema20"]:
        bear_score += 1

    ema_spread = (row["ema9"] - row["ema20"]) / row["close"] if row["close"] != 0 else 0
    if ema_spread > 0.001:
        bull_score += 1
    elif ema_spread < -0.001:
        bear_score += 1

    if row["rsi9"] >= 65:
        bull_score += 1
    elif row["rsi9"] <= 35:
        bear_score += 1

    return {
        "bull_score_5m": bull_score,
        "bear_score_5m": bear_score,
    }


def add_5m_gate(df_5m: pd.DataFrame) -> pd.DataFrame:
    """Add bull/bear score columns to a 5m OHLCV DataFrame (vectorized).

    与 classify_5m_gate(row) 的 10 维评分逻辑保持一致，但用 numpy 向量化
    布尔运算替代 df.apply(axis=1)，整列计算取代 Python 逐行循环。
    """
    df = add_5m_indicators(df_5m)
    bull = np.zeros(len(df), dtype=np.int16)
    bear = np.zeros(len(df), dtype=np.int16)

    bull += (df["ema9"] > df["ema20"]).to_numpy(dtype=np.int16)
    bear += (df["ema9"] < df["ema20"]).to_numpy(dtype=np.int16)

    bull += (df["ema9_slope_3"] > 0).to_numpy(dtype=np.int16)
    bear += (df["ema9_slope_3"] < 0).to_numpy(dtype=np.int16)

    bull += (df["macd"] > df["macd_signal"]).to_numpy(dtype=np.int16)
    bear += (df["macd"] < df["macd_signal"]).to_numpy(dtype=np.int16)

    bull += (df["macd_hist"] > 0).to_numpy(dtype=np.int16)
    bear += (df["macd_hist"] < 0).to_numpy(dtype=np.int16)

    bull += (df["macd_hist_delta"] > 0).to_numpy(dtype=np.int16)
    bear += (df["macd_hist_delta"] < 0).to_numpy(dtype=np.int16)

    bull += (df["rsi9"] >= 55).to_numpy(dtype=np.int16)
    bear += (df["rsi9"] <= 45).to_numpy(dtype=np.int16)

    bull += (df["rsi9_delta"] > 0).to_numpy(dtype=np.int16)
    bear += (df["rsi9_delta"] < 0).to_numpy(dtype=np.int16)

    bull += (df["close"] > df["ema20"]).to_numpy(dtype=np.int16)
    bear += (df["close"] < df["ema20"]).to_numpy(dtype=np.int16)

    close = df["close"].to_numpy()
    ema9 = df["ema9"].to_numpy()
    ema20 = df["ema20"].to_numpy()
    with np.errstate(divide="ignore", invalid="ignore"):
        ema_spread = np.where(close != 0, (ema9 - ema20) / close, 0.0)
    bull += (ema_spread > 0.001).astype(np.int16)
    bear += (ema_spread < -0.001).astype(np.int16)

    bull += (df["rsi9"] >= 65).to_numpy(dtype=np.int16)
    bear += (df["rsi9"] <= 35).to_numpy(dtype=np.int16)

    df["bull_score_5m"] = bull
    df["bear_score_5m"] = bear
    return df


def label_market_regime(bull_score: int, bear_score: int) -> str:
    """Convert bull/bear scores into a stable regime label."""
    if bull_score >= 6 and bear_score <= 1:
        return "bull_strong"
    if bull_score >= 4 and bear_score <= 2:
        return "bull_weak"
    if bear_score >= 6 and bull_score <= 1:
        return "bear_strong"
    if bear_score >= 4 and bull_score <= 2:
        return "bear_weak"
    if bull_score <= 2 and bear_score <= 2:
        return "flat"
    return "mixed"


def regime_text(label: str) -> str:
    """Short UI label."""
    return {
        "bull_strong": "强多",
        "bull_weak": "偏多",
        "bear_strong": "强空",
        "bear_weak": "偏空",
        "flat": "横盘",
        "mixed": "震荡",
    }.get(label, "未知")


def calculate_current_regime(df_5m: pd.DataFrame) -> Optional[dict]:
    """
    Calculate the latest regime from current in-memory 5m OHLCV data.

    The DataFrame must contain open, high, low, close, and volume columns and use
    a DatetimeIndex. Returns None until enough bars exist for stable indicators.
    """
    if df_5m is None or df_5m.empty or len(df_5m) < 30:
        return None

    required = {"open", "high", "low", "close", "volume"}
    if not required.issubset(df_5m.columns):
        return None

    # 指标计算须在全表上完成（EMA/MACD/RSI 需要 lookback），但 10 维打分
    # 只针对最新一根 K 线，避免对历史 bar 重复打分（实时路径每个 bar 都会调一次）。
    df_ind = add_5m_indicators(df_5m)
    last = df_ind.iloc[-1]

    if pd.isna(last.get("ema9")) or pd.isna(last.get("ema20")):
        return None

    scores = classify_5m_gate(last)
    bull_score = int(scores["bull_score_5m"])
    bear_score = int(scores["bear_score_5m"])
    label = label_market_regime(bull_score, bear_score)

    return {
        "bull_score_5m": bull_score,
        "bear_score_5m": bear_score,
        "label": label,
        "text": regime_text(label),
        "timestamp": int(df_ind.index[-1].timestamp()),
    }
