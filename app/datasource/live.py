"""Alpaca 原始实时数据源。"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from app.integrations.alpaca.data import AlpacaData


class LiveDataSource:
    """只提供原始 bars、账户、时钟与期权链。

    2026-08-11 GEX 下线：本类原有的 `gex_levels()` / `_async_update_gex()` 及其
    整套 UW 客户端管线（`_uw` / `_uw_fail_ts` / `_loop`）只为 GEX 存在，随规则7b
    距墙与触墙止盈一并删除。本模块因此不再依赖 `app.integrations.uw`。
    """

    name = "live"

    def __init__(self, cache_ttl: float = 20.0):
        self.alpaca = AlpacaData(cache_ttl)

    def market_payload(self, ticker: str) -> dict:
        return {"kind": "bars", "bars": self.alpaca.rth_bars(ticker)}

    def clock(self):
        return self.alpaca.clock()

    def buying_power(self) -> Decimal:
        return self.alpaca.buying_power()

    def program_pnl(self, now: datetime | None = None):
        return self.alpaca.program_pnl(now)

    def contract_candidates(
        self,
        ticker: str,
        direction: str,
        price: Decimal,
        **filters,
    ):
        return self.alpaca.contract_candidates(
            ticker,
            direction,
            price,
            **filters,
        )

    def option_quote(self, option_code: str, *, fresh: bool = False):
        return self.alpaca.option_quote(option_code, fresh=fresh)

    def signal_online(self) -> bool:
        return True

    def watcher_states(self) -> dict[str, dict]:
        return {}

    def last_signal_timestamps(self) -> dict[str, float]:
        return {}
