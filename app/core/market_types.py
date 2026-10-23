"""跨层共享的原始市场数据类型。"""
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class Bar:
    ts: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
