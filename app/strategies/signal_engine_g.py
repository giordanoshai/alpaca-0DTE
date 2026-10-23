"""
策略 G：EMA快速穿越 + VWAP + SMA趋势过滤

入场逻辑（1m bar）：
  bull: EMA(entry_short) 上穿 EMA(entry_long)，且 close > VWAP 且 close > SMA(sma_period)
  bear: EMA(entry_short) 下穿 EMA(entry_long)，且 close < VWAP 且 close < SMA(sma_period)

出场逻辑（持仓状态下）：
  多头退出: EMA(exit_small) 上穿 EMA(exit_long)（出场快线反向穿越）
  空头退出: EMA(exit_small) 下穿 EMA(exit_long)

VWAP 每日重置，使用当日盘中数据计算。
"""

from typing import Optional
import pandas as pd

from .base import BaseSignalEngine, SignalEvent


class SignalEngineG(BaseSignalEngine):
    """策略 G：EMA穿越 + VWAP + SMA过滤状态机"""

    def __init__(self, params: Optional[dict] = None):
        p = params or {}
        self._ema_entry_short: int = p.get('ema_entry_short', 1)
        self._ema_entry_long:  int = p.get('ema_entry_long',  3)
        self._sma_period:      int = p.get('sma_period',      63)
        self._ema_exit_small:  int = p.get('ema_exit_small',  3)
        self._ema_exit_long:   int = p.get('ema_exit_long',   7)
        self._cooldown_bars:   int = p.get('cooldown_bars',   3)

        self._long_active:  bool = False
        self._short_active: bool = False
        self._bar_idx:      int  = 0
        self._last_signal_bar: int = -999

        # 上一根的 EMA 值（用于检测穿越）
        self._prev_entry_short: Optional[float] = None
        self._prev_entry_long:  Optional[float] = None
        self._prev_exit_small:  Optional[float] = None
        self._prev_exit_long:   Optional[float] = None

    @property
    def name(self) -> str:
        return "G"

    def reset(self) -> None:
        self._long_active   = False
        self._short_active  = False
        self._bar_idx       = 0
        self._last_signal_bar = -999
        self._prev_entry_short = None
        self._prev_entry_long  = None
        self._prev_exit_small  = None
        self._prev_exit_long   = None

    def _calc_vwap(self, df: pd.DataFrame) -> float:
        """计算当日 VWAP（每日重置）"""
        try:
            today = df.index[-1].date()
            today_mask = pd.Series(df.index).apply(lambda x: x.date() == today).values
            today_df = df[today_mask]
            if len(today_df) == 0:
                today_df = df
            hlc3 = (today_df['high'] + today_df['low'] + today_df['close']) / 3
            vol  = today_df['volume'].replace(0, 1e-9)
            vwap = (hlc3 * vol).cumsum() / vol.cumsum()
            return float(vwap.iloc[-1])
        except Exception:
            return float(df['close'].iloc[-1])

    def process(
        self,
        df_1m: pd.DataFrame,
        df_5m: pd.DataFrame,
        current_price: float,
        regime_score: float,
        gex_levels: Optional[dict] = None,
    ) -> Optional[SignalEvent]:

        min_len = max(self._sma_period, self._ema_entry_long,
                      self._ema_exit_long) + 2
        if len(df_1m) < min_len:
            return None

        current_bar = self._bar_idx
        self._bar_idx += 1

        close = df_1m['close']

        # ── 计算指标 ────────────────────────────────────────────────────────
        entry_short_ser = close.ewm(span=self._ema_entry_short, adjust=False).mean()
        entry_long_ser  = close.ewm(span=self._ema_entry_long,  adjust=False).mean()
        sma_long_ser    = close.rolling(self._sma_period).mean()
        exit_small_ser  = close.ewm(span=self._ema_exit_small,  adjust=False).mean()
        exit_long_ser   = close.ewm(span=self._ema_exit_long,   adjust=False).mean()

        cur_entry_short = float(entry_short_ser.iloc[-1])
        cur_entry_long  = float(entry_long_ser.iloc[-1])
        cur_sma_long    = float(sma_long_ser.iloc[-1])
        cur_exit_small  = float(exit_small_ser.iloc[-1])
        cur_exit_long   = float(exit_long_ser.iloc[-1])
        cur_close       = float(close.iloc[-1])
        cur_vwap        = self._calc_vwap(df_1m)

        prev_es  = self._prev_entry_short
        prev_el  = self._prev_entry_long
        prev_xs  = self._prev_exit_small
        prev_xl  = self._prev_exit_long

        result: Optional[SignalEvent] = None

        if prev_es is not None and prev_el is not None:
            in_cooldown = (current_bar - self._last_signal_bar) < self._cooldown_bars

            # ── 入场检测 ────────────────────────────────────────────────────
            golden = prev_es <= prev_el and cur_entry_short > cur_entry_long
            death  = prev_es >= prev_el and cur_entry_short < cur_entry_long

            if golden and not self._long_active and not in_cooldown:
                if cur_close > cur_vwap and cur_close > cur_sma_long:
                    if self._short_active:
                        self._short_active = False
                    self._long_active = True
                    self._last_signal_bar = current_bar
                    result = self._make_signal(
                        "bull", "ema_entry_bull", "high",
                        cur_close, cur_vwap, cur_sma_long,
                        cur_entry_short, cur_entry_long, regime_score,
                    )

            elif death and not self._short_active and not in_cooldown:
                if cur_close < cur_vwap and cur_close < cur_sma_long:
                    if self._long_active:
                        self._long_active = False
                    self._short_active = True
                    self._last_signal_bar = current_bar
                    result = self._make_signal(
                        "bear", "ema_entry_bear", "high",
                        cur_close, cur_vwap, cur_sma_long,
                        cur_entry_short, cur_entry_long, regime_score,
                    )

            # ── 出场检测（仅在持仓时触发，低置信度提示） ────────────────────
            if result is None and prev_xs is not None and prev_xl is not None:
                exit_cross_up   = prev_xs <= prev_xl and cur_exit_small > cur_exit_long
                exit_cross_down = prev_xs >= prev_xl and cur_exit_small < cur_exit_long

                if exit_cross_up and self._long_active and not in_cooldown:
                    self._long_active = False
                    self._last_signal_bar = current_bar
                    result = self._make_signal(
                        "bear", "ema_exit_long", "low",
                        cur_close, cur_vwap, cur_sma_long,
                        cur_exit_small, cur_exit_long, regime_score,
                    )

                elif exit_cross_down and self._short_active and not in_cooldown:
                    self._short_active = False
                    self._last_signal_bar = current_bar
                    result = self._make_signal(
                        "bull", "ema_exit_short", "low",
                        cur_close, cur_vwap, cur_sma_long,
                        cur_exit_small, cur_exit_long, regime_score,
                    )

        self._prev_entry_short = cur_entry_short
        self._prev_entry_long  = cur_entry_long
        self._prev_exit_small  = cur_exit_small
        self._prev_exit_long   = cur_exit_long
        return result

    def _make_signal(
        self,
        direction: str,
        signal_type: str,
        confidence: str,
        close: float,
        vwap: float,
        sma: float,
        ema_fast: float,
        ema_slow: float,
        regime_score: float,
    ) -> SignalEvent:
        label_map = {
            "ema_entry_bull": "EMA金叉入场",
            "ema_entry_bear": "EMA死叉入场",
            "ema_exit_long":  "EMA出场(多)",
            "ema_exit_short": "EMA出场(空)",
        }
        return SignalEvent(
            strategy="G",
            signal_type=signal_type,
            direction=direction,
            confidence=confidence,
            label=label_map.get(signal_type, signal_type),
            metadata={
                "close":        round(close, 4),
                "vwap":         round(vwap, 4),
                "sma":          round(sma, 4),
                "ema_fast":     round(ema_fast, 4),
                "ema_slow":     round(ema_slow, 4),
                "regime_score": round(regime_score, 2),
            },
        )
