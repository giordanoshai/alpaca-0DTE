"""
策略 H：EMA9/21 裸交叉 + spread@K 确认闸（2026-07-11 定稿链路）

链路（混淆矩阵验证定稿，证据见 step7_trendscore_filter/筛选逻辑/2_闸门混淆矩阵验证 与 6_压榨）：
  1. EMA(fast) 上/下穿 EMA(slow) 产生裸交叉（交叉根须在 RTH 内）；
  2. 等 confirm_bars(K) 根，在确认根收盘检查
       spread = 方向 × (EMA快 − EMA慢) / ATR@交叉根 >= spread_threshold
     被反向叉回或没拉开 → 静默丢弃（这正是被杀掉的噪音）；拉开 → 发信号；
  3. 同向再响升级：距上一次本引擎信号同向且 <= recross_escalate_min 分钟
     → confidence='high'（前端响铃+通知）——实测为全场质量最高子集
       （第2响比第1响 Def2 好点率 +2.5~+7.4pp，四标的全同向）；
     首响 confidence='low'（徽章+边框闪烁，无声）。

与旧版的区别（2026-07-10/11 验证结论）：
  - 斜率闸/量能闸废除：杀好杀噪同比例（误杀 96% 好点零精度增益），纯随机砍量；
  - long_active/short_active 同向去重状态机废除：同向再响是质量最高的提醒，
    去重会精准删掉最好的那批——改为升级呈现；
  - 不做 flip 静音：反向响质量只比均值低 1~3pp，静音=随机砍量。

档位（gear）：watcher_manager 按 ticker 从 user_watchlist.gear 解析并覆盖
confirm_bars/spread_threshold（映射 app/config.py STRATEGY_H_GEARS）。

注：指标在完整 ETH 连续序列上计算（吸收开盘跳空），只对 RTH 交叉发信号；
确认根允许落在 16:00 之后（收盘前最后 K 分钟的交叉自然衰减）。
"""

from typing import Optional
import pandas as pd

from .base import BaseSignalEngine, SignalEvent, _atr

_RTH_LO, _RTH_HI = 570, 960  # 09:30–16:00 ET（分钟数）


