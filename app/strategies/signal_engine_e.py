"""
策略 E：V4 交叉后宽松锚定破位验证

对比策略 D（High/Low 锚定）：
- 锚定价改为交叉根 Close，降低入场门槛
- 允许小幅回踩：窗口内 min_close（bull）/ max_close（bear）在 tolerance 以内不丢弃
- 一旦回踩超出 tolerance → 立即丢弃 pending（不等窗口到期）
- 触发：close 突破 anchor 且窗口内未出现超过 tolerance 的反向深跌
"""

from dataclasses import dataclass
from typing import Optional
import pandas as pd

from .base import BaseSignalEngine, SignalEvent


@dataclass
class _Pending:
    direction: str        # "golden" | "death"
    bar_idx: int          # 交叉发生时的内部 bar 编号
    anchor_price: float   # 交叉根 Close（宽松锚定）
    extreme_close: float  # bull: 窗口内最低 close；bear: 窗口内最高 close


class SignalEngineE(BaseSignalEngine):
    """策略 E：V4 交叉 + 宽松 Close 锚定 + 允许回踩状态机"""

    def __init__(self, params: Optional[dict] = None):
        p = params or {}
        self._pending_window: int      = p.get('pending_window',     3)
        self._regime_high_conf: float  = p.get('regime_high_conf',   2.0)
        self._atr_ratio_limit: float   = p.get('atr_ratio_limit',    999)
        self._tolerance: float         = p.get('pullback_tolerance',  0.001)

        self._pending: Optional[_Pending] = None
        self._bar_idx: int = 0
        self._prev_fast: Optional[float] = None
        self._prev_slow: Optional[float] = None
        self._atr_col: Optional[str] = None

    @property
    def name(self) -> str:
        return "E"

    def reset(self) -> None:
        self._pending = None
        self._bar_idx = 0
        self._prev_fast = None
        self._prev_slow = None
        self._atr_col = None

    def _get_atr_last(self, df: pd.DataFrame) -> float:
        if self._atr_col and self._atr_col in df.columns:
            return float(df[self._atr_col].iloc[-1])
        for col in ('4in1_ATR_14', 'ATR_14', 'atr'):
            if col in df.columns:
                self._atr_col = col
                return float(df[col].iloc[-1])
        return 0.0

    def _get_atr_ma_last(self, df: pd.DataFrame, default_atr: float) -> float:
        for col in ('4in1_ATR_MA_20', 'atr_ma'):
            if col in df.columns:
                return float(df[col].iloc[-1])
        return default_atr + 1e-9

    def process(
        self,
        df_1m: pd.DataFrame,
        df_5m: pd.DataFrame,
        current_price: float,
        regime_score: float,
        gex_levels: Optional[dict] = None,
    ) -> Optional[SignalEvent]:

        if len(df_1m) < 2:
            return None

        current_bar = self._bar_idx
        self._bar_idx += 1

        if 'v4_fast' not in df_1m.columns or 'v4_slow' not in df_1m.columns:
            return None

        v4_fast = float(df_1m['v4_fast'].iloc[-1])
        v4_slow = float(df_1m['v4_slow'].iloc[-1])
        close   = float(df_1m['close'].iloc[-1])

        atr    = self._get_atr_last(df_1m)
        atr_ma = self._get_atr_ma_last(df_1m, atr)
        if atr_ma > 0 and atr / atr_ma > self._atr_ratio_limit:
            self._prev_fast = v4_fast
            self._prev_slow = v4_slow
            return None

        prev_fast = self._prev_fast
        prev_slow = self._prev_slow
        result: Optional[SignalEvent] = None

        # ── 步骤 1：检测 V4 交叉，锚定 Close ────────────────────────────────
        if prev_fast is not None and prev_slow is not None:
            golden = prev_fast <= prev_slow and v4_fast > v4_slow
            death  = prev_fast >= prev_slow and v4_fast < v4_slow

            if golden or death:
                self._pending = _Pending(
                    direction="golden" if golden else "death",
                    bar_idx=current_bar,
                    anchor_price=close,
                    extreme_close=close,
                )

        # ── 步骤 2：宽松破位 + 回踩容忍验证 ─────────────────────────────────
        if self._pending is not None:
            bars_since_cross = current_bar - self._pending.bar_idx

            if bars_since_cross == 0:
                pass  # 交叉当根不验证

            elif bars_since_cross <= self._pending_window:
                anchor = self._pending.anchor_price
                tol    = self._tolerance

                if self._pending.direction == "golden":
                    self._pending.extreme_close = min(self._pending.extreme_close, close)
                    if self._pending.extreme_close < anchor * (1.0 - tol):
                        # 回踩超出容忍 → 立即丢弃
                        self._pending = None
                    elif close > anchor:
                        result = self._make_signal("bull", bars_since_cross, anchor, close,
                                                   v4_fast, v4_slow, regime_score)
                        self._pending = None
                else:
                    self._pending.extreme_close = max(self._pending.extreme_close, close)
                    if self._pending.extreme_close > anchor * (1.0 + tol):
                        self._pending = None
                    elif close < anchor:
                        result = self._make_signal("bear", bars_since_cross, anchor, close,
                                                   v4_fast, v4_slow, regime_score)
                        self._pending = None

                # 窗口最后一根仍未触发 → 超时丢弃
                if self._pending is not None and bars_since_cross == self._pending_window:
                    self._pending = None

            else:
                self._pending = None

        self._prev_fast = v4_fast
        self._prev_slow = v4_slow
        return result

    def _make_signal(
        self,
        direction: str,
        bars_delayed: int,
        anchor: float,
        close: float,
        v4_fast: float,
        v4_slow: float,
        regime_score: float,
    ) -> SignalEvent:
        confidence = "high"
        cross_word  = "宽松金叉突破" if direction == "bull" else "宽松死叉破位"
        signal_type = "close_breakout" if direction == "bull" else "close_breakdown"
        return SignalEvent(
            strategy="E",
            signal_type=signal_type,
            direction=direction,
            confidence=confidence,
            label=f"{cross_word} +{bars_delayed}",
            metadata={
                "bars_delayed": bars_delayed,
                "anchor_price": round(anchor, 4),
                "close":        round(close, 4),
                "regime_score": round(regime_score, 2),
                "v4_fast":      round(v4_fast, 3),
                "v4_slow":      round(v4_slow, 3),
            },
        )
