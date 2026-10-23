"""
盯盘主循环模块
- 多 ticker 管理
- live stream 管理
- 策略调度
- WebSocket 推送
"""

import asyncio
import logging
import os
import time
from datetime import datetime, time as dtime
from decimal import Decimal
from typing import Optional, Callable, Awaitable
import pandas as pd
import pytz

from app.config import VWAP_SIGMA_ATR_FLOOR, env
from app.core.dailylogger import get_logger
from app.market_state import sigma_with_atr_floor

logger = get_logger("tt.watcher")
from app.watcher.quote_loops import QuoteLoopsMixin

# 常规交易时段（ET）。VWAP/σ 的锚定区间，左闭右开：09:30 计入、16:00 不计入。
RTH_START = dtime(9, 30)
RTH_END = dtime(16, 0)

# 最新一根 1m K 线比现在旧多少秒就算「数据落后」，落后即 fail-closed 不放行。
# K 线时间戳是**起始时间**，所以正常年龄本来就在 60~120 秒之间来回走 —— 阈值必须
# 高于两根 bar 的间隔，否则每分钟末尾都会闪一次误拦。180s 留一根 bar 的余量。
# 判据只有这一处：get_state 算完直接下发布尔值，market_context 与 feed_snapshot
# 只读结论，不各自再算一遍（本项目栽过「两套判据、卡片与按钮说两套话」）。
STATE_STALE_AFTER_S = 180.0

from app.core.bar_builder import BarBuilder, NTPTimeSync, resample_to_5m
from app.core.data_manager import DataManager
from app.services.liquidity import data_quality_ok
from app.strategies.base import StrategyResult, SignalEvent, BaseSignalEngine
from app.strategies.signal_engine_a import SignalEngineA
from app.strategies.signal_engine_b import SignalEngineB
from app.strategies.signal_engine_d import SignalEngineD
from app.strategies.signal_engine_e import SignalEngineE
from app.strategies.signal_engine_f import SignalEngineF
from app.strategies.signal_engine_g import SignalEngineG
from app.strategies.signal_engine_h import SignalEngineH
from app.strategies.signal_engine_c2 import SignalEngineC2
from app.strategies.signal_engine_c3 import SignalEngineC3


def session_rth_bars(df: pd.DataFrame, session_date=None) -> pd.DataFrame:
    """当日常规时段（09:30–15:59 ET）的**全量**分钟线 —— VWAP/σ 的锚定窗口。

    与 ATR/ER/趋势分那种滚动窗口不同，session VWAP 的定义就是「开盘至今全部」，
    一旦左边界被滚动窗口推着走，锚点就会漂移。当日 bar 自 04:00 起算（Yahoo
    prepost=True 带盘前），原先共用的 300 根切片在 14:30 ET 之后就越过 09:30，
    收盘时开盘后头 90 分钟已不在 VWAP 里（实测 SPY 2026-07-31 偏 0.45σ）。

    盘前 bar 一并剔除：Yahoo 盘前只给价格不给成交量（volume 恒 0），以往靠权重
    为零「碰巧」被排除；这里改成显式过滤，口径与 TV 的 Session VWAP 一致，
    也不再依赖数据源的这个偶然性质。

    `session_date`：显式指定"当日"。**实时调用必须传**。缺省的
    「取 df 最后一根 bar 的日期」只在盘中成立：盘前数据流尚无今日 bar 时，
    最后一根仍是昨日收盘，该缺省会返回**昨日的 RTH**，于是 VWAP/σ 全是昨日的值，
    再拿今日盘前实时价去比就得出荒谬的倍数 —— 实测 2026-07-31 盘前 SPY 被判
    「追高 10.8σ」而当时 price(750.07) 还在 vwap(750.94) 下方。
    传入当前交易日后，盘前会得到空窗口 → σ 留 None → validator 规则 7 fail-open 跳过。
    """
    if df.empty:
        return df
    day = session_date if session_date is not None else df.index[-1].date()
    same_day = df[df.index.date == day]
    if same_day.empty:
        return same_day
    return same_day.between_time(RTH_START, RTH_END, inclusive="left")


