"""
策略基类 - 定义统一接口
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional, Any
import pandas as pd


@dataclass
class StrategySignal:
    """策略信号"""
    golden_cross: bool = False
    death_cross: bool = False
    bull_pattern: bool = False
    bear_pattern: bool = False


@dataclass
class StrategyResult:
    """策略计算结果"""
    fast: float  # fast_score
    slow: float  # slow_score
    signals: StrategySignal

    # 风控状态（可选，用于 UI 展示）
    in_opening: bool = False
    extreme_volatility: bool = False
    in_saturation: bool = False
    no_trade_zone: bool = False

    # 质量因子（可选）
    atr_quality: float = 0.0
    time_factor: float = 0.0


@dataclass
class SignalEvent:
    """信号引擎产出的单次信号事件（策略 A / B 共用）"""
    strategy: str        # "A" | "B"
    signal_type: str     # "cross_confirm" | "pullback_bounce" | "breakout" | "pullback" | "contraction" | "exhaustion"
    direction: str       # "bull" | "bear"
    confidence: str      # "high" | "low"
    label: str           # 人类可读描述
    metadata: dict = field(default_factory=dict)


class BaseSignalEngine(ABC):
    """信号引擎基类（策略 A / B 继承）

    每个 ticker 必须创建独立实例，引擎内部维护状态机。
    """

    @abstractmethod
    def process(
        self,
        df_1m: pd.DataFrame,
        df_5m: pd.DataFrame,
        current_price: float,
        regime_score: float,
        gex_levels: Optional[dict] = None,
    ) -> Optional[SignalEvent]:
        """处理新定格的 1m bar，返回信号事件或 None

        Args:
            df_1m: 1m K 线 DataFrame（含 V4/V7 评分及技术指标列）
            df_5m: 5m K 线 DataFrame
            current_price: 最新 tick 价格
            regime_score: Market Regime 净得分（bull - bear）
            gex_levels: GEX 关键位（可选，策略 B 的 exhaustion 检测用）
        """
        pass

    @property
    @abstractmethod
    def name(self) -> str:
        """策略名称，返回 'A' 或 'B'"""
        pass

    def reset(self) -> None:
        """重置内部状态机（断线重连时调用）"""
        pass


class BaseStrategy(ABC):
    """策略基类

    所有策略必须继承此类并实现 calculate 方法
    """

    @abstractmethod
    def calculate(
        self,
        df_1m: pd.DataFrame,
        df_5m: pd.DataFrame,
        current_time: Optional[pd.Timestamp] = None,
        regime_score: float = 0.0
    ) -> StrategyResult:
        """计算策略信号

        Args:
            df_1m: 1 分钟 K 线 DataFrame（需包含指标）
            df_5m: 5 分钟 K 线 DataFrame（需包含指标）
            current_time: 当前时间（用于时段判断）
            regime_score: 5m Regime 净得分（bull - bear），用于趋势过滤

        Returns:
            StrategyResult 包含 fast/slow 分数和信号
        """
        pass

    @property
    @abstractmethod
    def name(self) -> str:
        """策略名称"""
        pass

    def get_required_indicators(self) -> dict:
        """返回策略需要的指标参数

        Returns:
            dict: 指标参数配置
        """
        return {}


def _wilder(series: pd.Series, n: int) -> pd.Series:
    return series.ewm(alpha=1.0 / n, adjust=False).mean()


def _atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    h, l, c = df['high'], df['low'], df['close']
    tr = pd.concat([
        (h - l),
        (h - c.shift(1)).abs(),
        (l - c.shift(1)).abs(),
    ], axis=1).max(axis=1)
    return _wilder(tr, n)
