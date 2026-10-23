"""
策略 B：事件驱动（价格行为 + 成交量）

信号类型：
    contraction    — 缩量整理开始（仅 UI 标注，无声音）
    breakout       — 放量突破整理区间
    pullback       — 趋势回踩关键位反弹
    exhaustion     — 动量衰竭预警（出场辅助）
"""

from typing import Optional
import pandas as pd
import numpy as np

from .base import BaseSignalEngine, SignalEvent

# ── 列名 fallback 顺序 ────────────────────────────────────────────────────────
_VOL_MA   = ('4in1_VOL_MA_20', 'VOL_SMA_20', 'vol_ma')
_ATR      = ('4in1_ATR_14',    'ATR_14',      'atr')
_ATR_MA   = ('4in1_ATR_MA_20', 'atr_ma')
_RSI_1M   = ('4in1_RSI_9',     'RSI_9',       'rsi')
_EMA20_5M = ('4in1_EMA_20',    'EMA_20',      'ema20')
_RSI_5M   = ('4in1_RSI_9',     'RSI_9',       'rsi')


def _col(df: pd.DataFrame, names: tuple, default=np.nan):
    """按优先级读取第一个存在的列，缺失返回 default。"""
    for n in names:
        if n in df.columns:
            return df[n]
    return pd.Series([default] * len(df), index=df.index)


