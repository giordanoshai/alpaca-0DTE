"""
策略 C3：EMA + MACD + RSI 原始触发 + 5min 噪音否决器
保留 1min 上的好信号，尽量只过滤掉 5min 环境下明显属于 chop / 噪音的信号。
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

class SignalEngineC3(BaseSignalEngine):
    """策略 C3：EMA + MACD + RSI 原始触发 + 5min 噪音否决器。
    带状态机：进入 buy 后不再重复发 buy，直到 sell 出现。
    """

    def __init__(self, params: Optional[dict] = None):
        p = params or {}
        # ── Core 参数 ──────────────────────────────────────────────────────────
        self._ema_fast_len:    int   = p.get('ema_fast_len',    5)
        self._ema_slow_len:    int   = p.get('ema_slow_len',   13)
        self._macd_fast:       int   = p.get('macd_fast',      6)
        self._macd_slow:       int   = p.get('macd_slow',      13)
        self._macd_signal_len: int   = p.get('macd_signal_len', 5)
        self._rsi_len:         int   = p.get('rsi_len',        10)
        self._rsi_ema_len:     int   = p.get('rsi_ema_len',    6)
        self._rsi_level:       float = p.get('rsi_level',     50.0)
        self._min_bars: int = (
            max(self._ema_slow_len, self._macd_slow, self._rsi_len)
            + self._rsi_ema_len + 4
        )

        # ── C3 特有参数 ────────────────────────────────────────────────────────
        self._skip_open_minutes:  int   = p.get('skip_open_minutes',  5)
        self._skip_close_minutes: int   = p.get('skip_close_minutes',  0)
        self._enforce_rth:        bool  = p.get('enforce_rth',       True)

        self._enable_5m_noise_veto: bool = p.get('enable_5m_noise_veto', True)
        self._chop_ema_spread_atr_threshold: float = p.get('chop_ema_spread_atr_threshold', 0.25)
        self._chop_macd_hist_atr_threshold: float = p.get('chop_macd_hist_atr_threshold', 0.05)
        self._breakout_vol_ratio_threshold: float = p.get('breakout_vol_ratio_threshold', 1.2)
        self._min_5m_bars: int = p.get('min_5m_bars', 30)

        # ── 状态机 ────────────────────────────────────────────────────────────
        self._pos_state: int = 0
        self._bar_count: int = 0

        # ── 历史回放标志 ──────────────────────────────────────────────────────
        self._state_initialized: bool = False
        self._last_date = None

    @property
    def name(self) -> str:
        return 'C3'

    def reset(self) -> None:
        self._pos_state     = 0
        self._bar_count     = 0
        self._state_initialized = False
        self._last_date = None

    def _replay_history(self, df_1m: pd.DataFrame) -> None:
        """回放历史 bar，同步 pos_state 与 Pine 的 var posState 一致。
        只跑 raw 信号逻辑。
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
                self._pos_state = 1
            elif sell_raw and self._pos_state != -1:
                self._pos_state = -1

            self._bar_count += 1

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

        # 日切换 → 重置状态机
        try:
            last_ts = df_1m.index[-1]
            cur_date = last_ts.date() if hasattr(last_ts, 'date') else pd.Timestamp(last_ts).date()
            if self._last_date is None or self._last_date != cur_date:
                self._pos_state = 0
                self._last_date = cur_date
        except Exception:
            pass

        self._bar_count += 1

        if len(df_1m) < self._min_bars:
            return None

        # 极端波动屏蔽
        atr_series = _atr(df_1m, 14)
        atr_ma_series = atr_series.rolling(20).mean()
        
        atr = float(atr_series.iloc[-1])
        atr_ma = float(atr_ma_series.iloc[-1])
        if atr_ma > 0 and atr / atr_ma > 2.0:
            return None

        # ── Session Filter ────────────────────────────────────────────────────
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
            pass

        close = df_1m['close']

        # ── 1min 指标计算 ──────────────────────────────────────────────────────────
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

        if any(np.isnan(v) for v in (ef, es, ef_p, es_p, ml, sl, ml_p, sl_p, ri, re, ri_p, re_p)):
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

        # ── Raw 信号 ──────────────────────────────────────────────────────────
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

        # 状态机更新由 Raw 信号驱动（与 _replay_history 及 Pine 无过滤状态机保持一致）
        if buy_raw and buy_state_ok:
            self._pos_state = 1
        elif sell_raw and sell_state_ok:
            self._pos_state = -1

        # 计算 1m Confluence Score 仅用于 metadata 显示和 confidence
        lv = self._rsi_level
        bull_score = (int(ema_bull) + int(macd_bull) + int(rsi_bull)
                      + int(ri > lv) + int(re > lv) + int(h > 0))
        bear_score = (int(ema_bear) + int(macd_bear) + int(rsi_bear)
                      + int(ri < lv) + int(re < lv) + int(h < 0))

        # ── 5min 噪音否决器 (Noise Veto) ─────────────────────────────────────────
        buy_noise_reject = False
        sell_noise_reject = False

        filter_5m_enabled = False
        filter_5m_chop = False
        filter_5m_trend_bull = False
        filter_5m_trend_bear = False
        filter_5m_bull_rebound = False
        filter_5m_bear_rebound = False
        filter_5m_bull_breakout = False
        filter_5m_bear_breakdown = False

        ema_spread_5m_atr = 0.0
        macd_hist_5m_atr = 0.0
        rsi9_5m_val = 0.0
        rsi_slope_5m = 0.0
        macd_hist_slope_5m = 0.0
        price_to_ema20_5m_atr = 0.0
        vol_ratio_5m_val = 1.0

        if self._enable_5m_noise_veto:
            if len(df_5m) >= self._min_5m_bars:
                # 默认使用倒数第二根（已完成）的 5m bar 来计算
                df_5m_closed = df_5m.iloc[:-1]
                if len(df_5m_closed) >= 20: # 确保足以计算均线
                    close_5m = df_5m_closed['close']
                    vol_5m   = df_5m_closed['volume']
                    
                    ema9_5m  = _ema(close_5m, 9)
                    ema20_5m = _ema(close_5m, 20)
                    _, _, macd_hist_5m = _macd(close_5m, 12, 26, 9)
                    rsi9_5m  = _rsi(close_5m, 9)
                    atr14_5m = _atr(df_5m_closed, 14)
                    volume_ma10_5m = vol_5m.rolling(10).mean()
                    
                    sma20_5m = close_5m.rolling(20).mean()
                    std20_5m = close_5m.rolling(20).std()
                    bb_lower_5m = sma20_5m - 2 * std20_5m

                    cur_ema9 = float(ema9_5m.iloc[-1])
                    cur_ema20 = float(ema20_5m.iloc[-1])
                    cur_macd_hist = float(macd_hist_5m.iloc[-1])
                    prev_macd_hist = float(macd_hist_5m.iloc[-2])
                    cur_rsi = float(rsi9_5m.iloc[-1])
                    prev_rsi = float(rsi9_5m.iloc[-2])
                    cur_atr = float(atr14_5m.iloc[-1])
                    cur_vol = float(vol_5m.iloc[-1])
                    cur_vol_ma = float(volume_ma10_5m.iloc[-1])
                    cur_bb_lower = float(bb_lower_5m.iloc[-1])

                    prev_5m_high = float(df_5m_closed['high'].iloc[-1])
                    prev_5m_low  = float(df_5m_closed['low'].iloc[-1])

                    if cur_atr > 0 and cur_vol_ma > 0 and not np.isnan(cur_macd_hist):
                        filter_5m_enabled = True

                        ema_spread_5m = cur_ema9 - cur_ema20
                        ema_spread_5m_atr = abs(ema_spread_5m) / cur_atr
                        price_to_ema20_5m_atr = (current_price - cur_ema20) / cur_atr
                        macd_hist_slope_5m = cur_macd_hist - prev_macd_hist
                        rsi_slope_5m = cur_rsi - prev_rsi
                        vol_ratio_5m_val = cur_vol / cur_vol_ma
                        macd_hist_5m_atr = abs(cur_macd_hist) / cur_atr
                        rsi9_5m_val = cur_rsi

                        # --- 严苛的高胜率过滤逻辑 (Strict Veto) ---
                        
                        # 做多放行条件：必须满足【MACD 柱 > 0 (右侧顺势确认)】 或 【当前价格跌穿 5m 布林带下轨 (左侧极限深V)】
                        is_bull_valid = (cur_macd_hist > 0) or (current_price < cur_bb_lower)
                        buy_noise_reject = not is_bull_valid
                        
                        # 做空放行条件：必须满足【5m EMA9 < EMA20 (均线空头确认)】 或 【当前价格 < 5m EMA20 且 5m RSI < 50】
                        is_bear_valid = (cur_ema9 < cur_ema20) or (current_price < cur_ema20 and cur_rsi < 50)
                        sell_noise_reject = not is_bear_valid
                        
                        # 兼容老状态字段（为了防报错，赋默认值）
                        filter_5m_trend_bull = is_bull_valid
                        filter_5m_trend_bear = is_bear_valid
                        filter_5m_bull_rebound = False
                        filter_5m_bear_rebound = False
                        filter_5m_bull_breakout = False
                        filter_5m_bear_breakdown = False
                        filter_5m_chop = buy_noise_reject and sell_noise_reject


        # ── 最终信号 ──────────────────────────────────────────────────────────
        buy_signal  = (buy_raw  and buy_state_ok  and not buy_noise_reject)
        sell_signal = (sell_raw and sell_state_ok and not sell_noise_reject)

        if not buy_signal and not sell_signal:
            return None

        direction = 'bull' if buy_signal else 'bear'
        score     = bull_score if buy_signal else bear_score



        # ── Signal Type ───────────────────────────────────────────────────────
        if direction == 'bull':
            if   ema_bull_cross  and macd_bull and rsi_bull: sig_type = 'ema_cross'
            elif macd_bull_cross and ema_bull  and rsi_bull: sig_type = 'macd_cross'
            else:                                             sig_type = 'rsi_cross'
        else:
            if   ema_bear_cross  and macd_bear and rsi_bear: sig_type = 'ema_cross'
            elif macd_bear_cross and ema_bear  and rsi_bear: sig_type = 'macd_cross'
            else:                                             sig_type = 'rsi_cross'

        metadata = {
            'confluence':   score,
            'ema_fast':     round(ef, 4),
            'ema_slow':     round(es, 4),
            'macd_line':    round(ml, 4),
            'rsi':          round(ri, 1),
            'rsi_ema':      round(re, 1),

            'filter_5m_enabled':        filter_5m_enabled,
            'filter_5m_chop':           bool(filter_5m_chop),
            'filter_5m_trend_bull':     bool(filter_5m_trend_bull),
            'filter_5m_trend_bear':     bool(filter_5m_trend_bear),
            'filter_5m_bull_rebound':   bool(filter_5m_bull_rebound),
            'filter_5m_bear_rebound':   bool(filter_5m_bear_rebound),
            'filter_5m_bull_breakout':  bool(filter_5m_bull_breakout),
            'filter_5m_bear_breakdown': bool(filter_5m_bear_breakdown),
        }

        if filter_5m_enabled:
            metadata.update({
                'ema_spread_5m_atr':     round(ema_spread_5m_atr, 4),
                'macd_hist_5m_atr':      round(macd_hist_5m_atr, 4),
                'rsi9_5m':               round(rsi9_5m_val, 1),
                'rsi_slope_5m':          round(rsi_slope_5m, 3),
                'macd_hist_slope_5m':    round(macd_hist_slope_5m, 5),
                'price_to_ema20_5m_atr': round(price_to_ema20_5m_atr, 4),
                'vol_ratio_5m':          round(vol_ratio_5m_val, 3),
            })
        else:
            if self._enable_5m_noise_veto:
                metadata['filter_5m_reason'] = 'insufficient_5m_bars'

        return SignalEvent(
            strategy='C3',
            signal_type=sig_type,
            direction=direction,
            confidence='high' if score >= 5 else 'low',
            label='买入' if direction == 'bull' else '卖出',
            metadata=metadata,
        )