class TickerWatcher:
    """单个 ticker 盯盘器"""

    def __init__(
        self,
        ticker: str,
        on_signal: Optional[Callable[[str, StrategyResult], Awaitable[None]]] = None,
        on_signal_event: Optional[Callable[[str, SignalEvent], Awaitable[None]]] = None,
    ):
        self.ticker = ticker
        self.on_signal = on_signal
        self.on_signal_event = on_signal_event

        self.time_sync = NTPTimeSync()
        self.bar_builder = BarBuilder(self.time_sync)
        self.data_manager = DataManager(ticker, self.time_sync)

        # 信号引擎（per-ticker，持有状态机）
        self._signal_engines: list[BaseSignalEngine] = self._build_signal_engines()
        self._last_signal_events: list[SignalEvent] = []

        self._running = False
        self._live_stream = None
        self._last_result: Optional[StrategyResult] = None
        self._minute_state: dict = {}

    def _recalculate_minute_state(self) -> None:
        """按 Watcher 原始设计：只在每分钟 (1m Bar) 收盘截断时全量重算一次 1m 趋势分、VWAP、ATR 与 ER。

        算好后结果保存在 self._minute_state 中，后续交易系统 (Validator) 与 WS 心跳直接读取，零重复计算。
        """
        # 两个窗口，口径不同，不可混用：
        #   df_1m_ts  滚动 300 根 —— ATR/ER/趋势分，本就只看近端
        #   df_session 当日 RTH 全量 —— VWAP/σ 是 session 锚定量，左边界必须钉在 09:30
        df_1m_ts = self.data_manager.get_df_1m(tail=300)
        # 必须显式传当前 ET 交易日：盘前数据流还没有今日 bar 时，若让
        # session_rth_bars 按「最后一根 bar 的日期」推断，会拿到昨日 RTH，
        # VWAP/σ 全是昨日残留（见该函数 docstring 里的 SPY 10.8σ 实例）。
        session_day = self.time_sync.get_ntp_time().date()
        df_session = session_rth_bars(
            self.data_manager.get_df_1m(), session_date=session_day)
        vwap_val = None
        sigma_val = None
        atr_val = None
        trend_score_dict = None

        if not df_1m_ts.empty:
            import numpy as np
            # 1. 计算 vwap（当日 RTH 开盘至今累计，典型价=(H+L+C)/3）
            try:
                if not df_session.empty:
                    tp = (df_session['high'] + df_session['low'] + df_session['close']) / 3
                    tp_vol = tp * df_session['volume']
                    vol_sum = df_session['volume'].sum()
                    if vol_sum > 0:
                        vwap_val = float(tp_vol.sum() / vol_sum)
                        # band 宽度：典型价对 VWAP 的成交量加权标准差（复用上面已算的 tp，不重算）
                        var = float(((tp - vwap_val) ** 2 * df_session['volume']).sum() / vol_sum)
                        sigma_val = var ** 0.5
                    else:
                        vwap_val = float(df_session['close'].iloc[-1])
                else:
                    # 盘前/RTH 尚未开始：没有锚点可言，退回现价，σ 留 None
                    # → validator 规则7 fail-open 跳过追高判定（由 blackout 兜底）
                    vwap_val = float(df_1m_ts['close'].iloc[-1])
            except Exception as e:
                logger.error(f"[*] {self.ticker} 计算 vwap 失败: {e}")

            # 2. 计算 atr (最近 14 根 TR 的 SMA)
            try:
                if len(df_1m_ts) >= 15:
                    high = df_1m_ts['high'].to_numpy()
                    low = df_1m_ts['low'].to_numpy()
                    close = df_1m_ts['close'].to_numpy()
                    prev_close = close[:-1]
                    tr_1 = high[1:] - low[1:]
                    tr_2 = np.abs(high[1:] - prev_close)
                    tr_3 = np.abs(low[1:] - prev_close)
                    tr = np.maximum(tr_1, np.maximum(tr_2, tr_3))
                    atr_val = float(np.mean(tr[-14:]))
            except Exception as e:
                logger.error(f"[*] {self.ticker} 计算 atr 失败: {e}")

            # 2b. σ 的 ATR 地板：σ_session 从 0 根 bar 长起，开盘早段是高方差估计，
            #     而它落在卡片那行「距 VWAP ±x σ」的分母上。ATR 走跨日滚动窗口，
            #     开盘首分钟即有值。地板只在 σ 未长起时生效，长起来后自动失效。
            #     2026-09-01 追高闸删除后它只剩显示用途，常量名随之从
            #     CHASE_SIGMA_ATR_FLOOR 改为 VWAP_SIGMA_ATR_FLOOR。
            #     spec: docs/superpowers/specs/2026-08-08-追高闸σ开盘失真-ATR地板-design.md
            #     包 try/except 的理由：本块是唯一会把 float 塞进 Decimal 比较的地方，
            #     若上游漏进 NaN，max() 会抛 decimal.InvalidOperation，把后面的
            #     trend_score/indicators 一起带断。失败时 sigma_val 保持地板前的值，
            #     退化回改动前的行为，不影响本分钟其余计算。
            try:
                if sigma_val is not None and atr_val is not None:
                    sigma_val = float(sigma_with_atr_floor(
                        Decimal(str(sigma_val)),
                        Decimal(str(atr_val)),
                        VWAP_SIGMA_ATR_FLOOR,
                    ))
            except Exception as e:
                logger.error(f"[*] {self.ticker} σ 套 ATR 地板失败: {e}")

            try:
                # 这里只剩流动性硬闸。趋势方向/强度那四个键随趋势分于 2026-08-26
                # 下线；盯盘页徽章（_regime_display）与效率比于 2026-09-01 随四条
                # 闸一并删除。
                # `trend_score` 这个键名保留：market_context 与 feed_snapshot 都
                # 按它取 data_quality_ok，改键名等于同时动三处，没必要。
                trend_score_dict = {
                    'data_quality_ok': data_quality_ok(df_1m_ts),
                }
            except ValueError:
                # 预热期数据不足（data_quality_ok 抛 ValueError）。保持原语义：
                # 跳过不下发，而不是明确说"不可交易" —— 两者对下游不是一回事。
                pass

        # 5m MACD/RSI 实时指标（前端详情图 MACD/RSI 子图 + K线渲染门槛用）。
        # 合并 tt+4in1 时这块随 add_all_indicators 死导入被误删，导致 WS update 帧
        # 缺 `indicators` 键、前端 `if (chartObj && state.indicators)` 恒假、K线不跳。
        # 按 Watcher 设计放在每分钟预算里（原 4in1 是每 tick 在 get_state 算，会拖慢），
        # get_state 的 state.update(self._minute_state) 自动带出。
        indicators_dict = None
        try:
            from app.core.indicators import add_all_indicators
            df_5m = self.data_manager.get_df_5m(tail=100)
            if len(df_5m) >= 35:
                df_5m = add_all_indicators(df_5m)
                last = df_5m.iloc[-1]

                def _cf(v):
                    return None if pd.isna(v) else float(v)

                # `macd_hist` 取的是 iloc[-1] = **未收盘**的当前 5min bar，
                # 给前端画图用，本来就该是实时的。
                # 2026-09-01 删除：`macd_hist_closed` / `macd_hist_peak` /
                # `macd_hist_shrinking` 三个键只服务 stall 动能闸（连同它们那套
                # 「峰值窗口不许摸到盘前」的 RTH 切片），闸删了就没有消费方。
                indicators_dict = {
                    'macd': _cf(last.get('MACD_12_26_9')),
                    'macd_hist': _cf(last.get('MACDh_12_26_9')),
                    'macd_signal': _cf(last.get('MACDs_12_26_9')),
                    'rsi': _cf(last.get('RSI_9')),
                    'rsi_ma': _cf(last.get('RSI_9_MA_14')),
                }
        except Exception as e:
            logger.error(f"[*] {self.ticker} 计算 indicators 失败: {e}")

        self._minute_state = {
            # 这批 VWAP/σ 锚定的交易日。get_state 用它判新鲜度：VWAP/σ 是 session
            # 锚定量，跨日即失效，而本字典只在 1m bar 收盘时刷新。
            'session_date': session_day,
            # 本批指标算在哪一根 K 线上（该 bar 的**起始**时刻，epoch 秒）。
            # get_state 拿它算「数据落后多少秒」——崩溃重启时补齐还没落地，
            # 这里就是几十分钟前的旧 bar，下游据此 fail-closed。
            'data_ts': (None if df_1m_ts.empty
                        else float(df_1m_ts.index[-1].timestamp())),
            'vwap': vwap_val,
            'vwap_sigma': sigma_val,
            'atr': atr_val,
            'trend_score': trend_score_dict,
            'indicators': indicators_dict,
            # 2026-09-01 删除的键：`er`（规则 5 震荡闸）、`ema_dir`（规则 6 方向闸）、
            # `market_regime`（盯盘页趋势徽章）。四条闸下线后没有消费方。
        }

    def _build_signal_engines(self) -> list[BaseSignalEngine]:
        """按当前运行时配置构建启用的信号引擎列表（参数改动后可重建）。"""
        cfg = {}
        try:
            from app.services.trading_config import get_config
            cfg = get_config().get('signal_strategy', {})
        except Exception:
            pass
        from app.config import ENABLED_STRATEGIES

        # 策略 H：按本 ticker 的 gear 档位覆盖确认参数（user_watchlist.gear → STRATEGY_H_GEARS），
        # spread_threshold 再叠加全局系数（TODO P2.5，max(0.15, 票档基准 × 全局系数)）
        h_params = dict(cfg.get('H') or {})
        try:
            from app.config import STRATEGY_H_GEARS
            from app.services.trading_config import get_ticker_gear, resolve_h_spread_threshold
            gear = get_ticker_gear(self.ticker)
            if gear and gear in STRATEGY_H_GEARS:
                h_params.update(STRATEGY_H_GEARS[gear])
                h_params['gear'] = gear
            h_params['spread_threshold'] = resolve_h_spread_threshold(self.ticker)
        except Exception as e:
            logger.error(f"[*] {self.ticker} gear 档位解析失败,用默认档: {e}")

        _all_engines: list[BaseSignalEngine] = [
            SignalEngineA(cfg.get('A')),
            SignalEngineB(cfg.get('B')),
            SignalEngineD(cfg.get('D')),
            SignalEngineE(cfg.get('E')),
            SignalEngineF(cfg.get('F')),
            SignalEngineG(cfg.get('G')),
            SignalEngineH(h_params),
            SignalEngineC2(cfg.get('C2')),
            SignalEngineC3(cfg.get('C3')),
        ]
        return [e for e in _all_engines if e.name.lower() in ENABLED_STRATEGIES]

    def rebuild_engines(self) -> None:
        """用最新参数重建引擎（状态机重置，等同断线重连）。"""
        self._signal_engines = self._build_signal_engines()
        self._last_signal_events = []

    async def start(self) -> bool:
        """启动盯盘

        Returns:
            是否启动成功
        """
        # 同步 NTP
        self.time_sync.sync()

        if self.ticker.startswith("^"):
            # 纯展示 ticker（见 app/config.py MARKET_STATUS_TICKERS），不进交易池、
            # 不需要历史指标，任何 DATA_SOURCE 下都跳过 history 预热——只是实时报价源
            # 随 DATA_SOURCE 走 Yahoo/UW/... 不同分支（见 WatcherManager.start()）。
            data_source = env("DATA_SOURCE", "yahoo").lower()
            logger.info(f"[*] {self.ticker} 使用 {data_source} live quote，跳过 history 预热")
            # 预热成功/启动前，重置所有策略引擎
            for engine in self._signal_engines:
                try:
                    engine.reset()
                except Exception as e:
                    logger.error(f"[*] {self.ticker} Engine {engine.name} reset error: {e}")
            self._running = True
            return True

        # 预热数据
        if not await self.data_manager.warmup():
            return False

        # 预热成功/启动前，重置所有策略引擎
        for engine in self._signal_engines:
            try:
                engine.reset()
            except Exception as e:
                logger.error(f"[*] {self.ticker} Engine {engine.name} reset error: {e}")

        self._recalculate_minute_state()
        self._running = True
        return True

    async def stop(self) -> None:
        """停止盯盘"""
        self._running = False
        if self._live_stream:
            self._live_stream.close()

        # 保存缓存
        self.data_manager.save_cache()

    async def process_tick(self, price: float, volume: int, timestamp: Optional[datetime] = None) -> Optional[dict]:
        """处理 tick 数据

        Args:
            price: 价格
            volume: 成交量
            timestamp: 时间戳

        Returns:
            完成的 bar（如果有）
        """
        bar = self.bar_builder.update(price, volume, timestamp)

        if bar:
            # 添加到数据管理器
            self.data_manager.add_bar(bar)
            # 1m Bar 形成，重算每分钟趋势分、VWAP、ATR 与 ER 状态
            self._recalculate_minute_state()

            # 计算策略
            result = await self.calculate_strategy()

            # V7 信号回调（signal_handler 内部会检查 v7_alerts_enabled 开关）
            if self.on_signal and result:
                await self.on_signal(self.ticker, result)

            # Signal Engine A/B 事件回调
            if self.on_signal_event:
                for event in self._last_signal_events:
                    await self.on_signal_event(self.ticker, event)

            self._last_result = result

        return bar

    async def calculate_strategy(self) -> Optional[StrategyResult]:
        """运行信号引擎（当前只有 H 启用）

        2026-07-13 停用：V4 评分服务 / V7 旧 4in1 策略 / 旧 5m regime。三者
        都只为 A/B/D/E/F/G/C2/C3/V7（均未启用，见 STRATEGY_REGISTRY）或前端
        4in1 曲线图/风控展示字段服务，H 引擎的 process() 只用 df_1m['close']
        自算 EMA9/21，不依赖这些列。若未来重新启用上述策略，需要恢复
        add_4in1_indicators + V4IndicatorService + calculate_current_regime
        的计算并把 regime_score 传回 engine.process()。

        Returns:
            始终 None（V7 StrategyResult 已停算，保留返回类型兼容 on_signal 回调签名）
        """
        # 获取数据
        df_1m = self.data_manager.get_df_1m(tail=500)
        df_5m = self.data_manager.get_df_5m(tail=100)

        if df_1m.empty or df_5m.empty:
            return None

        # ── 运行 Signal Engines（当前只有 H），收集信号事件 ──────────────
        current_price = float(df_1m.iloc[-1]['close'])
        self._last_signal_events = []
        # 2026-08-11 GEX 下线：`_gex_levels` 原先只由 /api/gex/refresh 这个手动端点写入，
        # 而它背后的 calculate_gex 是恒返回全零的存根 —— 实际从未被写入过。
        # 引擎的 gex_levels 形参保留（将来若接别的墙位数据源仍从这里传入），此处恒传 None。
        for engine in self._signal_engines:
            try:
                event = engine.process(df_1m, df_5m, current_price, 0.0, None)
                if event:
                    self._last_signal_events.append(event)
            except Exception as e:
                logger.error(f"[SignalEngine{engine.name}] {self.ticker} error: {e}")

        return None

    def _data_freshness(self) -> tuple[float | None, bool]:
        """(落后秒数, 是否算落后)。没算过状态 = 没有数据，按落后处理（fail-closed）。"""
        if env("DATA_SOURCE", "yahoo").lower() == "mock":
            return None, False
        data_ts = (self._minute_state or {}).get('data_ts')
        if data_ts is None:
            return None, True
        age = self.time_sync.get_ntp_time().timestamp() - float(data_ts)
        return age, age > STATE_STALE_AFTER_S

    def get_state(self) -> dict:
        """获取当前状态：直接组装已按设计算好的 1m 静态成品数据 + 实时现价 (0 毫秒开销)"""
        current_bar = self.bar_builder.get_current_bar()

        state = {
            'ticker': self.ticker,
            'running': self._running,
            'bar_count': self.data_manager.bar_count,
            'current_bar': current_bar,
        }

        # 融入每分钟按 Watcher 设计计算好的成品数据
        state.update(self._minute_state)

        # VWAP/σ 跨日即失效。本字典只在 1m bar 收盘时刷新，而盘前数据源往往不推 bar，
        # 于是它会一直留着**昨日收盘**算出的 VWAP/σ；上面的 current_bar 却是实时价。
        # 两者拼进同一份快照，validator 就会拿今日价去比昨日 VWAP —— 实测
        # 2026-07-31 盘前 SPY 被判「追高 10.8σ」，而当时 price(750.07) 尚在
        # vwap(750.94) 下方、做多本属有利位置（见 ttt_intercept_log）。
        # 置 None 后走 market_context 既有的兜底：vwap→现价、σ→0 → 规则 7 fail-open 跳过。
        # 只失效这两个：ATR/ER/趋势分是滚动窗口量，跨日仍有意义，不在本次修复范围。
        if self._minute_state and \
                self._minute_state.get('session_date') != self.time_sync.get_ntp_time().date():
            state['vwap'] = None
            state['vwap_sigma'] = None

        # 数据落后多少秒（2026-08-28）。**在读取时刻算**，不是在重算时刻算 ——
        # 状态本身停止刷新（崩溃重启后补齐未落地、行情源不推 bar）正是要抓的情况，
        # 只在重算时打一次标记就永远抓不到。下游（market_context 硬闸 / 卡片）只读
        # `data_stale` 这个结论，判据不许有第二份。
        # mock 回放不判：回放的钟与真实钟不同源，拿真钟去比会把整个 mock 联调拦死。
        state['data_age_s'], state['data_stale'] = self._data_freshness()

        # H 策略当前档位 + 生效阈值
        for engine in self._signal_engines:
            if engine.name == 'H':
                state['h_gear'] = getattr(engine, '_gear', None)
                state['h_spread_threshold'] = getattr(engine, '_spread_threshold', None)
                break

        if self._last_result:
            state['strategy'] = {
                'fast': self._last_result.fast,
                'slow': self._last_result.slow,
            }
            state['signals'] = {
                'golden_cross': self._last_result.signals.golden_cross,
                'death_cross': self._last_result.signals.death_cross,
                'bull_pattern': self._last_result.signals.bull_pattern,
                'bear_pattern': self._last_result.signals.bear_pattern,
            }
            state['risk'] = {
                'in_opening': self._last_result.in_opening,
                'extreme_volatility': self._last_result.extreme_volatility,
                'in_saturation': self._last_result.in_saturation,
            }
            state['atr_quality'] = self._last_result.atr_quality
            state['time_factor'] = self._last_result.time_factor

        return state


