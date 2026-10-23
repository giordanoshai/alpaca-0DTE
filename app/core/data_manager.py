"""
数据管理模块
- 历史数据加载（yfinance）
- 本地 Parquet 缓存
- 断线补齐
- 预热逻辑
"""

import json
import os
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import quote
from urllib.request import build_opener, ProxyHandler, Request
import numpy as np
import pandas as pd
import pytz
from pathlib import Path
import random

from app.core.bar_builder import NTPTimeSync, bars_to_dataframe, resample_to_5m
from app.core.dailylogger import get_logger
import yfinance as yf

logger = get_logger("tt.data_manager")

# K线预热/补齐按 DATA_SOURCE 分流（与实时报价源一致）：yahoo(默认)→yfinance，mock→本地回放。
# yfinance 内部 requests 自动读 os.environ 的 HTTP(S)_PROXY —— 这段注入是全进程
# 唯一入口，删掉会静默破坏 Yahoo 实时源的代理路由，不要删。
from app.config import HTTP_PROXY, env, normalize_http_proxy
if HTTP_PROXY:
    # 显式赋值而非 setdefault：系统里残留的用户级 HTTP_PROXY 会静默盖过 .env，
    # 导致「改了 .env 的代理，Yahoo 却还走老地址」且无迹可查（2026-09-01 修）。
    _yahoo_proxy = normalize_http_proxy(HTTP_PROXY)
    os.environ['HTTP_PROXY'] = _yahoo_proxy
    os.environ['HTTPS_PROXY'] = _yahoo_proxy


# 时区
ET_TIMEZONE = 'America/New_York'

# 缓存目录
CACHE_DIR = Path(__file__).parent / '.cache'
CACHE_DIR.mkdir(exist_ok=True)

# 预热数据最小条数
MIN_WARMUP_BARS = 100 # 只要有数据就行，不需要强制300，但下载时会尽量下载500

# 内存滑动窗口最大条数（约2-3天1m数据，防止长期运行时内存和缓存无限增长）
MAX_CACHE_BARS = 3000

# 启动时最多等断层补齐多少秒（超时转后台，不阻断启动）。
# 14 只票并行补齐实测约 10s（2026-08-28 21:45:53 重启 → 21:46:05 缓存落盘），
# 留一倍余量。这几秒本来也不能交易：状态还没算出来，闸门一律拦。
WARMUP_FILL_WAIT_S = 20.0


