"""
策略 C2：EMA + MACD + RSI 三指标共振 V2.1（升级自策略C/V1）

V2.1 新增四个过滤器：
  F1 - Confluence Score 门槛（默认 0 = 禁用）
  F2 - 同方向 Time Cooldown（默认 30 bars）
  F3 - ATR Price Cooldown（默认 0.5 × ATR，0 = 禁用）
  F4 - Session Filter：跳过开盘前/收盘前 N 分钟（ET）

原始信号逻辑（buyRaw/sellRaw）与 C V1 完全一致。
"""

from typing import Optional
import numpy as np
import pandas as pd
import pytz

from .base import BaseSignalEngine, SignalEvent, _atr


_ET = pytz.timezone('America/New_York')


# ── 指标计算 ─────────────────────────────────────────────────────────────────

def _ema(series: pd.Series, n: int) -> pd.Series:
    return series.ewm(span=n, adjust=False).mean()


def _rsi(series: pd.Series, n: int) -> pd.Series:
    """Wilder 平滑 RSI，与 Pine ta.rsi 等价（alpha = 1/n）。"""
    delta = series.diff()
    gain  = delta.clip(lower=0)
    loss  = (-delta).clip(lower=0)
    avg_g = gain.ewm(alpha=1.0 / n, min_periods=n, adjust=False).mean()
    avg_l = loss.ewm(alpha=1.0 / n, min_periods=n, adjust=False).mean()
    rs    = avg_g / avg_l.replace(0, 1e-9)
    return 100.0 - (100.0 / (1.0 + rs))


def _macd(
    series: pd.Series, fast: int, slow: int, signal: int
) -> tuple[pd.Series, pd.Series, pd.Series]:
    ema_f     = _ema(series, fast)
    ema_s     = _ema(series, slow)
    macd_line = ema_f - ema_s
    sig_line  = _ema(macd_line, signal)
    return macd_line, sig_line, macd_line - sig_line


