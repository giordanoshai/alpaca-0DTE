"""交易闸门的组合输入模型。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.validator import AccountState, Contract, TickerSignal


@dataclass(frozen=True)
class MarketContext:
    signal: TickerSignal
    account: AccountState
    contract: Contract | None
    selected: Any = None
