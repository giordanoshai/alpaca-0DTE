"""
指标计算模块 - 使用 pandas-ta classic 模式
只做技术指标计算，不含策略逻辑
"""

import pandas as pd
import pandas_ta_classic as ta


def add_macd(
    df: pd.DataFrame,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
    prefix: str = ""
) -> pd.DataFrame:
    """添加 MACD 指标

    Args:
        df: 包含 'close' 列的 DataFrame
        fast: 快线周期
        slow: 慢线周期
        signal: 信号线周期
        prefix: 列名前缀

    Returns:
        添加 MACD_列后的 DataFrame
    """
    result = df.copy()
    macd = ta.macd(result['close'], fast=fast, slow=slow, signal=signal)

    col_macd = f"{prefix}MACD_{fast}_{slow}_{signal}"
    col_hist = f"{prefix}MACDh_{fast}_{slow}_{signal}"
    col_signal = f"{prefix}MACDs_{fast}_{slow}_{signal}"

    if macd is not None:
        result[col_macd] = macd[f"MACD_{fast}_{slow}_{signal}"]
        result[col_hist] = macd[f"MACDh_{fast}_{slow}_{signal}"]
        result[col_signal] = macd[f"MACDs_{fast}_{slow}_{signal}"]
    else:
        # 数据量不足时，ta.macd 返回 None，此处填充 NaN 以防程序崩溃
        result[col_macd] = pd.Series(index=result.index, dtype='float64')
        result[col_hist] = pd.Series(index=result.index, dtype='float64')
        result[col_signal] = pd.Series(index=result.index, dtype='float64')

    return result


def add_rsi(
    df: pd.DataFrame,
    period: int = 9,
    signal_period: int = 14,
    prefix: str = ""
) -> pd.DataFrame:
    """添加 RSI 指标及信号线

    Args:
        df: 包含 'close' 列的 DataFrame
        period: RSI 周期
        signal_period: RSI 信号线 (SMA) 周期
        prefix: 列名前缀

    Returns:
        添加 RSI 列及信号线列后的 DataFrame
    """
    result = df.copy()
    col_name = f"{prefix}RSI_{period}"
    result[col_name] = ta.rsi(result['close'], length=period)

    if signal_period > 0:
        col_signal = f"{col_name}_MA_{signal_period}"
        result[col_signal] = ta.sma(result[col_name], length=signal_period)

    return result


def add_ema(
    df: pd.DataFrame,
    periods: list[int],
    prefix: str = ""
) -> pd.DataFrame:
    """添加 EMA 指标

    Args:
        df: 包含 'close' 列的 DataFrame
        periods: EMA 周期列表
        prefix: 列名前缀

    Returns:
        添加 EMA 列后的 DataFrame
    """
    result = df.copy()
    for p in periods:
        col_name = f"{prefix}EMA_{p}"
        result[col_name] = ta.ema(result['close'], length=p)
    return result


def add_atr(
    df: pd.DataFrame,
    period: int = 14,
    prefix: str = ""
) -> pd.DataFrame:
    """添加 ATR 指标

    Args:
        df: 包含 'high', 'low', 'close' 列的 DataFrame
        period: ATR 周期
        prefix: 列名前缀

    Returns:
        添加 ATR 列后的 DataFrame
    """
    result = df.copy()
    col_name = f"{prefix}ATR_{period}"
    result[col_name] = ta.atr(result['high'], result['low'], result['close'], length=period)
    return result


def add_sma(
    df: pd.DataFrame,
    column: str,
    period: int,
    prefix: str = ""
) -> pd.DataFrame:
    """添加 SMA 指标

    Args:
        df: DataFrame
        column: 要计算 SMA 的列名
        period: SMA 周期
        prefix: 列名前缀

    Returns:
        添加 SMA 列后的 DataFrame
    """
    result = df.copy()
    col_name = f"{prefix}SMA_{period}"
    result[col_name] = ta.sma(result[column], length=period)
    return result


def add_all_indicators(
    df: pd.DataFrame,
    macd_fast: int = 12,
    macd_slow: int = 26,
    macd_signal: int = 9,
    rsi_period: int = 9,
    rsi_signal: int = 14,
    ema_periods: list[int] = [9, 20],
    atr_period: int = 14,
    volume_ma_period: int = 20,
    prefix: str = ""
) -> pd.DataFrame:
    """添加所有常用指标（UI 展示用）

    Args:
        df: OHLCV DataFrame
        macd_fast/slow/signal: MACD 参数（默认 12,26,9 用于 UI）
        rsi_period: RSI 周期
        ema_periods: EMA 周期列表
        atr_period: ATR 周期
        volume_ma_period: 成交量均线周期
        prefix: 列名前缀

    Returns:
        添加所有指标后的 DataFrame
    """
    result = df.copy()

    # 价格指标
    result = add_macd(result, fast=macd_fast, slow=macd_slow, signal=macd_signal, prefix=prefix)
    result = add_rsi(result, period=rsi_period, signal_period=rsi_signal, prefix=prefix)
    result = add_ema(result, periods=ema_periods, prefix=prefix)
    result = add_atr(result, period=atr_period, prefix=prefix)

    # 成交量指标
    vol_sma_col = f"{prefix}VOL_SMA_{volume_ma_period}"
    result[vol_sma_col] = ta.sma(result['volume'], length=volume_ma_period)

    return result


def add_4in1_indicators(
    df: pd.DataFrame,
    macd_fast: int = 8,
    macd_slow: int = 17,
    macd_signal: int = 5,
    rsi_period: int = 9,
    atr_period: int = 14,
    atr_ma_period: int = 20,
    volume_ma_period: int = 20,
    ema_periods: list[int] = [9, 20],
) -> pd.DataFrame:
    """添加 4in1 策略所需的指标

    Args:
        df: OHLCV DataFrame
        macd_fast/slow/signal: 4in1 策略 MACD 参数（默认 8,17,5）
        rsi_period: RSI 周期（默认 9）
        atr_period: ATR 周期（默认 14）
        atr_ma_period: ATR 的 SMA 周期（默认 20）
        volume_ma_period: 成交量均线周期（默认 20）
        ema_periods: EMA 周期列表（默认 [9, 20]）

    Returns:
        添加 4in1 所需指标后的 DataFrame
    """
    result = df.copy()

    # MACD (4in1 参数)
    result = add_macd(result, fast=macd_fast, slow=macd_slow, signal=macd_signal, prefix="4in1_")

    # RSI
    result = add_rsi(result, period=rsi_period, prefix="4in1_")

    # ATR + ATR_MA
    result = add_atr(result, period=atr_period, prefix="4in1_")
    atr_col = f"4in1_ATR_{atr_period}"
    result[f"4in1_ATR_MA_{atr_ma_period}"] = ta.sma(result[atr_col], length=atr_ma_period)

    # 成交量 MA
    result[f"4in1_VOL_MA_{volume_ma_period}"] = ta.sma(result['volume'], length=volume_ma_period)

    # EMA (用于 trend 分数)
    result = add_ema(result, periods=ema_periods, prefix="4in1_")

    return result
