"""MarketFeedHub：全进程只生成一份行情快照，所有消费者共享。"""
from __future__ import annotations

import asyncio

from app.market_feed import MarketFeedHub


def test_refresh_once_builds_one_shared_snapshot(monkeypatch):
    calls = []

    class Source:
        name = "watcher"
        _loop = None

        @staticmethod
        def feed_snapshot():
            calls.append("feed")
            return {"hbMs": 7, "ticks": [{"sym": "QQQ"}], "pool": []}

    source = Source()
    monkeypatch.setattr("app.market_feed.datasource.current_source", lambda: source)
    hub = MarketFeedHub()

    asyncio.run(hub.refresh_once())
    first = hub.snapshot()
    second = hub.snapshot()

    assert calls == ["feed"]
    assert first == second
    assert first["ticks"] == [{"sym": "QQQ"}]
    assert first["source"] == "watcher"
    assert first["stale"] is False
    assert hub.generation == 1


def test_failed_refresh_retains_last_snapshot_and_marks_stale(monkeypatch):
    class Source:
        name = "watcher"
        _loop = None
        fail = False

        def feed_snapshot(self):
            if self.fail:
                raise RuntimeError("temporary feed failure")
            return {"hbMs": 0, "ticks": [{"sym": "SPY"}], "pool": []}

    source = Source()
    monkeypatch.setattr("app.market_feed.datasource.current_source", lambda: source)
    hub = MarketFeedHub()

    asyncio.run(hub.refresh_once())
    source.fail = True
    asyncio.run(hub.refresh_once())
    snap = hub.snapshot()

    assert snap["ticks"] == [{"sym": "SPY"}]
    assert snap["stale"] is True
    assert "temporary feed failure" in snap["feed_error"]
    assert hub.generation == 1


def test_source_switch_never_reuses_old_ticks(monkeypatch):
    class Source:
        _loop = None

        def __init__(self, name, sym):
            self.name = name
            self.sym = sym

        def feed_snapshot(self):
            return {"hbMs": 0, "ticks": [{"sym": self.sym}], "pool": []}

    current = {"source": Source("watcher", "QQQ")}
    monkeypatch.setattr(
        "app.market_feed.datasource.current_source",
        lambda: current["source"],
    )
    hub = MarketFeedHub()
    asyncio.run(hub.refresh_once())

    current["source"] = Source("real", "SPY")
    asyncio.run(hub.refresh_once())

    snap = hub.snapshot()
    assert snap["source"] == "real"
    assert snap["ticks"] == [{"sym": "SPY"}]
    assert all(tick["sym"] != "QQQ" for tick in snap["ticks"])
