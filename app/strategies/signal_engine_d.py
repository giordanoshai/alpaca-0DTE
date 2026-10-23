"""
策略 D：V4 交叉后价格破位验证

死叉时锁定该 K 线 Low 为锚定价；随后 n 根内若 Close < Low → 空头破位确认。
金叉时锁定该 K 线 High 为锚定价；随后 n 根内若 Close > High → 多头突破确认。
窗口内无破位则丢弃（假交叉噪音）。
"""

from dataclasses import dataclass
from typing import Optional
import pandas as pd

from .base import BaseSignalEngine, SignalEvent


@dataclass
class _Pending:
    direction: str       # "golden" | "death"
    bar_idx: int         # 交叉发生时的内部 bar 编号
    anchor_price: float  # 金叉→交叉根 High，死叉→交叉根 Low


class SignalEngineD(BaseSignalEngine):
    """策略 D：V4 交叉 + 随后价格破位验证状态机"""

    def __init__(self, params: Optional[dict] = None):
        p = params or {}
        self._pending_window: int     = p.get('pending_window',   2)
        self._regime_high_conf: float = p.get('regime_high_conf', 2.0)
        self._atr_ratio_limit: float  = p.get('atr_ratio_limit',  2.0)

        self._pending: Optional[_Pending] = None
        self._bar_idx: int = 0
        self._prev_fast: Optional[float] = None
        self._prev_slow: Optional[float] = None
        self._atr_col: Optional[str] = None  # 动态缓存字段名，提升实盘速度

    @property
    def name(self) -> str:
        return "D"

    def reset(self) -> None:
        self._pending = None
        self._bar_idx = 0
        self._prev_fast = None
        self._prev_slow = None
        self._atr_col = None

    def _get_atr_last(self, df: pd.DataFrame) -> float:
        """ATR 字段快速提取，首次运行动态探测并缓存列名"""
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
        high    = float(df_1m['high'].iloc[-1])
        low     = float(df_1m['low'].iloc[-1])

        # 风控：极端波动时屏蔽所有信号
        atr    = self._get_atr_last(df_1m)
        atr_ma = self._get_atr_ma_last(df_1m, atr)
        if atr_ma > 0 and atr / atr_ma > self._atr_ratio_limit:
            self._prev_fast = v4_fast
            self._prev_slow = v4_slow
            return None

        prev_fast = self._prev_fast
        prev_slow = self._prev_slow
        result: Optional[SignalEvent] = None

        # ── 步骤 1：检测 V4 交叉，更新 pending ──────────────────────────────
        if prev_fast is not None and prev_slow is not None:
            golden = prev_fast <= prev_slow and v4_fast > v4_slow
            death  = prev_fast >= prev_slow and v4_fast < v4_slow

            if golden or death:
                new_dir = "golden" if golden else "death"
                anchor  = high if golden else low
                # 新交叉（同向刷新 / 反向替换）均重建 pending
                self._pending = _Pending(
                    direction=new_dir,
                    bar_idx=current_bar,
                    anchor_price=anchor,
                )

        # ── 步骤 2：价格破位验证 ──────────────────────────────────────────────
        if self._pending is not None:
            bars_since_cross = current_bar - self._pending.bar_idx

            if bars_since_cross == 0:
                # 交叉当根不验证，等待下一根收盘
                pass

            elif bars_since_cross <= self._pending_window:
                if self._pending.direction == "golden":
                    triggered = close > self._pending.anchor_price
                else:
                    triggered = close < self._pending.anchor_price

                if triggered:
                    direction = "bull" if self._pending.direction == "golden" else "bear"
                    confidence = "high"
                    cross_word  = "金叉突破" if direction == "bull" else "死叉破位"
                    signal_type = "price_breakout" if direction == "bull" else "price_breakdown"

                    result = SignalEvent(
                        strategy="D",
                        signal_type=signal_type,
                        direction=direction,
                        confidence=confidence,
                        label=f"{cross_word} +{bars_since_cross}",
                        metadata={
                            "bars_delayed": bars_since_cross,
                            "anchor_price": round(self._pending.anchor_price, 4),
                            "close":        round(close, 4),
                            "regime_score": round(regime_score, 2),
                            "v4_fast":      round(v4_fast, 3),
                            "v4_slow":      round(v4_slow, 3),
                        },
                    )
                    self._pending = None  # 成功触发，清除状态

                elif bars_since_cross == self._pending_window:
                    # 最后一根窗口 bar 仍未破位 → 假信号，立即丢弃
                    self._pending = None

            else:
                # 安全兜底：超过窗口期直接清除
                self._pending = None

        self._prev_fast = v4_fast
        self._prev_slow = v4_slow
        return result
