"""
策略 A：V4 交叉 + Regime 追赶确认

每个 ticker 创建独立实例（持有状态机）。
df_1m 需先经过 V4IndicatorService.calculate_indicators_for_1m() 处理，
包含 v4_fast / v4_slow 列。
"""

from dataclasses import dataclass
from typing import Optional
import pandas as pd

from .base import BaseSignalEngine, SignalEvent


@dataclass
class _Pending:
    direction: str              # "golden" | "death"
    bar_idx: int                # 交叉发生时的内部 bar 编号
    regime_at_cross: float      # 交叉瞬间的 regime_score
    confirmed: bool = False
    tentative_reverse: Optional[int] = None   # 疑似反转的 bar 编号
    gap_peak: float = 0.0
    in_pullback: bool = False
    last_alert_bar: int = -9999


class SignalEngineA(BaseSignalEngine):
    """策略 A：V4 交叉 + Regime 追赶确认状态机"""

    def __init__(self, params: Optional[dict] = None):
        p = params or {}
        self._pending_window: int = p.get('pending_window', 8)
        self._cooldown_bars: int = p.get('cooldown_bars', 5)
        self._regime_high_conf: float = p.get('regime_high_confidence_threshold', 4)
        self._pullback_enabled: bool = p.get('pullback_enabled', True)
        self._pullback_gap_pct: float = p.get('pullback_gap_pct', 0.20)
        self._pullback_reopen_pct: float = p.get('pullback_reopen_pct', 0.40)
        self._min_gap_peak: float = p.get('min_gap_peak', 0.5)
        self._debounce_bars: int = p.get('debounce_bars', 2)

        self._pending: Optional[_Pending] = None
        self._bar_idx: int = 0
        self._prev_fast: Optional[float] = None
        self._prev_slow: Optional[float] = None
        self._prev_regime: Optional[float] = None
        self._prev2_regime: Optional[float] = None  # 两根前 regime，用于连续方向确认

    @property
    def name(self) -> str:
        return "A"

    def reset(self) -> None:
        self._pending = None
        self._bar_idx = 0
        self._prev_fast = None
        self._prev_slow = None
        self._prev_regime = None
        self._prev2_regime = None

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

        # V4 评分
        if 'v4_fast' not in df_1m.columns or 'v4_slow' not in df_1m.columns:
            return None
        v4_fast = float(df_1m['v4_fast'].iloc[-1])
        v4_slow = float(df_1m['v4_slow'].iloc[-1])

        # 风控：极端波动屏蔽所有信号
        atr = float(df_1m.get('4in1_ATR_14', df_1m.get('ATR_14', df_1m.get('atr', pd.Series([0])))).iloc[-1])
        atr_ma = float(df_1m.get('4in1_ATR_MA_20', df_1m.get('atr_ma', pd.Series([atr + 1e-9]))).iloc[-1])
        if atr_ma > 0 and atr / atr_ma > 2.0:
            self._prev2_regime = self._prev_regime
            self._prev_fast = v4_fast
            self._prev_slow = v4_slow
            self._prev_regime = regime_score
            return None

        prev_fast = self._prev_fast
        prev_slow = self._prev_slow
        prev_regime = self._prev_regime
        result: Optional[SignalEvent] = None

        # ── 步骤 1：检测 V4 交叉 ─────────────────────────────────────────
        if prev_fast is not None and prev_slow is not None:
            golden = prev_fast <= prev_slow and v4_fast > v4_slow
            death = prev_fast >= prev_slow and v4_fast < v4_slow

            if golden or death:
                new_dir = "golden" if golden else "death"
                if self._pending is None:
                    self._pending = _Pending(
                        direction=new_dir,
                        bar_idx=current_bar,
                        regime_at_cross=regime_score,
                    )
                elif self._pending.direction != new_dir:
                    # 方向相反 → 标记疑似反转，不立即替换
                    if self._pending.tentative_reverse is None:
                        self._pending.tentative_reverse = current_bar

        # ── 步骤 2：防抖（反向交叉需持续 debounce_bars 根才确认）──────────
        if self._pending and self._pending.tentative_reverse is not None:
            tent_dir = "golden" if self._pending.direction == "death" else "death"
            still_reversed = (
                (tent_dir == "golden" and v4_fast > v4_slow) or
                (tent_dir == "death" and v4_fast < v4_slow)
            )
            back_to_original = (
                (self._pending.direction == "golden" and v4_fast > v4_slow) or
                (self._pending.direction == "death" and v4_fast < v4_slow)
            )
            bars_since_tent = current_bar - self._pending.tentative_reverse

            if still_reversed and bars_since_tent >= self._debounce_bars - 1:
                # 确认反转 → 替换 pending
                self._pending = _Pending(
                    direction=tent_dir,
                    bar_idx=current_bar,
                    regime_at_cross=regime_score,
                )
            elif back_to_original:
                # 假反转 → 取消
                self._pending.tentative_reverse = None

        # ── 步骤 3：Regime 追赶检查 ──────────────────────────────────────
        if (self._pending and
                not self._pending.confirmed and
                self._pending.tentative_reverse is None):

            bars_since_cross = current_bar - self._pending.bar_idx

            if bars_since_cross <= self._pending_window:
                cooldown_ok = (current_bar - self._pending.last_alert_bar) >= self._cooldown_bars

                if prev_regime is not None and cooldown_ok:
                    if self._pending.direction == "golden":
                        catching_up = regime_score > self._pending.regime_at_cross
                        # 要求连续 2 根都在上升（prev2 → prev → current 均向上）
                        still_moving = (regime_score > prev_regime and
                                        (self._prev2_regime is None or prev_regime >= self._prev2_regime))
                    else:
                        catching_up = regime_score < self._pending.regime_at_cross
                        # 要求连续 2 根都在下降（prev2 → prev → current 均向下）
                        still_moving = (regime_score < prev_regime and
                                        (self._prev2_regime is None or prev_regime <= self._prev2_regime))

                    if catching_up and still_moving:
                        direction = "bull" if self._pending.direction == "golden" else "bear"
                        confidence = "high" if abs(regime_score) >= self._regime_high_conf else "low"
                        cross_word = "金叉" if direction == "bull" else "死叉"
                        label = (f"{cross_word}确认" if bars_since_cross == 0
                                 else f"{cross_word}延迟确认（{bars_since_cross}根）")

                        result = SignalEvent(
                            strategy="A",
                            signal_type="cross_confirm",
                            direction=direction,
                            confidence=confidence,
                            label=label,
                            metadata={
                                "bars_delayed": bars_since_cross,
                                "regime_score": round(regime_score, 2),
                                "v4_fast": round(v4_fast, 3),
                                "v4_slow": round(v4_slow, 3),
                            },
                        )
                        self._pending.confirmed = True
                        self._pending.last_alert_bar = current_bar
            else:
                # 超时过期，标记已处理（回踩检测仍可继续）
                self._pending.confirmed = True

        # ── 步骤 4：趋势回踩检测 ─────────────────────────────────────────
        if (result is None and
                self._pending and
                self._pending.confirmed and
                self._pullback_enabled and
                self._pending.tentative_reverse is None):

            cooldown_ok = (current_bar - self._pending.last_alert_bar) >= self._cooldown_bars
            gap = v4_fast - v4_slow
            abs_gap = abs(gap)
            direction_ok = (gap > 0) if self._pending.direction == "golden" else (gap < 0)

            if direction_ok:
                self._pending.gap_peak = max(self._pending.gap_peak, abs_gap)

                if self._pending.gap_peak > self._min_gap_peak:
                    ratio = abs_gap / self._pending.gap_peak

                    if ratio <= self._pullback_gap_pct:
                        self._pending.in_pullback = True

                    if self._pending.in_pullback and ratio >= self._pullback_reopen_pct and cooldown_ok:
                        regime_ok = (regime_score > 0) if self._pending.direction == "golden" else (regime_score < 0)
                        if regime_ok:
                            direction = "bull" if self._pending.direction == "golden" else "bear"
                            label = "多头回踩反弹" if direction == "bull" else "空头回踩反弹"
                            confidence = "high" if abs(regime_score) >= self._regime_high_conf else "low"

                            result = SignalEvent(
                                strategy="A",
                                signal_type="pullback_bounce",
                                direction=direction,
                                confidence=confidence,
                                label=label,
                                metadata={
                                    "gap_ratio": round(ratio, 3),
                                    "gap_peak": round(self._pending.gap_peak, 3),
                                    "regime_score": round(regime_score, 2),
                                    "v4_fast": round(v4_fast, 3),
                                    "v4_slow": round(v4_slow, 3),
                                },
                            )
                            self._pending.in_pullback = False
                            self._pending.gap_peak = abs_gap   # 从新起点重新计
                            self._pending.last_alert_bar = current_bar

        # 更新上一根 bar 的值
        self._prev2_regime = self._prev_regime
        self._prev_fast = v4_fast
        self._prev_slow = v4_slow
        self._prev_regime = regime_score

        return result