class SignalEngineH(BaseSignalEngine):
    """策略 H：EMA9/21 裸交叉 + spread@K 确认闸 + 同向再响升级"""

    def __init__(self, params: Optional[dict] = None):
        p = params or {}
        self._ema_fast: int = p.get('ema_fast', 9)
        self._ema_slow: int = p.get('ema_slow', 21)
        self._atr_period: int = p.get('atr_period', 14)
        # 0 = 即时档：交叉当根就提醒，spread 改作「升级」条件（见 _process_instant）
        self._confirm_bars: int = max(0, int(p.get('confirm_bars', 3)))
        self._spread_threshold: float = float(p.get('spread_threshold', 0.15))
        self._recross_min: int = int(p.get('recross_escalate_min', 45))
        self._rth_only: bool = bool(p.get('rth_only', True))
        self._gear: str = p.get('gear', '') or f"spread{self._confirm_bars}_{self._spread_threshold}"

        # 状态：重复调用去重 + 同向再响追踪
        self._last_bar_ts = None          # 已处理的最后一根 bar 时间（防同 bar 重复评估）
        self._last_emit_ts = None         # 上一次发出信号的 bar 时间
        self._last_emit_dir: Optional[str] = None
        # 即时档专用：等待「拉开」升级的那个交叉；被反向交叉覆盖即作废
        self._pending: Optional[dict] = None

    @property
    def name(self) -> str:
        return "H"

    def reset(self) -> None:
        self._last_bar_ts = None
        self._last_emit_ts = None
        self._last_emit_dir = None
        self._pending = None

    def set_spread_threshold(self, value: float) -> None:
        """运行时热更新确认阈值（TODO P2.5 全局系数滑杆），不重置同向再响状态机。"""
        self._spread_threshold = float(value)

    def process(
        self,
        df_1m: pd.DataFrame,
        df_5m: pd.DataFrame,
        current_price: float,
        regime_score: float,
        gex_levels: Optional[dict] = None,
    ) -> Optional[SignalEvent]:

        K = self._confirm_bars
        min_len = max(self._ema_slow, self._atr_period) * 3 + K + 2
        if len(df_1m) < min_len:
            return None

        now_ts = df_1m.index[-1]
        if self._last_bar_ts is not None and now_ts == self._last_bar_ts:
            return None  # 同一根 bar 重复调用，只评估一次
        self._last_bar_ts = now_ts

        close = df_1m['close']
        fast = close.ewm(span=self._ema_fast, adjust=False).mean()
        slow = close.ewm(span=self._ema_slow, adjust=False).mean()
        sep = fast - slow

        if K == 0:
            return self._instant(df_1m, sep, regime_score)

        # 候选交叉根 = 恰好 K 根之前那根（每个交叉只会在它 K 根大时被评估一次）
        i = len(df_1m) - 1 - K
        if i < 1:
            return None
        s_i = float(sep.iloc[i])
        s_prev = float(sep.iloc[i - 1])
        golden = s_i > 0 and s_prev <= 0
        death = s_i < 0 and s_prev >= 0
        if not (golden or death):
            return None
        direction = "bull" if golden else "bear"
        sign = 1.0 if golden else -1.0

        cross_ts = df_1m.index[i]
        if self._rth_only and not self._in_rth(cross_ts):
            return None

        atr_cross = float(_atr(df_1m, self._atr_period).iloc[i])
        if not atr_cross > 1e-9:
            return None

        spread_now = sign * float(sep.iloc[-1]) / atr_cross
        if spread_now < self._spread_threshold:
            return None  # 没拉开/被反向叉回 → 噪音，静默丢弃

        # ── 同向再响升级 ────────────────────────────────────────────────────
        recross = False
        if (self._last_emit_dir == direction and self._last_emit_ts is not None):
            try:
                gap_min = (now_ts - self._last_emit_ts).total_seconds() / 60.0
                same_day = now_ts.date() == self._last_emit_ts.date()
                recross = same_day and 0 < gap_min <= self._recross_min
            except Exception:
                recross = False

        self._last_emit_ts = now_ts
        self._last_emit_dir = direction

        if direction == "bull":
            signal_type = "h_spread_bull2" if recross else "h_spread_bull"
            label = "H二次确认金叉" if recross else "H确认金叉"
        else:
            signal_type = "h_spread_bear2" if recross else "h_spread_bear"
            label = "H二次确认死叉" if recross else "H确认死叉"

        return SignalEvent(
            strategy="H",
            signal_type=signal_type,
            direction=direction,
            confidence="high" if recross else "low",
            label=label,
            metadata={
                "gear":         self._gear,
                "confirm_bars": K,
                "spread_thr":   self._spread_threshold,
                "spread_now":   round(spread_now, 3),
                "atr_cross":    round(atr_cross, 5),
                "cross_ts":     str(cross_ts),
                "recross":      recross,
                "close":        round(float(close.iloc[-1]), 4),
                "regime_score": round(regime_score, 2),
            },
        )

    # ── 即时档（confirm_bars=0）─────────────────────────────────────────────
    # 与老档位的区别只有一条：spread 阈值从「过滤」变成「升级」。
    #   交叉当根 → 立即发 low（无声徽章），一个交叉都不漏；
    #   之后拉开到阈值 → 补发一次 high（响铃+推送），每个交叉只升级一次；
    #   反向交叉 / 跨日 → 待升级作废。
    # 注意「同向再响升级」在本档下天然失效（裸交叉里金叉死叉必然交替，实测 0 次触发），
    # 响铃改由「拉开」触发，这是 2026-09-05 换 3 只标的时用户拍板的取舍。
    def _instant(self, df, sep, regime_score: float) -> Optional[SignalEvent]:
        i = len(df) - 1
        if i < 1:
            return None
        now_ts = df.index[i]
        if self._rth_only and not self._in_rth(now_ts):
            return None

        s_i, s_prev = float(sep.iloc[i]), float(sep.iloc[i - 1])
        golden = s_i > 0 and s_prev <= 0
        death = s_i < 0 and s_prev >= 0

        if golden or death:
            self._pending = None          # 反向交叉让上一个待升级作废
            atr_cross = float(_atr(df, self._atr_period).iloc[i])
            if not atr_cross > 1e-9:
                return None
            direction = "bull" if golden else "bear"
            sign = 1.0 if golden else -1.0
            self._pending = {"dir": direction, "sign": sign, "atr": atr_cross,
                             "ts": now_ts, "upgraded": False}
            self._last_emit_ts, self._last_emit_dir = now_ts, direction
            return self._instant_event(df, direction, False, sign * s_i / atr_cross,
                                       atr_cross, now_ts, regime_score)

        p = self._pending
        if not p or p["upgraded"]:
            return None
        try:
            if p["ts"].date() != now_ts.date():
                self._pending = None      # 跨日不再补发
                return None
        except Exception:
            pass
        spread_now = p["sign"] * s_i / p["atr"]
        if spread_now < self._spread_threshold:
            return None
        p["upgraded"] = True
        self._last_emit_ts, self._last_emit_dir = now_ts, p["dir"]
        return self._instant_event(df, p["dir"], True, spread_now, p["atr"],
                                   p["ts"], regime_score)

    def _instant_event(self, df, direction: str, escalated: bool, spread_now: float,
                       atr_cross: float, cross_ts, regime_score: float) -> SignalEvent:
        if direction == "bull":
            signal_type = "h_spread_bull2" if escalated else "h_spread_bull"
            label = "H金叉已拉开" if escalated else "H金叉"
        else:
            signal_type = "h_spread_bear2" if escalated else "h_spread_bear"
            label = "H死叉已拉开" if escalated else "H死叉"
        return SignalEvent(
            strategy="H",
            signal_type=signal_type,
            direction=direction,
            confidence="high" if escalated else "low",
            label=label,
            metadata={
                "gear":         self._gear,
                "confirm_bars": 0,
                "spread_thr":   self._spread_threshold,
                "spread_now":   round(spread_now, 3),
                "atr_cross":    round(atr_cross, 5),
                "cross_ts":     str(cross_ts),
                "recross":      False,      # 即时档不走同向再响，升级由「拉开」触发
                "escalated":    escalated,
                "close":        round(float(df['close'].iloc[-1]), 4),
                "regime_score": round(regime_score, 2),
            },
        )

    @staticmethod
    def _in_rth(ts) -> bool:
        try:
            t = ts.tz_convert('America/New_York') if ts.tzinfo else ts
            m = t.hour * 60 + t.minute
            return _RTH_LO <= m < _RTH_HI
        except Exception:
            return True  # 时区异常时不拦截（宁多勿漏）
