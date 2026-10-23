"""
K线合成模块 - tick → 1m OHLCV
金融行业标准合成规则，NTP 时间对齐
"""

import ntplib
from datetime import datetime, timedelta
from typing import Optional
import pandas as pd
import pytz

# NTP 服务器
NTP_SERVER = 'pool.ntp.org'
NTP_TIMEOUT = 5

# 时区
ET_TIMEZONE = 'America/New_York'
UTC_TIMEZONE = 'UTC'


class NTPTimeSync:
    """NTP 时间同步"""

    def __init__(self, ntp_server: str = NTP_SERVER):
        self.ntp_server = ntp_server
        self._offset: Optional[float] = None
        self._last_sync: Optional[datetime] = None

    def sync(self) -> bool:
        """同步 NTP 时间

        Returns:
            是否同步成功
        """
        try:
            client = ntplib.NTPClient()
            response = client.request(self.ntp_server, version=3, timeout=NTP_TIMEOUT)
            self._offset = response.offset
            self._last_sync = datetime.now(pytz.UTC)
            return True
        except (ntplib.NTPException, Exception):
            return False

    def get_ntp_time(self) -> datetime:
        """获取 NTP 时间（US/Eastern）

        Returns:
            当前 US/Eastern 时间
        """
        now_utc = datetime.now(pytz.UTC)

        if self._offset is not None:
            now_utc = now_utc + timedelta(seconds=self._offset)

        et_tz = pytz.timezone(ET_TIMEZONE)
        return now_utc.astimezone(et_tz)

    def get_minutes_since_open(self, current_time: Optional[datetime] = None) -> float:
        """获取开盘后分钟数

        Args:
            current_time: 当前时间（None 则使用 NTP 时间）

        Returns:
            开盘后分钟数（负数表示开盘前）
        """
        if current_time is None:
            current_time = self.get_ntp_time()

        # 当天 9:30 ET
        market_open = current_time.replace(hour=9, minute=30, second=0, microsecond=0)

        delta = current_time - market_open
        return delta.total_seconds() / 60


class BarBuilder:
    """1 分钟 K 线合成器"""

    def __init__(self, time_sync: Optional[NTPTimeSync] = None):
        self.time_sync = time_sync or NTPTimeSync()

        # 当前分钟状态
        self._current_bar: dict = {}
        self._current_minute: Optional[int] = None

        # 完成的 bar 缓冲
        self._completed_bars: list[dict] = []

    def _get_bar_timestamp(self, dt: datetime) -> int:
        """获取 bar 的时间戳（分钟级）

        Args:
            dt: datetime

        Returns:
            分钟级时间戳
        """
        # 对齐到分钟开始
        bar_time = dt.replace(second=0, microsecond=0)
        return int(bar_time.timestamp())

    def _get_current_minute(self, dt: datetime) -> int:
        """获取当前分钟数（用于判断是否新 bar）

        Args:
            dt: datetime

        Returns:
            分钟标识（YYYYMMDDHHMM）
        """
        return int(dt.strftime('%Y%m%d%H%M'))

    def update(self, price: float, volume: int, timestamp: Optional[datetime] = None) -> Optional[dict]:
        """更新 tick 数据，返回完成的 bar（如果有）

        Args:
            price: 成交价格
            volume: 成交量
            timestamp: 时间戳（None 则使用 NTP 时间）

        Returns:
            完成的 bar 字典，如果没有完成的 bar 则返回 None
        """
        if timestamp is None:
            timestamp = self.time_sync.get_ntp_time()

        current_minute = self._get_current_minute(timestamp)

        # 新的一分钟开始
        if self._current_minute is not None and current_minute != self._current_minute:
            # 保存当前 bar
            completed_bar = self._current_bar.copy()
            completed_bar['close'] = completed_bar.get('last_price', completed_bar['open'])
            self._completed_bars.append(completed_bar)

            # 重置
            self._current_bar = {}
            self._current_minute = current_minute

        # 更新当前 bar
        if not self._current_bar:
            # 新 bar
            bar_datetime = timestamp.replace(second=0, microsecond=0)
            self._current_bar = {
                'timestamp': self._get_bar_timestamp(timestamp),
                'datetime': bar_datetime,
                'open': price,
                'high': price,
                'low': price,
                'volume': volume,
                'last_price': price,
            }
            self._current_minute = current_minute
        else:
            # 更新现有 bar
            self._current_bar['high'] = max(self._current_bar['high'], price)
            self._current_bar['low'] = min(self._current_bar['low'], price)
            self._current_bar['volume'] += volume
            self._current_bar['last_price'] = price

        # 返回完成的 bar（如果有）
        if self._completed_bars:
            return self._completed_bars.pop(0)
        return None

    def get_current_bar(self) -> Optional[dict]:
        """获取当前未完成的 bar

        Returns:
            当前 bar（实时更新）
        """
        if self._current_bar:
            bar = self._current_bar.copy()
            bar['close'] = bar.get('last_price', bar['open'])
            return bar
        return None

    def flush(self) -> list[dict]:
        """强制结束当前 bar，返回所有缓存的 bar

        Returns:
            所有完成的 bar 列表
        """
        bars = self._completed_bars.copy()

        if self._current_bar:
            bar = self._current_bar.copy()
            bar['close'] = bar.get('last_price', bar['open'])
            bars.append(bar)

        self._completed_bars = []
        self._current_bar = {}
        self._current_minute = None

        return bars


def bars_to_dataframe(bars: list[dict]) -> pd.DataFrame:
    """将 bar 列表转换为 DataFrame"""
    if not bars:
        return pd.DataFrame(columns=['open', 'high', 'low', 'close', 'volume'])

    # 为了避免 pd.DataFrame(bars) 自动推断时区出错
    # 我们预先将 datetime 处理成 pandas 喜欢的 Timestamp 对象
    processed_bars = []
    for b in bars:
        row = b.copy()
        if 'datetime' in row:
            # 统一转为带时区的 Timestamp
            row['datetime'] = pd.to_datetime(row['datetime'], utc=True).tz_convert(ET_TIMEZONE)
        processed_bars.append(row)

    df = pd.DataFrame(processed_bars)

    # 确保列顺序
    cols = ['timestamp', 'datetime', 'open', 'high', 'low', 'close', 'volume']
    existing_cols = [c for c in cols if c in df.columns]
    df = df[existing_cols]

    if 'datetime' in df.columns:
        df = df.set_index('datetime')
    
    df = df.sort_index()
    return df


def resample_to_5m(df_1m: pd.DataFrame) -> pd.DataFrame:
    """将 1m K 线重采样为 5m"""
    if df_1m.empty:
        return pd.DataFrame(columns=['open', 'high', 'low', 'close', 'volume'])

    # 确保 datetime 索引
    if not isinstance(df_1m.index, pd.DatetimeIndex):
        df_1m = df_1m.reset_index()
        if 'datetime' in df_1m.columns:
            # 修复：这里也必须指定 utc=True
            df_1m['datetime'] = pd.to_datetime(df_1m['datetime'], utc=True).dt.tz_convert(ET_TIMEZONE)
            df_1m = df_1m.set_index('datetime')

    ohlc = {
        'open': 'first',
        'high': 'max',
        'low': 'min',
        'close': 'last',
        'volume': 'sum',
    }

    # resample 在带时区的索引上运行是安全的
    df_5m = df_1m.resample('5min').agg(ohlc).dropna()

    return df_5m
