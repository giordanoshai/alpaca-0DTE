"""
策略 F：V4 gap 连续扩大动量验证

完全不看价格，仅观察 v4_fast - v4_slow 差值（gap）是否在交叉后持续单调扩大。
金叉后：任意连续 N 根 gap > 上一根 gap → bull 信号（多周期共振强化）
死叉后：任意连续 N 根 gap 持续向负方向扩大 → bear 信号
连续中断时重置计数器，继续在 max_window 内寻找下一个连续窗口。
"""

from dataclasses import dataclass
from typing import Optional
import pandas as pd

from .base import BaseSignalEngine, SignalEvent


@dataclass
class _Pending:
    direction: str       # "golden" | "death"
    bar_idx: int         # 交叉发生时的内部 bar 编号
    last_gap: float      # 上一根的 gap（初始为交叉根 gap，中断后更新为中断根 gap）
    consec_count: int    # 当前连续满足扩大条件的根数


class SignalEngineF(BaseSignalEngine):
    """策略 F：V4 gap 连续扩大状态机"""

    def __init__(self, params: Optional[dict] = None):
        p = params or {}
        self._pending_window: int      = p.get('pending_window',     2)
        self._max_window: int          = p.get('max_window',          8)
        self._regime_high_conf: float  = p.get('regime_high_conf',   2.0)
        self._atr_ratio_limit: float   = p.get('atr_ratio_limit',    999)
        self._gap_min_expansion: float = p.get('gap_min_expansion',   0.0)

        self._pending: Optional[_Pending] = None
        self._bar_idx: int = 0
        self._prev_fast: Optional[float] = None
        self._prev_slow: Optional[float] = None
        self._atr_col: Optional[str] = None

    @property
    def name(self) -> str:
        return "F"

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

        current_gap = v4_fast - v4_slow

        # ── 步骤 1：检测 V4 交叉，初始化 pending ────────────────────────────
        if prev_fast is not None and prev_slow is not None:
            golden = prev_fast <= prev_slow and v4_fast > v4_slow
            death  = prev_fast >= prev_slow and v4_fast < v4_slow

            if golden or death:
                self._pending = _Pending(
                    direction="golden" if golden else "death",
                    bar_idx=current_bar,
                    last_gap=current_gap,
                    consec_count=0,
                )

        # ── 步骤 2：gap 连续扩大检测 ─────────────────────────────────────────
        if self._pending is not None:
            bars_since_cross = current_bar - self._pending.bar_idx

            if bars_since_cross == 0:
                pass  # 交叉当根不计入

            elif bars_since_cross <= self._max_window:
                # 检测反向交叉，直接销毁 pending
                if (self._pending.direction == "golden" and v4_fast < v4_slow) or \
                   (self._pending.direction == "death" and v4_fast > v4_slow):
                    self._pending = None
                else:
                    last_gap = self._pending.last_gap
                    min_exp  = self._gap_min_expansion

                    if self._pending.direction == "golden":
                        expanding = (v4_fast > v4_slow) and (current_gap > last_gap + min_exp)
                    else:
                        expanding = (v4_fast < v4_slow) and (current_gap < last_gap - min_exp)

                    if expanding:
                        self._pending.consec_count += 1
                        self._pending.last_gap = current_gap
                        if self._pending.consec_count >= self._pending_window:
                            direction = "bull" if self._pending.direction == "golden" else "bear"
                            result = self._make_signal(direction, bars_since_cross,
                                                       current_gap, close, v4_fast, v4_slow, regime_score)
                            self._pending = None
                    else:
                        # 连续中断：重置计数，以当前 gap 作为新基准继续观察
                        self._pending.consec_count = 0
                        self._pending.last_gap = current_gap

                    # 最大观察窗口到期 → 丢弃
                    if self._pending is not None and bars_since_cross >= self._max_window:
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
        gap: float,
        close: float,
        v4_fast: float,
        v4_slow: float,
        regime_score: float,
    ) -> SignalEvent:
        confidence = "high"
        cross_word  = "V4 gap扩大" if direction == "bull" else "V4 gap压缩"
        signal_type = "gap_expansion" if direction == "bull" else "gap_compression"
        return SignalEvent(
            strategy="F",
            signal_type=signal_type,
            direction=direction,
            confidence=confidence,
            label=f"{cross_word} +{bars_delayed}",
            metadata={
                "bars_delayed": bars_delayed,
                "gap":          round(gap, 4),
                "close":        round(close, 4),
                "regime_score": round(regime_score, 2),
                "v4_fast":      round(v4_fast, 3),
                "v4_slow":      round(v4_slow, 3),
            },
        )
