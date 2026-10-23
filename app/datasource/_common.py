"""数据接入层协议。

实现只暴露原始市场/账户/期权数据，不装配交易规则或订单上下文。
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol


class DataSource(Protocol):
    name: str

    def market_payload(self, ticker: str) -> dict: ...

    def clock(self) -> Any: ...

    def buying_power(self) -> Decimal: ...

    def program_pnl(self, now: datetime | None = None) -> Any: ...

    def contract_candidates(
        self,
        ticker: str,
        direction: str,
        price: Decimal,
        **filters,
    ) -> list: ...

    def option_quote(self, option_code: str, *, fresh: bool = False) -> Any: ...

    def signal_online(self) -> bool: ...

    def watcher_states(self) -> dict[str, dict]: ...

    def last_signal_timestamps(self) -> dict[str, float]: ...