def _fetch_json_no_proxy(url: str, timeout: float) -> dict:
    """读取本机 Mock HTTP JSON；显式禁用系统/.env 代理。"""
    opener = build_opener(ProxyHandler({}))
    request = Request(url, headers={"Accept": "application/json"})
    with opener.open(request, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return json.loads(response.read().decode(charset))


class DataManager:
    """K 线数据管理器"""

    def __init__(self, ticker: str, time_sync: Optional[NTPTimeSync] = None):
        self.ticker = ticker
        self.time_sync = time_sync or NTPTimeSync()
        self.cache_file = CACHE_DIR / f"{ticker}_1m.parquet"

        # 内存中的滑动窗口
        self._df_1m: pd.DataFrame = pd.DataFrame()
        self._df_5m: pd.DataFrame = pd.DataFrame()

        # 最后更新时间
        self._last_bar_time: Optional[datetime] = None

    def _get_trading_session(self) -> str:
        """判断当前交易时段

        Returns:
            'pre_market' | 'regular' | 'after_hours' | 'closed'
        """
        now_et = self.time_sync.get_ntp_time()
        hour = now_et.hour
        minute = now_et.minute

        # 盘前: 04:00 - 09:30 ET
        if 4 <= hour < 9 or (hour == 9 and minute < 30):
            return 'pre_market'

        # 常规时段: 09:30 - 16:00 ET
        if (hour == 9 and minute >= 30) or (10 <= hour < 16):
            return 'regular'

        # 盘后: 16:00 - 20:00 ET
        if 16 <= hour < 20:
            return 'after_hours'

        return 'closed'

    def _get_cache_expiry(self) -> datetime:
        """获取缓存过期时间（当天收盘后）

        Returns:
            过期时间 datetime
        """
        now_et = self.time_sync.get_ntp_time()
        # 当天 16:30 ET 过期
        expiry = now_et.replace(hour=16, minute=30, second=0, microsecond=0)
        return expiry

    def _is_cache_valid(self) -> bool:
        """检查缓存是否有效

        Returns:
            缓存是否有效
        """
        if not self.cache_file.exists():
            return False

        # 检查文件修改时间
        mtime = datetime.fromtimestamp(self.cache_file.stat().st_mtime, pytz.UTC)
        mtime_et = mtime.astimezone(pytz.timezone(ET_TIMEZONE))

        now_et = self.time_sync.get_ntp_time()

        # 如果现在是新的一天，缓存无效
        if now_et.date() > mtime_et.date():
            return False

        # 如果现在是收盘后且缓存是今天盘中的，有效
        if now_et.hour >= 16 and mtime_et.hour < 16 and mtime_et.date() == now_et.date():
            return True

        # 否则检查是否在过期时间前
        expiry = self._get_cache_expiry()
        return now_et < expiry

    def load_cache(self) -> bool:
        """加载本地缓存

        Returns:
            是否加载成功
        """
        if env("DATA_SOURCE", "yahoo").lower() == "mock":
            return False
        if not self._is_cache_valid():
            return False

        try:
            df = pd.read_parquet(self.cache_file)
            if not df.empty:
                df.index = pd.to_datetime(df.index, utc=True).tz_convert(ET_TIMEZONE)
                self._df_1m = df
                self._df_5m = resample_to_5m(df)
                return True
        except Exception:
            pass

        return False

    def save_cache(self) -> bool:
        """保存到本地缓存

        Returns:
            是否保存成功
        """
        # Mock 使用 trading_test 的独立数据集，不得把回放/预热数据写进生产行情缓存。
        if env("DATA_SOURCE", "yahoo").lower() == "mock":
            return True
        try:
            if self._df_1m.empty:
                return False
            self._df_1m.to_parquet(self.cache_file, index=True)
            return True
        except Exception:
            return False

    def download_history(
        self,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        interval: str = '1m',
        period: str = '2d',
    ) -> pd.DataFrame:
        """下载历史数据，按 DATA_SOURCE 分流（与实时报价源保持一致）：
        mock→本地回放服务器前一交易日，yahoo(默认及其它一切取值)→yfinance。

        Args:
            start: 开始时间（可选）
            end: 结束时间（可选）
            interval: K 线周期（'1m', '5m'）
            period: 如果未指定 start/end，使用 period（如 '1d', '5d'）

        Returns:
            历史 DataFrame（index=datetime, tz=ET，列 [open,high,low,close,volume]）
        """
        data_source = env("DATA_SOURCE", "yahoo").lower()
        if data_source == "mock":
            return self._download_history_mock()
        return self._download_history_yf(interval, start, end, period)

    def _download_history_mock(self) -> pd.DataFrame:
        """从 trading_test 获取回放日前一交易日的 RTH bars。"""
        base_url = env(
            "MOCK_HISTORY_URL", "http://127.0.0.1:9000/sim/history").rstrip("/")
        url = f"{base_url}/{quote(self.ticker, safe='')}"
        try:
            payload = _fetch_json_no_proxy(url, timeout=3.0)
            rows = payload.get("bars") if isinstance(payload, dict) else None
            if not rows:
                return pd.DataFrame()

            df = pd.DataFrame(rows)
            required = {"timestamp", "open", "high", "low", "close", "volume"}
            if not required.issubset(df.columns):
                logger.warning(
                    f"[DataManager] {self.ticker} Mock 预热响应缺字段："
                    f"{sorted(required - set(df.columns))}"
                )
                return pd.DataFrame()

            df["datetime"] = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert(
                ET_TIMEZONE)
            df = df.set_index("datetime")
            df = df[["open", "high", "low", "close", "volume"]].dropna()
            logger.info(
                f"[DataManager] {self.ticker} Mock 预热日="
                f"{payload.get('warmup_date')}，回放日={payload.get('replay_date')}，"
                f"{len(df)} bars"
            )
            return df
        except Exception as e:
            logger.warning(
                f"[DataManager] {self.ticker} Mock 前一交易日预热失败: {e}")
            return pd.DataFrame()

    def _download_history_yf(self, interval, start, end, period) -> pd.DataFrame:
        """yfinance 源（默认；YF+Alpaca 路径）。代理经 import-time os.environ 注入。"""
        try:
            ticker_obj = yf.Ticker(self.ticker)

            if start is None and end is None:
                df = ticker_obj.history(period=period, interval=interval, prepost=True, timeout=3.0)
            else:
                if start is None:
                    start = datetime.now(pytz.UTC) - timedelta(days=7)
                if end is None:
                    end = datetime.now(pytz.UTC)
                # yfinance 需要无时区的时间
                if hasattr(start, 'tzinfo') and start.tzinfo:
                    start = start.astimezone(pytz.UTC).replace(tzinfo=None)
                if hasattr(end, 'tzinfo') and end.tzinfo:
                    end = end.astimezone(pytz.UTC).replace(tzinfo=None)
                start_date = start.strftime('%Y-%m-%d')
                # yfinance 的 end 是**右开区间**：写今天的日期，返回的最后一天是昨天，
                # 今天一根 bar 都拿不回来。跨夜缺口（昨晚收盘 → 今早开盘）正是栽在这里 ——
                # 2026-09-03 线上 SPY 缓存空了 806 分钟、今天盘前全缺，而昨天盘后
                # 16:00–20:00 那 239 根零成交量 bar 一根不少地补进来了，就是这个右开
                # 区间的直接后果（现场记录见 tests/test_overnight_gap_fill.py 顶部）。
                # 故 end 统一 +1 天。
                #
                # 加一天之后 start_date 与 end_date 不可能再相等，原先「同一天返回空数据
                # → 退回 period='1d'」那个分支随之消失：同日缺口（盘中断线重连）也走
                # 精确区间请求，不再连带把当天全量重下一遍。
                end_date = (end + timedelta(days=1)).strftime('%Y-%m-%d')
                df = ticker_obj.history(start=start_date, end=end_date, interval=interval,
                                        prepost=True, timeout=3.0)

            if df.empty:
                return pd.DataFrame()

            df = df.reset_index()
            if 'Datetime' in df.columns:
                df['datetime'] = pd.to_datetime(df['Datetime'], utc=True).dt.tz_convert(ET_TIMEZONE)
            elif 'Date' in df.columns:
                df['datetime'] = pd.to_datetime(df['Date'], utc=True).dt.tz_convert(ET_TIMEZONE)
            else:
                df['datetime'] = pd.to_datetime(df.index, utc=True).dt.tz_convert(ET_TIMEZONE)

            df = df.set_index('datetime')
            df = df[['Open', 'High', 'Low', 'Close', 'Volume']]
            df.columns = ['open', 'high', 'low', 'close', 'volume']
            df = df.dropna()

            # 过滤 yfinance 盘前盘后的异常数据（Bad Prints）
            df = self._clean_bad_prints(df)

            return df
        except Exception as e:
            logger.warning(f"[DataManager] {self.ticker} yfinance 获取 {period} 历史数据失败（预热/补缺缺数据）: {e}")
            return pd.DataFrame()

    @staticmethod
    def _is_crypto(ticker: str) -> bool:

        t = ticker.upper()
        return '-USD' in t or '-USDT' in t or '-BTC' in t

    # 盘前/盘后坏 print 清洗参数
    _WICK_CLAMP_K = 4.0          # 影线越过 局部中枢±K*局部尺度 才算坏 print
    _WICK_CLAMP_WIN = 15         # 计算局部中枢/尺度的滚动窗口（分钟）
    _WICK_MIN_SCALE_FRAC = 0.0008  # 局部尺度地板（价格的 0.08%），防止平段 scale=0 误伤正常小影线

    def _clean_bad_prints(self, df: pd.DataFrame) -> pd.DataFrame:
        """清洗 yfinance 盘前/盘后（ETH）的坏 print 长影线。

        背景（2026-07-09 重写，见 changelog）：盘前整体波动极小（典型 1m bar 振幅
        ~0.03%），Yahoo 偶发把某个错误报价（如反复出现的“卡住”低价）塞进某根 bar 的
        high/low，形成向上/向下的长插针。旧逻辑用固定 3% 绝对阈值判坏，盘前这些坏
        print 偏离通常只有 ~0.9%，永远够不到 3% → 清洗形同虚设，插针原样留在图上。

        新逻辑（局部自适应 + 只夹影线）：
        - 用 close 的滚动中位数当**局部中枢** center、滚动 MAD 当**局部尺度** scale，
          尺度随盘前真实波动自适应（放量新闻缺口时带宽自动放宽，不误伤真实大影线）。
        - 只夹越界的 high/low 影线，**保留真实的 open/close**（无成交平 bar o=h=l=c
          属真实数据，不动它；坏 print 落在 close 上的极少数情形也按用户口径不处理）。
        - 只作用于 ETH（盘前盘后）bar，RTH 常规时段完全不碰。
        """
        cols = ['open', 'high', 'low', 'close']
        if df.empty or not all(c in df.columns for c in cols):
            return df

        idx = df.index
        is_eth = (
            (idx.hour < 9) |
            ((idx.hour == 9) & (idx.minute < 30)) |
            (idx.hour >= 16)
        )
        if not is_eth.any():
            return df

        df = df.copy()
        win = self._WICK_CLAMP_WIN
        k = self._WICK_CLAMP_K
        close = df['close']

        # 局部中枢：close 的居中滚动中位数（对单根坏 print 稳健）
        center = close.rolling(win, center=True, min_periods=3).median()
        center = center.fillna(close)
        # 局部尺度：|close - center| 的居中滚动中位数（MAD 味），再用价格比例地板兜底
        scale = (close - center).abs().rolling(win, center=True, min_periods=3).median().fillna(0.0)
        scale = np.maximum(scale.to_numpy(), center.to_numpy() * self._WICK_MIN_SCALE_FRAC)
        scale = pd.Series(scale, index=df.index)

        body_lo = df[['open', 'close']].min(axis=1)
        body_hi = df[['open', 'close']].max(axis=1)
        allowed_lo = np.minimum(body_lo, center) - k * scale
        allowed_hi = np.maximum(body_hi, center) + k * scale

        bad_lo = is_eth & (df['low'] < allowed_lo)
        bad_hi = is_eth & (df['high'] > allowed_hi)

        if bad_lo.any():
            df.loc[bad_lo, 'low'] = allowed_lo[bad_lo]
        if bad_hi.any():
            df.loc[bad_hi, 'high'] = allowed_hi[bad_hi]

        return df

    async def warmup(self, session: Optional[str] = None) -> bool:
        """预热数据 (Async)"""
        if session is None:
            session = self._get_trading_session()

        import asyncio

        # Mock 的唯一历史真源是回放服务器：禁止读取/保存生产 .cache。
        if env("DATA_SOURCE", "yahoo").lower() == "mock":
            print(f"[DataManager] {self.ticker} 从 Mock 服务器加载前一交易日预热...")
            df = await asyncio.to_thread(self.download_history, period="2d")
            if df.empty:
                print(f"[DataManager] {self.ticker} Mock 预热失败: 无数据")
                return False
            self._df_1m = df.sort_index()
            self._df_5m = resample_to_5m(self._df_1m)
            print(f"[DataManager] {self.ticker} Mock 预热完成: {len(self._df_1m)} bars")
            return len(self._df_1m) >= MIN_WARMUP_BARS

        # 生产数据源先尝试加载本地缓存
        cache_loaded = await asyncio.to_thread(self.load_cache)

        if cache_loaded:
            print(f"[DataManager] {self.ticker} 缓存命中: {len(self._df_1m)} bars")
            # 检查是否有断层需要补齐
            last_time = self._df_1m.index[-1]
            now_et = self.time_sync.get_ntp_time()

            # 断层必须**补齐后再返回**（2026-08-28）：本函数一返回，
            # `TickerWatcher.start()` 立刻用当前这份数据算第一份每分钟状态并对外发布。
            # 原先补齐是纯后台任务，于是崩溃重启时第一份状态算在旧数据上 —— 那天缓存
            # 停在 09:27（盘前），而 Yahoo 盘前分钟线成交量恒为 0，流动性硬闸当场判
            # 「不可交易」，13 只票全灰、真下单也被拦，直到第一根盘中 K 线收掉才自愈。
            # 反面同样成立：缓存停在盘中时硬闸会说「可以交易」，那就变成拿几十分钟前的
            # VWAP/效率比/均线方向放行一笔。守卫见 tests/test_warmup_gap_fill.py。
            #
            # 仍不允许被行情源拖住启动：等到 WARMUP_FILL_WAIT_S 就放行，任务留在后台
            # 继续跑完（新鲜度那道闸会在补齐落地前把这票挡住，见 watcher_manager.get_state）。
            if now_et - last_time >= timedelta(minutes=1):
                print(f"[*] {self.ticker} 检测到缓存断层 ({last_time.strftime('%H:%M')} -> {now_et.strftime('%H:%M')})，正在补齐...")
                fill_task = asyncio.create_task(self._async_fill_and_save(last_time, now_et))
                await asyncio.wait({fill_task}, timeout=WARMUP_FILL_WAIT_S)
                if not fill_task.done():
                    print(f"[*] {self.ticker} 补齐超过 {WARMUP_FILL_WAIT_S}s 未完成，转后台继续")
            else:
                print(f"[DataManager] {self.ticker} 缓存最新，无需补齐")

            if len(self._df_1m) >= MIN_WARMUP_BARS:
                return True

        # 无缓存或缓存过期
        print(f"[DataManager] {self.ticker} 缓存无效/无缓存，下载历史数据...")
        # 先试 period='2d'；不够再 fallback 到 '5d'（覆盖周末/长假/盘中早期等
        # 2 个日历日覆盖不到足够交易日的场景——2026-09-17 修）。
        df = await asyncio.to_thread(self.download_history, period='2d')

        if len(df) < MIN_WARMUP_BARS:
            got = len(df)
            print(f"[DataManager] {self.ticker} period='2d' 只拿到 {got} bars（<{MIN_WARMUP_BARS}），回退到 '5d'...")
            df_5d = await asyncio.to_thread(self.download_history, period='5d')
            if len(df_5d) > got:
                df = df_5d

        if df.empty:
            print(f"[DataManager] {self.ticker} 下载失败: 无数据")
            return False

        self._df_1m = df.sort_index()
        self._df_5m = resample_to_5m(self._df_1m)

        # 异步保存缓存
        await asyncio.to_thread(self.save_cache)
        print(f"[DataManager] {self.ticker} 下载完成: {len(self._df_1m)} bars")

        return len(self._df_1m) >= MIN_WARMUP_BARS

    async def _async_fill_and_save(self, last_time: datetime, now_et: datetime) -> None:
        """后台异步补齐断层数据并保存缓存，绝不上锁阻塞启动过程。"""
        import asyncio
        try:
            filled = await self.fill_gap(last_time, now_et)
            if filled:
                await asyncio.to_thread(self.save_cache)
        except Exception as e:
            print(f"[DataManager] {self.ticker} 后台补齐异常: {e}")

    async def fill_gap(self, gap_start: datetime, gap_end: datetime) -> bool:
        """填补断线缺口 (Async)"""
        import asyncio
        # Mock WS 在服务器游标处继续；历史端点只提供预热日，不能拿来补实时缺口。
        if env("DATA_SOURCE", "yahoo").lower() == "mock":
            return False
        try:
            df_gap = await asyncio.to_thread(self.download_history, start=gap_start, end=gap_end)

            if df_gap.empty:
                return False

            gap_count = len(df_gap)
            self._df_1m = pd.concat([self._df_1m, df_gap])
            self._df_1m = self._df_1m[~self._df_1m.index.duplicated(keep='last')]
            self._df_1m = self._df_1m.sort_index()
            print(f"[DataManager] {self.ticker} 补齐完成: +{gap_count} bars")
            # 滑动窗口裁剪
            if len(self._df_1m) > MAX_CACHE_BARS:
                self._df_1m = self._df_1m.tail(MAX_CACHE_BARS)
            self._df_5m = resample_to_5m(self._df_1m)

            return True
        except Exception as e:
            print(f"[DataManager] {self.ticker} 补齐失败: {e}")
            return False

    def add_bar(self, bar: dict) -> None:
        """添加新的 bar

        Args:
            bar: bar 字典
        """
        if not bar:
            return

        df_new = bars_to_dataframe([bar])

        if df_new.empty:
            return

        # 合并
        self._df_1m = pd.concat([self._df_1m, df_new])
        self._df_1m = self._df_1m[~self._df_1m.index.duplicated(keep='last')]
        self._df_1m = self._df_1m.sort_index()

        # 滑动窗口裁剪，防止长期运行时数据无限增长
        if len(self._df_1m) > MAX_CACHE_BARS:
            self._df_1m = self._df_1m.tail(MAX_CACHE_BARS)

        # 更新 5m
        self._df_5m = resample_to_5m(self._df_1m)

        self._last_bar_time = df_new.index[-1] if len(df_new) > 0 else None

    def get_df_1m(self, tail: Optional[int] = None, current_bar: Optional[dict] = None) -> pd.DataFrame:
        """获取 1m DataFrame

        Args:
            tail: 只返回最后 N 根
            current_bar: 当前未完成的 bar（由 BarBuilder 提供）

        Returns:
            1m DataFrame
        """
        df = self._df_1m.copy()
        
        if current_bar:
            df_curr = bars_to_dataframe([current_bar])
            df = pd.concat([df, df_curr])
            # 去重，以最后的数据为准（处理 1m 刚好结束时的瞬间冲突）
            df = df[~df.index.duplicated(keep='last')]
            df = df.sort_index()

        if tail is None or len(df) <= tail:
            return df
        return df.tail(tail)

    def get_df_15m(self, tail: Optional[int] = None) -> pd.DataFrame:
        """获取 15m DataFrame（从 5m 重采样合成，不补缺失 bar）

        Args:
            tail: 只返回最后 N 根

        Returns:
            15m DataFrame
        """
        if self._df_5m.empty:
            return pd.DataFrame(columns=['open', 'high', 'low', 'close', 'volume'])

        agg = {k: v for k, v in {
            'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last', 'volume': 'sum',
        }.items() if k in self._df_5m.columns}

        df_15m = self._df_5m.resample('15min', label='left', closed='left').agg(agg)
        # 不 ffill：直接丢掉没有成交量的空 bar（缺失 5m bar 不补）
        df_15m = df_15m.dropna(subset=['close'])
        df_15m = df_15m[df_15m['volume'] > 0]

        if tail is None or len(df_15m) <= tail:
            return df_15m
        return df_15m.tail(tail)

    def get_df_5m(self, tail: Optional[int] = None, current_bar: Optional[dict] = None) -> pd.DataFrame:
        """获取 5m DataFrame

        Args:
            tail: 只返回最后 N 根
            current_bar: 当前未完成的 bar

        Returns:
            5m DataFrame
        """
        if not current_bar:
            df = self._df_5m.copy()
        else:
            # 合并当前 bar 后重新计算 5m
            df_1m_plus = self.get_df_1m(current_bar=current_bar)
            df = resample_to_5m(df_1m_plus)

        if tail is None or len(df) <= tail:
            return df
        return df.tail(tail)

    @property
    def last_bar_time(self) -> Optional[datetime]:
        """最后 bar 时间"""
        return self._last_bar_time

    @property
    def last_known_bar_time(self) -> Optional[datetime]:
        """补缺口的起点：实时 bar 优先，没有就退回已加载历史的最后一根。

        与 `last_bar_time` 是两个不同的问题，不可互换：
        - `last_bar_time` 只在实时 tick 攒出 bar 时才赋值，答的是「行情流活到哪一刻」，
          新鲜度展示（`app/routers/watcher_api.py`）靠的正是这个语义，拿缓存去填会让
          陈旧缓存看起来很新鲜。
        - 补缺口要问的是「我手上的数据到哪儿为止」。服务 2026-09-02 起 24 小时常驻，
          过夜或周末重启时实时侧一根 bar 都还没有，若补齐也认 `last_bar_time`，
          `_fill_gaps` 会因为 None 把这只票整个跳过 —— 窗口开门补缺口就成了空转。
        """
        if self._last_bar_time is not None:
            return self._last_bar_time
        if not self._df_1m.empty:
            return self._df_1m.index[-1]
        return None

    @property
    def bar_count(self) -> int:
        """1m bar 数量"""
        return len(self._df_1m)
