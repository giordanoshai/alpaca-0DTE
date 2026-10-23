"""Watcher 内存状态适配器。"""
from __future__ import annotations

from typing import Any

from app.datasource.live import LiveDataSource


class WatcherDataSource(LiveDataSource):
    """从注入的 Watcher 读取原始 state，其余账户/期权数据复用 Alpaca。"""

    name = "watcher"

    def __init__(self, watcher: Any, cache_ttl: float = 20.0):
        super().__init__(cache_ttl)
        self._client = watcher

    def _state(self, ticker: str) -> dict:
        return self._client.get_ticker_state(ticker.upper()) or {}

    def market_payload(self, ticker: str) -> dict:
        return {"kind": "watcher", "state": self._state(ticker)}

    def signal_online(self) -> bool:
        return bool(self._client._running and self._client._watchers)

    def watcher_states(self) -> dict[str, dict]:
        return self._client.get_all_states()

    def last_signal_timestamps(self) -> dict[str, float]:
        return dict(self._client.last_signal_ts)