class SignalEngineB(BaseSignalEngine):
    """策略 B：事件驱动信号引擎"""

    def __init__(self, params: Optional[dict] = None):
        p = params or {}
        self._lookback:         int   = p.get('contraction_lookback', 8)
        self._vol_low_ratio:    float = p.get('vol_low_ratio', 0.7)
        self._vol_low_pct:      float = p.get('vol_low_pct', 0.6)
        self._max_range_pct:    float = p.get('max_range_pct', 0.5)
        self._atr_ratio:        float = p.get('atr_contraction_ratio', 0.8)
        self._cont_score_min:   int   = p.get('contraction_score_min', 2)
        self._vol_break_ratio:  float = p.get('vol_breakout_ratio', 2.0)
        self._cont_memory:      int   = p.get('contraction_memory', 5)
        self._trend_inv_pct:    float = p.get('trend_invalidate_pct', 0.005)
        self._key_tolerance:    float = p.get('key_level_tolerance', 0.001)
        self._gex_prox_pct:     float = p.get('gex_proximity_pct', 0.003)
        self._cooldown_bars:    int   = p.get('cooldown_bars', 5)

        self._bar_idx:              int   = 0
        self._contraction_detected: bool  = False
        self._contraction_start:    int   = -9999
        self._range_high:           float = 0.0
        self._range_low:            float = 0.0

        self._breakout_dir:   Optional[str]   = None   # "bull" | "bear"
        self._breakout_bar:   int             = -9999
        self._breakout_price: float           = 0.0

        self._last_alert_bar:  int = -9999
        self._last_alert_type: str = ""

    @property
    def name(self) -> str:
        return "B"

    def reset(self) -> None:
        self._bar_idx = 0
        self._contraction_detected = False
        self._contraction_start = -9999
        self._range_high = 0.0
        self._range_low = 0.0
        self._breakout_dir = None
        self._breakout_bar = -9999
        self._breakout_price = 0.0
        self._last_alert_bar = -9999
        self._last_alert_type = ""

    # ── 公共入口 ──────────────────────────────────────────────────────────────

    def process(
        self,
        df_1m: pd.DataFrame,
        df_5m: pd.DataFrame,
        current_price: float,
        regime_score: float,
        gex_levels: Optional[dict] = None,
    ) -> Optional[SignalEvent]:

        if len(df_1m) < self._lookback + 2:
            return None

        current_bar = self._bar_idx
        self._bar_idx += 1

        # 风控：极端波动屏蔽所有信号
        atr    = float(_col(df_1m, _ATR).iloc[-1])
        atr_ma = float(_col(df_1m, _ATR_MA, atr + 1e-9).iloc[-1])
        if atr_ma > 0 and atr / atr_ma > 2.0:
            self._contraction_detected = False
            return None

        # 基础数据
        close  = float(df_1m['close'].iloc[-1])
        open_  = float(df_1m['open'].iloc[-1])
        high   = float(df_1m['high'].iloc[-1])
        vol    = float(df_1m['volume'].iloc[-1])
        vol_1  = float(df_1m['volume'].iloc[-2]) if len(df_1m) > 1 else vol
        vol_2  = float(df_1m['volume'].iloc[-3]) if len(df_1m) > 2 else vol_1

        vol_ma_s = _col(df_1m, _VOL_MA)
        vol_ma   = float(vol_ma_s.iloc[-1]) if not np.isnan(vol_ma_s.iloc[-1]) else (
            float(df_1m['volume'].tail(20).mean()))

        # ── 1. 缩量整理检测 ────────────────────────────────────────────────
        is_contracting = self._check_contraction(df_1m, vol_ma, atr, atr_ma)
        prev_contraction = self._contraction_detected

        if is_contracting:
            lb = self._lookback
            # 区间用前 lb 根 bar（不含当前 bar）：
            # 当前 bar 的 low <= close（OHLC 约束），若 range_low 包含当前 bar 的 low，
            # 则 close < range_low 永远不成立，导致 bear breakout 无法触发。
            self._range_high = float(df_1m['high'].iloc[-(lb + 1):-1].max())
            self._range_low  = float(df_1m['low'].iloc[-(lb + 1):-1].min())
            if not prev_contraction:
                self._contraction_start = current_bar
        self._contraction_detected = is_contracting

        # ── 2. 优先级：突破 > 衰竭 > 回踩 > 缩量（仅状态变化时通知）─────────

        result = (
            self._check_breakout(current_bar, close, open_, vol, vol_ma, regime_score) or
            self._check_exhaustion(current_bar, df_1m, close, high, vol, vol_1, vol_2,
                                   df_5m, regime_score, current_price, gex_levels) or
            self._check_pullback(current_bar, close, open_, vol, vol_1, vol_ma, df_5m) or
            self._check_contraction_event(current_bar, is_contracting, prev_contraction)
        )

        return result

    # ── 私有检测方法 ──────────────────────────────────────────────────────────

    def _check_contraction(
        self, df_1m: pd.DataFrame, vol_ma: float, atr: float, atr_ma: float
    ) -> bool:
        lb = self._lookback
        window = df_1m.iloc[-lb:]

        # 条件 1：成交量持续低迷
        if vol_ma > 0:
            low_vol = (window['volume'] < vol_ma * self._vol_low_ratio).sum()
            vol_contracting = low_vol >= lb * self._vol_low_pct
        else:
            vol_contracting = False

        # 条件 2：价格区间收窄
        rng_high = float(window['high'].max())
        rng_low  = float(window['low'].min())
        if rng_low > 0:
            range_pct = (rng_high - rng_low) / rng_low * 100
            price_tight = range_pct < self._max_range_pct
        else:
            price_tight = False

        # 条件 3：ATR 收缩
        atr_contracting = (atr_ma > 0) and (atr < atr_ma * self._atr_ratio)

        score = int(vol_contracting) + int(price_tight) + int(atr_contracting)
        return score >= self._cont_score_min

    def _has_contraction_context(self, current_bar: int) -> bool:
        return self._contraction_detected or (
            current_bar - self._contraction_start <= self._cont_memory
        )

    def _cooldown_ok(self, current_bar: int) -> bool:
        return (current_bar - self._last_alert_bar) >= self._cooldown_bars

    def _check_contraction_event(
        self, current_bar: int, is_contracting: bool, was_contracting: bool
    ) -> Optional[SignalEvent]:
        """只在进入缩量状态时通知一次（状态转变）"""
        if is_contracting and not was_contracting:
            return SignalEvent(
                strategy="B",
                signal_type="contraction",
                direction="bull",   # 缩量无方向，用 bull 作占位
                confidence="low",
                label="蓄力整理",
                metadata={
                    "range_high": round(self._range_high, 2),
                    "range_low":  round(self._range_low, 2),
                },
            )
        return None

    def _check_breakout(
        self,
        current_bar: int,
        close: float, open_: float,
        vol: float, vol_ma: float,
        regime_score: float,
    ) -> Optional[SignalEvent]:

        if not self._has_contraction_context(current_bar):
            return None
        if not self._cooldown_ok(current_bar):
            return None
        if vol_ma <= 0:
            return None

        volume_surge = vol > vol_ma * self._vol_break_ratio
        if not volume_surge:
            return None

        break_up   = close > self._range_high and close > open_
        break_down = close < self._range_low  and close < open_

        if not break_up and not break_down:
            return None

        direction  = "bull" if break_up else "bear"
        confidence = "high" if (
            (direction == "bull" and regime_score > 0) or
            (direction == "bear" and regime_score < 0)
        ) else "low"

        self._breakout_dir   = direction
        self._breakout_bar   = current_bar
        self._breakout_price = close
        self._contraction_detected = False   # 清除整理状态

        self._last_alert_bar  = current_bar
        self._last_alert_type = "breakout"

        label = f"{'向上' if direction == 'bull' else '向下'}放量突破"
        return SignalEvent(
            strategy="B",
            signal_type="breakout",
            direction=direction,
            confidence=confidence,
            label=label,
            metadata={
                "breakout_price":  round(close, 2),
                "range_high":      round(self._range_high, 2),
                "range_low":       round(self._range_low, 2),
                "vol_ratio":       round(vol / vol_ma, 2),
                "regime_score":    round(regime_score, 2),
            },
        )

    def _check_pullback(
        self,
        current_bar: int,
        close: float, open_: float,
        vol: float, vol_1: float,
        vol_ma: float,
        df_5m: pd.DataFrame,
    ) -> Optional[SignalEvent]:

        if self._breakout_dir is None:
            return None
        if not self._cooldown_ok(current_bar):
            return None

        direction = self._breakout_dir
        bp = self._breakout_price

        # 趋势仍有效
        if direction == "bull":
            trend_valid = close > bp * (1 - self._trend_inv_pct)
        else:
            trend_valid = close < bp * (1 + self._trend_inv_pct)
        if not trend_valid:
            self._breakout_dir = None   # 趋势失效，重置
            return None

        # 接近关键位
        ema20_5m = float(_col(df_5m, _EMA20_5M, 0.0).iloc[-1]) if not df_5m.empty else 0.0
        tol = self._key_tolerance

        if direction == "bull":
            near_ema = ema20_5m > 0 and close <= ema20_5m * (1 + tol)
            near_bp  = close <= bp * (1 + tol)
            near_key = near_ema or near_bp
            pullback_low_vol = vol_ma > 0 and vol < vol_ma * 0.8
            bounce = close > open_ and vol > vol_1
        else:
            near_ema = ema20_5m > 0 and close >= ema20_5m * (1 - tol)
            near_bp  = close >= bp * (1 - tol)
            near_key = near_ema or near_bp
            pullback_low_vol = vol_ma > 0 and vol < vol_ma * 0.8
            bounce = close < open_ and vol > vol_1

        if not (near_key and pullback_low_vol and bounce):
            return None

        self._last_alert_bar  = current_bar
        self._last_alert_type = "pullback"

        label = "多头回踩反弹" if direction == "bull" else "空头回踩反弹"
        return SignalEvent(
            strategy="B",
            signal_type="pullback",
            direction=direction,
            confidence="high",
            label=label,
            metadata={
                "close":         round(close, 2),
                "breakout_price": round(bp, 2),
                "near_ema20_5m": near_ema,
            },
        )

    def _check_exhaustion(
        self,
        current_bar: int,
        df_1m: pd.DataFrame,
        close: float, high: float,
        vol: float, vol_1: float, vol_2: float,
        df_5m: pd.DataFrame,
        regime_score: float,
        current_price: float,
        gex_levels: Optional[dict],
    ) -> Optional[SignalEvent]:

        if self._breakout_dir is None:
            return None
        if not self._cooldown_ok(current_bar):
            return None
        if self._last_alert_type == "exhaustion":
            # 避免连续触发，需要至少一次其他类型的信号间隔
            if (current_bar - self._last_alert_bar) < self._cooldown_bars * 2:
                return None

        direction = self._breakout_dir
        lookback_n = 10

        rsi_1m = float(_col(df_1m, _RSI_1M, 50).iloc[-1])
        rsi_5m = float(_col(df_5m, _RSI_5M, 50).iloc[-1]) if not df_5m.empty else 50.0

        count = 0

        if direction == "bull":
            # 条件 1：新高但量能递减
            recent_high = float(df_1m['high'].iloc[-lookback_n:].max()) if len(df_1m) >= lookback_n else high
            new_extreme = high >= recent_high
            vol_declining = vol < vol_1 and vol_1 < vol_2
            if new_extreme and vol_declining:
                count += 1

            # 条件 2：接近 GEX 墙
            if gex_levels:
                gex_strike = gex_levels.get('max_pos_gex_strike')
                if gex_strike and current_price >= gex_strike * (1 - self._gex_prox_pct):
                    count += 1

            # 条件 3：RSI 极端
            if rsi_1m > 75 or rsi_5m > 70:
                count += 1

            direction_label = "多头"
        else:
            low = float(df_1m['low'].iloc[-1])
            recent_low = float(df_1m['low'].iloc[-lookback_n:].min()) if len(df_1m) >= lookback_n else low
            new_extreme = low <= recent_low
            vol_declining = vol < vol_1 and vol_1 < vol_2
            if new_extreme and vol_declining:
                count += 1

            if gex_levels:
                gex_strike = gex_levels.get('max_neg_gex_strike')
                if gex_strike and current_price <= gex_strike * (1 + self._gex_prox_pct):
                    count += 1

            if rsi_1m < 25 or rsi_5m < 30:
                count += 1

            direction_label = "空头"

        if count < 2:
            return None

        self._last_alert_bar  = current_bar
        self._last_alert_type = "exhaustion"

        return SignalEvent(
            strategy="B",
            signal_type="exhaustion",
            direction=direction,
            confidence="high",
            label=f"{direction_label}动量衰竭",
            metadata={
                "conditions_met": count,
                "rsi_1m":  round(rsi_1m, 1),
                "rsi_5m":  round(rsi_5m, 1),
                "regime_score": round(regime_score, 2),
            },
        )