class SignalEngineC2(BaseSignalEngine):
    """策略 C2：EMA + MACD + RSI 三指标共振 V2.1。

    带状态机（与 Pine posState 一致）：进入 buy 后不再重复发 buy，直到 sell 出现。
    """

    def __init__(self, params: Optional[dict] = None):
        p = params or {}
        # ── Core 参数（与 C V1 一致）──────────────────────────────────────────
        self._ema_fast_len:    int   = p.get('ema_fast_len',    9)
        self._ema_slow_len:    int   = p.get('ema_slow_len',   20)
        self._macd_fast:       int   = p.get('macd_fast',      12)
        self._macd_slow:       int   = p.get('macd_slow',      26)
        self._macd_signal_len: int   = p.get('macd_signal_len', 9)
        self._rsi_len:         int   = p.get('rsi_len',        14)
        self._rsi_ema_len:     int   = p.get('rsi_ema_len',    14)
        self._rsi_level:       float = p.get('rsi_level',     50.0)
        self._min_bars: int = (
            max(self._ema_slow_len, self._macd_slow, self._rsi_len)
            + self._rsi_ema_len + 4
        )

        # ── V2.1 过滤器参数 ────────────────────────────────────────────────────
        self._min_confluence:     int   = p.get('min_confluence',     0)    # F1
        self._cooldown_bars:      int   = p.get('cooldown_bars',     30)    # F2
        self._price_cooldown_atr: float = p.get('price_cooldown_atr', 0.5)  # F3
        self._skip_open_minutes:  int   = p.get('skip_open_minutes',  15)   # F4a
        self._skip_close_minutes: int   = p.get('skip_close_minutes',  0)   # F4b
        self._enforce_rth:        bool  = p.get('enforce_rth',       True)  # F4: RTH 硬过滤（24/7 场景可关）

        # ── 状态机 ────────────────────────────────────────────────────────────
        self._pos_state: int = 0

        # ── F2 状态：bar 计数器 ────────────────────────────────────────────────
        self._bar_count:    int = 0
        self._last_buy_bar:  int = -10000
        self._last_sell_bar: int = -10000

        # ── F3 状态：上次信号价格 ──────────────────────────────────────────────
        self._last_buy_price:  float = float('nan')
        self._last_sell_price: float = float('nan')

        # ── 历史回放标志（首次 process 时自动同步 pos_state）────────────────────
        self._state_initialized: bool = False
        self._last_date = None

    @property
    def name(self) -> str:
        return 'C2'

    def reset(self) -> None:
        self._pos_state     = 0
        self._bar_count     = 0
        self._last_buy_bar  = -10000
        self._last_sell_bar = -10000
        self._last_buy_price  = float('nan')
        self._last_sell_price = float('nan')
        self._state_initialized = False
        self._last_date = None

    def _replay_history(self, df_1m: pd.DataFrame) -> None:
        """回放历史 bar，同步 pos_state 与 Pine 的 var posState 一致。

        只跑 raw 信号逻辑（无 F2/F3/F4 过滤），与 Pine 状态机行为保持一致。
        扫描 df_1m 的所有历史 bar（不含最后一根，最后一根由当次 process 处理）。
        """
        close = df_1m['close']
        n = len(close)
        if n < self._min_bars + 1:
            return

        ema_fast_s      = _ema(close, self._ema_fast_len)
        ema_slow_s      = _ema(close, self._ema_slow_len)
        ml_s, sl_s, h_s = _macd(close, self._macd_fast, self._macd_slow, self._macd_signal_len)
        rsi_s           = _rsi(close, self._rsi_len)
        rsi_ema_s       = _ema(rsi_s, self._rsi_ema_len)

        cur_day = None
        for i in range(self._min_bars, n - 1):
            ts = df_1m.index[i]
            try:
                bar_date = ts.date() if hasattr(ts, 'date') else pd.Timestamp(ts).date()
                if cur_day is None or cur_day != bar_date:
                    cur_day = bar_date
                    self._pos_state = 0
                    self._last_buy_bar = -10000
                    self._last_sell_bar = -10000
                    self._last_buy_price = float('nan')
                    self._last_sell_price = float('nan')
                    self._last_date = bar_date
            except Exception:
                pass

            ef,   es   = float(ema_fast_s.iloc[i]),   float(ema_slow_s.iloc[i])
            ef_p, es_p = float(ema_fast_s.iloc[i-1]), float(ema_slow_s.iloc[i-1])
            ml,   sl   = float(ml_s.iloc[i]),          float(sl_s.iloc[i])
            ml_p, sl_p = float(ml_s.iloc[i-1]),        float(sl_s.iloc[i-1])
            ri,   re   = float(rsi_s.iloc[i]),         float(rsi_ema_s.iloc[i])
            ri_p, re_p = float(rsi_s.iloc[i-1]),       float(rsi_ema_s.iloc[i-1])

            if any(np.isnan(v) for v in (ef, es, ef_p, es_p, ml, sl, ml_p, sl_p, ri, re, ri_p, re_p)):
                self._bar_count += 1
                continue

            ema_bull_cross  = ef_p <= es_p and ef > es
            ema_bear_cross  = ef_p >= es_p and ef < es
            macd_bull_cross = ml_p <= sl_p and ml > sl
            macd_bear_cross = ml_p >= sl_p and ml < sl
            rsi_bull_cross  = ri_p <= re_p and ri > re
            rsi_bear_cross  = ri_p >= re_p and ri < re

            ema_bull  = ef > es;  ema_bear  = ef < es
            macd_bull = ml > sl;  macd_bear = ml < sl
            rsi_bull  = ri > re;  rsi_bear  = ri < re

            buy_raw = (
                (ema_bull_cross  and macd_bull and rsi_bull) or
                (macd_bull_cross and ema_bull  and rsi_bull) or
                (rsi_bull_cross  and ema_bull  and macd_bull)
            )
            sell_raw = (
                (ema_bear_cross  and macd_bear and rsi_bear) or
                (macd_bear_cross and ema_bear  and rsi_bear) or
                (rsi_bear_cross  and ema_bear  and macd_bear)
            )

            if buy_raw and self._pos_state != 1:
                self._pos_state      = 1
                self._last_buy_bar   = self._bar_count
                self._last_buy_price = float(close.iloc[i])
            elif sell_raw and self._pos_state != -1:
                self._pos_state       = -1
                self._last_sell_bar   = self._bar_count
                self._last_sell_price = float(close.iloc[i])

            self._bar_count += 1

    # ── 公共入口 ──────────────────────────────────────────────────────────────

    def process(
        self,
        df_1m: pd.DataFrame,
        df_5m: pd.DataFrame,
        current_price: float,
        regime_score: float,
        gex_levels: Optional[dict] = None,
    ) -> Optional[SignalEvent]:

        if not self._state_initialized:
            self._replay_history(df_1m)
            self._state_initialized = True

        # 日切换 → 重置状态机和 cooldown 状态
        try:
            last_ts = df_1m.index[-1]
            cur_date = last_ts.date() if hasattr(last_ts, 'date') else pd.Timestamp(last_ts).date()
            if self._last_date is None or self._last_date != cur_date:
                self._pos_state = 0
                self._last_buy_bar = -10000
                self._last_sell_bar = -10000
                self._last_buy_price = float('nan')
                self._last_sell_price = float('nan')
                self._last_date = cur_date
        except Exception:
            pass

        self._bar_count += 1

        if len(df_1m) < self._min_bars:
            return None

        # 极端波动屏蔽：独立计算 ATR 达到与 Grid search 完美一致
        atr_series = _atr(df_1m, 14)
        atr_ma_series = atr_series.rolling(20).mean()
        
        atr = float(atr_series.iloc[-1])
        atr_ma = float(atr_ma_series.iloc[-1])
        if atr_ma > 0 and atr / atr_ma > 2.0:
            return None

        # ── F4: Session Filter ────────────────────────────────────────────────
        # RTH 硬过滤始终执行（与 Pine inRTH 一致），skip_open/close 可选
        try:
            last_ts = df_1m.index[-1]
            if hasattr(last_ts, 'tzinfo') and last_ts.tzinfo is not None:
                et_ts = last_ts.tz_convert(_ET)
            else:
                et_ts = _ET.localize(pd.Timestamp(last_ts).to_pydatetime())
            et_min = et_ts.hour * 60 + et_ts.minute

            if self._enforce_rth:
                in_rth = (9 * 60 + 30) <= et_min < (16 * 60)
                if not in_rth:
                    return None

            if self._skip_open_minutes > 0 and et_min < (9 * 60 + 30 + self._skip_open_minutes):
                return None
            if self._skip_close_minutes > 0 and et_min >= (16 * 60 - self._skip_close_minutes):
                return None
        except Exception:
            pass  # 时间解析失败时不屏蔽

        close = df_1m['close']

        # ── 指标计算 ──────────────────────────────────────────────────────────
        ema_fast_s      = _ema(close, self._ema_fast_len)
        ema_slow_s      = _ema(close, self._ema_slow_len)
        ml_s, sl_s, h_s = _macd(close, self._macd_fast, self._macd_slow, self._macd_signal_len)
        rsi_s           = _rsi(close, self._rsi_len)
        rsi_ema_s       = _ema(rsi_s, self._rsi_ema_len)

        ef,   es   = float(ema_fast_s.iloc[-1]),  float(ema_slow_s.iloc[-1])
        ef_p, es_p = float(ema_fast_s.iloc[-2]),  float(ema_slow_s.iloc[-2])
        ml,   sl   = float(ml_s.iloc[-1]),         float(sl_s.iloc[-1])
        ml_p, sl_p = float(ml_s.iloc[-2]),         float(sl_s.iloc[-2])
        ri,   re   = float(rsi_s.iloc[-1]),        float(rsi_ema_s.iloc[-1])
        ri_p, re_p = float(rsi_s.iloc[-2]),        float(rsi_ema_s.iloc[-2])
        h          = float(h_s.iloc[-1])

        if any(np.isnan(v) for v in (ef, es, ef_p, es_p,
                                     ml, sl, ml_p, sl_p,
                                     ri, re, ri_p, re_p)):
            return None

        # ── Cross 检测 ────────────────────────────────────────────────────────
        ema_bull_cross  = ef_p <= es_p and ef > es
        ema_bear_cross  = ef_p >= es_p and ef < es
        macd_bull_cross = ml_p <= sl_p and ml > sl
        macd_bear_cross = ml_p >= sl_p and ml < sl
        rsi_bull_cross  = ri_p <= re_p and ri > re
        rsi_bear_cross  = ri_p >= re_p and ri < re

        ema_bull  = ef > es;  ema_bear  = ef < es
        macd_bull = ml > sl;  macd_bear = ml < sl
        rsi_bull  = ri > re;  rsi_bear  = ri < re

        # ── Raw 信号（与 C V1 完全一致）──────────────────────────────────────
        buy_raw = (
            (ema_bull_cross  and macd_bull and rsi_bull) or
            (macd_bull_cross and ema_bull  and rsi_bull) or
            (rsi_bull_cross  and ema_bull  and macd_bull)
        )
        sell_raw = (
            (ema_bear_cross  and macd_bear and rsi_bear) or
            (macd_bear_cross and ema_bear  and rsi_bear) or
            (rsi_bear_cross  and ema_bear  and macd_bear)
        )

        if not buy_raw and not sell_raw:
            return None

        # ── 状态机 ────────────────────────────────────────────────────────────
        buy_state_ok  = self._pos_state != 1
        sell_state_ok = self._pos_state != -1

        # 状态机和 cooldown 计时由 Raw 信号驱动（与 _replay_history 及 Pine 一致）
        if buy_raw and buy_state_ok:
            self._pos_state = 1
            self._last_buy_bar = self._bar_count
            self._last_buy_price = current_price
        elif sell_raw and sell_state_ok:
            self._pos_state = -1
            self._last_sell_bar = self._bar_count
            self._last_sell_price = current_price

        # ── Confluence Score (0–6) ────────────────────────────────────────────
        lv = self._rsi_level
        bull_score = (int(ema_bull) + int(macd_bull) + int(rsi_bull)
                      + int(ri > lv) + int(re > lv) + int(h > 0))
        bear_score = (int(ema_bear) + int(macd_bear) + int(rsi_bear)
                      + int(ri < lv) + int(re < lv) + int(h < 0))

        # ── F1: Confluence Score 门槛 ─────────────────────────────────────────
        buy_score_ok  = bull_score >= self._min_confluence
        sell_score_ok = bear_score >= self._min_confluence

        # ── F2: 同方向 Time Cooldown ──────────────────────────────────────────
        buy_time_cd_ok  = (self._bar_count - self._last_buy_bar)  >= self._cooldown_bars
        sell_time_cd_ok = (self._bar_count - self._last_sell_bar) >= self._cooldown_bars

        # ── F3: ATR Price Cooldown ────────────────────────────────────────────
        if self._price_cooldown_atr > 0 and atr > 0:
            buy_price_cd_ok  = (np.isnan(self._last_buy_price)  or
                                abs(current_price - self._last_buy_price)  > self._price_cooldown_atr * atr)
            sell_price_cd_ok = (np.isnan(self._last_sell_price) or
                                abs(current_price - self._last_sell_price) > self._price_cooldown_atr * atr)
        else:
            buy_price_cd_ok  = True
            sell_price_cd_ok = True

        # ── 最终信号 ──────────────────────────────────────────────────────────
        buy_signal  = (buy_raw  and buy_state_ok  and buy_score_ok
                       and buy_time_cd_ok  and buy_price_cd_ok)
        sell_signal = (sell_raw and sell_state_ok and sell_score_ok
                       and sell_time_cd_ok and sell_price_cd_ok)

        if not buy_signal and not sell_signal:
            return None

        direction = 'bull' if buy_signal else 'bear'
        score     = bull_score if buy_signal else bear_score



        # ── Signal Type（优先级：EMA > MACD > RSI）────────────────────────────
        if direction == 'bull':
            if   ema_bull_cross  and macd_bull and rsi_bull: sig_type = 'ema_cross'
            elif macd_bull_cross and ema_bull  and rsi_bull: sig_type = 'macd_cross'
            else:                                             sig_type = 'rsi_cross'
        else:
            if   ema_bear_cross  and macd_bear and rsi_bear: sig_type = 'ema_cross'
            elif macd_bear_cross and ema_bear  and rsi_bear: sig_type = 'macd_cross'
            else:                                             sig_type = 'rsi_cross'

        return SignalEvent(
            strategy='C2',
            signal_type=sig_type,
            direction=direction,
            confidence='high' if score >= 5 else 'low',
            label='买入' if direction == 'bull' else '卖出',
            metadata={
                'confluence':   score,
                'ema_fast':     round(ef, 4),
                'ema_slow':     round(es, 4),
                'macd_line':    round(ml, 4),
                'rsi':          round(ri,  1),
                'rsi_ema':      round(re,  1),
            },
        )