class WatcherManager(QuoteLoopsMixin):
    """多 ticker 管理器"""

    def __init__(
        self,
        on_signal: Optional[Callable[[str, StrategyResult], Awaitable[None]]] = None,
        on_update: Optional[Callable[[str, dict], Awaitable[None]]] = None,
        on_signal_event: Optional[Callable[[str, SignalEvent], Awaitable[None]]] = None,
    ):
        self.on_signal = on_signal
        self.on_update = on_update
        self.on_signal_event = on_signal_event

        self._watchers: dict[str, TickerWatcher] = {}
        self._running = False
        self._yf_ws_task: Optional[asyncio.Task] = None
        self._live_quotes: dict[str, dict] = {}
        self._last_quote_times: dict[str, str] = {}

        # 2026-07-23 簇E #4：票池信号驱动排序（feed_snapshot 服务的输入）——之前一直是
        # 死代码，票池展示顺序从未按信号新鲜度排过。
        self.last_signal_ts: dict[str, float] = {}

        # 前端 state 推送节流：Yahoo WS 报价可能每秒多次，但 get_state() 每次都要
        # 重算 trend_score(300根1m) + 5m MACD/RSI，且每次都全量广播触发前端图表重绘。
        # 信号引擎只在整分钟 bar 收盘时跑（process_tick 的 bar 完成才调用 calculate_strategy），
        # 与这里的 UI 推送完全解耦，所以节流只影响价格/图表刷新频率，不影响信号时效性。
        self._last_state_push: dict[str, float] = {}
        self._state_push_interval: float = 1.0  # 秒，同一 ticker 两次推送最小间隔

    def set_callbacks(
        self,
        *,
        on_signal: Optional[Callable[[str, StrategyResult], Awaitable[None]]] = None,
        on_update: Optional[Callable[[str, dict], Awaitable[None]]] = None,
        on_signal_event: Optional[
            Callable[[str, SignalEvent], Awaitable[None]]
        ] = None,
    ) -> None:
        """注册由入口/传输层提供的事件消费者。"""
        self.on_signal = on_signal
        self.on_update = on_update
        self.on_signal_event = on_signal_event
        for watcher in self._watchers.values():
            watcher.on_signal = on_signal
            watcher.on_signal_event = on_signal_event

    def record_signal(self, ticker: str) -> None:
        """Signal Engine A/B 出信号时打点，供 feed_snapshot 驱动票池排序 + 前端信号定位（#4/#5）。"""
        self.last_signal_ts[ticker.upper()] = time.time()

    async def add_ticker(self, ticker: str) -> bool:
        """添加 ticker

        Args:
            ticker: 股票代码

        Returns:
            是否添加成功
        """
        if ticker in self._watchers:
            return True

        watcher = TickerWatcher(
            ticker,
            on_signal=self.on_signal,
            on_signal_event=self.on_signal_event,
        )

        if await watcher.start():
            self._watchers[ticker] = watcher
            return True

        return False

    def bar_count(self, ticker: str) -> int:
        """该票手上现有多少根 1m K 线（不在池里 = 0）。

        手动加票接口拿它回报「补了多少根」给前端。走这个方法而不是让路由层伸手进
        `_watchers` —— 路由层不该知道盯盘池的内部结构。
        """
        watcher = self._watchers.get(ticker.upper())
        if watcher is None:
            return 0
        return len(watcher.data_manager.get_df_1m())

    async def remove_ticker(self, ticker: str) -> bool:
        """移除 ticker

        Args:
            ticker: 股票代码

        Returns:
            是否移除成功
        """
        if ticker not in self._watchers:
            return False

        watcher = self._watchers.pop(ticker)
        await watcher.stop()
        return True

    async def start(self) -> None:
        """启动盯盘"""
        if self._running:
            return
            
        self._running = True
        
        # 启动所有停止的 watcher
        for watcher in self._watchers.values():
            if not watcher._running:
                await watcher.start()
                
        data_source = env("DATA_SOURCE", "yahoo").lower()
        if data_source == "mock":
            logger.info("[WatcherManager] 使用本地回放服务器作为实时数据源（盘外测试）")
            self._yf_ws_task = asyncio.create_task(self._mock_ws_quote_loop())
        elif data_source == "massive":
            logger.info("[WatcherManager] 使用 Massive WebSocket 作为实时数据源")
            self._yf_ws_task = asyncio.create_task(self._massive_ws_quote_loop())
        elif data_source == "uw":
            logger.info("[WatcherManager] 使用 Unusual Whales WebSocket 作为实时数据源")
            self._yf_ws_task = asyncio.create_task(self._uw_ws_quote_loop())
        else:
            logger.info("[WatcherManager] 使用 Yahoo WebSocket 作为实时数据源")
            self._yf_ws_task = asyncio.create_task(self._yf_quote_loop())

    async def stop(self) -> None:
        """停止盯盘"""
        self._running = False

        if self._yf_ws_task:
            self._yf_ws_task.cancel()
            try:
                await self._yf_ws_task
            except asyncio.CancelledError:
                pass

        # 停止所有 watcher
        for watcher in self._watchers.values():
            await watcher.stop()

    async def _fill_gaps(self, source_name: str, tickers: Optional[list[str]] = None,
                         *, reason: str = "断线") -> None:
        # tickers=None 补全池；分片连接重连时只传本片 tickers，避免每片都全量补（N× 冗余）。
        # reason 只进日志：断线重连与「行情窗口开门」都会补，日志里必须能分得清。
        et_tz = pytz.timezone('America/New_York')
        disconnect_time = datetime.now(et_tz)
        if tickers is None:
            watchers = list(self._watchers.values())
        else:
            watchers = [self._watchers[t] for t in tickers if t in self._watchers]
        # 起点用 last_known_bar_time 而不是 last_bar_time：过夜/周末重启时实时侧一根
        # bar 都还没有，认 last_bar_time 会把每只票都跳过（详见该属性 docstring）。
        fill_tasks = [
            w.data_manager.fill_gap(w.data_manager.last_known_bar_time, disconnect_time)
            for w in watchers
            if w.data_manager.last_known_bar_time is not None
        ]
        if fill_tasks:
            logger.info(f"[{source_name}] {reason}，补齐 {len(fill_tasks)} 个 ticker 缺口...")
            await asyncio.gather(*fill_tasks, return_exceptions=True)

    async def _ingest_tick(
        self,
        ticker: str,
        price: float,
        tick_vol: int,
        timestamp: Optional[datetime] = None,
    ) -> None:
        """单笔 tick 入 watcher + 节流推送 state（四路数据源共用尾巴）。

        bar 收盘（新的一分钟）立即推送；否则同一 ticker 至少间隔 _state_push_interval
        秒才重算/推送一次，跳过密集 tick 中的中间态。Mock 传虚拟历史时间，
        实时源省略时继续使用 NTP 墙钟。未登记的 ticker 直接忽略。
        """
        watcher = self._watchers.get(ticker)
        if not watcher:
            return
        bar = await watcher.process_tick(price, tick_vol, timestamp)
        if self.on_update:
            now = time.monotonic()
            last_push = self._last_state_push.get(ticker, 0.0)
            if bar or (now - last_push) >= self._state_push_interval:
                self._last_state_push[ticker] = now
                state = watcher.get_state()
                await self.on_update(ticker, state)

    def active_tickers(self) -> list[str]:
        """当前活跃盯盘标的代码列表。

        只取 keys，**不构造 state**（区别于 get_all_states）—— 距墙服务每 10s
        调一次只需要代码，为每只票跑一遍 get_state() 纯属浪费。
        """
        return list(self._watchers.keys())

    def get_all_states(self) -> dict[str, dict]:
        """获取所有 ticker 状态

        Returns:
            ticker -> state 映射
        """
        return {ticker: watcher.get_state() for ticker, watcher in self._watchers.items()}

    def get_ticker_state(self, ticker: str) -> Optional[dict]:
        """获取单个 ticker 状态

        Args:
            ticker: 股票代码

        Returns:
            状态字典
        """
        watcher = self._watchers.get(ticker)
        if watcher:
            return watcher.get_state()
        return None

    def reload_strategy_engines(self) -> int:
        """策略参数改动后，用最新参数热重建所有 ticker 的信号引擎。

        Returns:
            受影响的 ticker 数量
        """
        count = 0
        for watcher in self._watchers.values():
            try:
                watcher.rebuild_engines()
                count += 1
            except Exception as e:
                logger.error(f"[WatcherManager] {watcher.ticker} rebuild_engines 失败: {e}")
        return count

    def reload_ticker_engine(self, ticker: str) -> bool:
        """单个 ticker 换档（gear）后热重建其信号引擎（状态机重置，不影响其他 ticker）。

        TODO P2.5：POST /api/set-gear 写完 user_watchlist.gear 后调用。
        """
        watcher = self._watchers.get(ticker)
        if not watcher:
            return False
        watcher.rebuild_engines()
        return True

    def apply_global_spread_factor(self) -> int:
        """全局 spread 系数变更后，逐个 ticker 热更新其 H 引擎的 spread_threshold。

        与 reload_ticker_engine 不同：只改阈值数值，不重置状态机（同向再响的
        今日历史保留）。TODO P2.5：POST /api/global-spread-factor 后调用。

        Returns:
            成功热更新的 ticker 数量
        """
        from app.services.trading_config import resolve_h_spread_threshold
        count = 0
        for ticker, watcher in self._watchers.items():
            for engine in watcher._signal_engines:
                if engine.name == 'H' and hasattr(engine, 'set_spread_threshold'):
                    try:
                        engine.set_spread_threshold(resolve_h_spread_threshold(ticker))
                        count += 1
                    except Exception as e:
                        logger.error(f"[WatcherManager] {ticker} H引擎热更新spread_threshold失败: {e}")
        return count



# -----------------------------
# Global Singleton Instance
# -----------------------------
GLOBAL_WATCHER = WatcherManager()
