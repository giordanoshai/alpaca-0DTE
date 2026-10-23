from decimal import Decimal

from app.watcher_client import WatcherClient


def test_get_watchlist_parses_tickers(monkeypatch):
    c = WatcherClient()
    monkeypatch.setattr(c, "_get_json", lambda path: {"tickers": ["SPY", "QQQ"]})
    assert c.get_watchlist() == ["SPY", "QQQ"]


def test_get_watchlist_returns_empty_on_error(monkeypatch):
    c = WatcherClient()
    def boom(path):
        raise ConnectionError("down")
    monkeypatch.setattr(c, "_get_json", boom)
    assert c.get_watchlist() == []


def test_get_watcher_status_reads_running(monkeypatch):
    c = WatcherClient()
    monkeypatch.setattr(c, "_get_json", lambda path: {"running": True})
    assert c.get_watcher_status() is True


def test_get_history_bars_maps_to_decimal_bars(monkeypatch):
    c = WatcherClient()
    payload = {"ticker": "SPY", "interval": "5m", "bars": [
        {"time": 111, "open": 1.0, "high": 1.2, "low": 0.9, "close": 1.1, "volume": 100},
    ]}
    captured = {}
    def fake(path):
        captured["path"] = path
        return payload
    monkeypatch.setattr(c, "_get_json", fake)
    bars = c.get_history_bars("spy")
    assert captured["path"] == "/api/history/SPY?interval=5m"
    assert len(bars) == 1
    assert bars[0].close == Decimal("1.1")
    assert isinstance(bars[0].high, Decimal)
