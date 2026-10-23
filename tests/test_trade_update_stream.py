"""Alpaca TradingStream 事件规范化与 watchdog WS 主路径。"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal as D
from types import SimpleNamespace

import app.trade_update_stream as trade_update_stream
import app.watchdog as watchdog
from app.order_journal import OrderJournal
from app.order_state import JOURNAL, REGISTRY, ManagedPosition
from app.services import order_sync as order_sync_module
from app.services.order_sync import OrderSyncService
from app.trade_update_stream import TradeUpdateStream, normalize_trade_update

_CODE = "SPXW260727C06400000"


def test_mock_trade_stream_is_derived_and_bypasses_proxy(monkeypatch):
    monkeypatch.setattr(trade_update_stream, "ALPACA_TRADING_STREAM_URL", "")
    monkeypatch.setattr(
        trade_update_stream,
        "trading_stream_endpoint_override",
        lambda: "ws://127.0.0.1:9000/stream",
    )
    monkeypatch.setattr(trade_update_stream, "read_keys", lambda: ("mock", "mock"))
    monkeypatch.setattr(
        trade_update_stream,
        "alpaca_proxy_url",
        lambda: (_ for _ in ()).throw(AssertionError("本地 MOCK 不应读取代理")),
    )
    stream = TradeUpdateStream()

    client = stream._build_client()

    assert str(client._endpoint) == "ws://127.0.0.1:9000/stream"
    assert client._proxy is None
    assert stream.status_snapshot()["transport"] == "direct"


def test_status_snapshot_reports_alpaca_websocket_pong_latency():
    stream = TradeUpdateStream()
    stream._client = SimpleNamespace(_ws=SimpleNamespace(latency=0.210021))
    stream.connected = True

    assert stream.status_snapshot()["pong_ms"] == 210

    stream.connected = False
    assert stream.status_snapshot()["pong_ms"] is None


def _wire(event: str, status: str, *, filled_qty: str = "0", price=None) -> dict:
    return {
        "stream": "trade_updates",
        "data": {
            "event": event,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "position_qty": filled_qty,
            "order": {
                "id": "order-1",
                "symbol": _CODE,
                "side": "buy",
                "status": status,
                "qty": "2",
                "filled_qty": filled_qty,
                "filled_avg_price": price,
            },
        },
    }


def test_normalize_trade_update_prefers_event_terminal_status():
    update = normalize_trade_update(
        _wire("fill", "partially_filled", filled_qty="2", price="5.25"),
        revision=7,
    )

    assert update is not None
    assert update.revision == 7
    assert update.status == "filled"
    assert update.filled_qty == 2
    assert update.filled_avg_price == "5.25"


def test_stream_caches_latest_update_by_order():
    stream = TradeUpdateStream()
    received = []
    unsubscribe = stream.subscribe(received.append)
    asyncio.run(stream.handle_update(_wire("partial_fill", "new", filled_qty="1", price="5.10")))
    asyncio.run(stream.handle_update(_wire("fill", "filled", filled_qty="2", price="5.20")))
    unsubscribe()

    order = stream.order("order-1")
    assert order is not None
    assert order["revision"] == 2
    assert order["status"] == "filled"
    assert order["filled_qty"] == "2"
    assert stream.status_snapshot()["cached_orders"] == 1
    assert [update.revision for update in received] == [1, 2]
    # 传输层不再直接写业务账本；由 OrderSyncService 订阅后处理。
    assert JOURNAL.get("order-1") is None
    status = stream.status_snapshot()
    assert set(status) >= {
        "session_date", "session_start", "session_end", "timezone",
    }
    assert status["timezone"] == "America/New_York"


def test_stream_handler_only_enqueues_persistence(monkeypatch):
    """慢数据库写不能阻塞 SDK 顺序消费后续 trade_updates。"""
    stream = TradeUpdateStream()
    journal = OrderJournal(persist=False)
    sync = OrderSyncService(stream, journal, queue_limit=4)

    async def run():
        called = []
        monkeypatch.setattr(
            journal,
            "persist",
            lambda order_id: called.append(order_id),
        )
        task = asyncio.create_task(sync.run_forever())
        assert await sync.wait_ready()
        await stream.handle_update(
            _wire("fill", "filled", filled_qty="2", price="5.20"))
        assert called == []
        assert await sync.wait_idle()
        assert called == ["order-1"]
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


def test_persist_queue_is_bounded_and_reports_full_drop(monkeypatch):
    """DB 消费跟不上时队列有硬上限；WS handler 不等待，只计数并告警。"""
    stream = TradeUpdateStream()
    sync = OrderSyncService(stream, OrderJournal(persist=False), queue_limit=1)
    warnings = []
    monkeypatch.setattr(
        order_sync_module.logger,
        "warning",
        lambda message: warnings.append(message),
    )

    async def run():
        sync._queue = asyncio.Queue(maxsize=1)
        unsubscribe = stream.subscribe(sync.enqueue)
        await stream.handle_update(
            _wire("partial_fill", "partially_filled", filled_qty="1"))
        second = _wire("fill", "filled", filled_qty="2", price="5.20")
        second["data"]["order"]["id"] = "order-2"
        await stream.handle_update(second)

        assert sync._queue.qsize() == 1
        snapshot = sync.status_snapshot()
        assert snapshot["persist_queue_limit"] == 1
        assert snapshot["persist_queue_dropped"] == 1
        assert any("order_sync_queue_full" in item for item in warnings)
        unsubscribe()

    asyncio.run(run())


def test_persist_queue_coalesces_same_order_and_replays_latest(monkeypatch):
    """同一批订单的高频累计帧按 revision 合并，只持久化最新状态。"""
    stream = TradeUpdateStream()
    journal = OrderJournal(persist=False)
    sync = OrderSyncService(stream, journal, queue_limit=2)
    persisted = []
    monkeypatch.setattr(
        journal,
        "persist",
        lambda order_id: persisted.append(order_id),
    )

    async def run():
        task = asyncio.create_task(sync.run_forever())
        assert await sync.wait_ready()
        await stream.handle_update(
            _wire("partial_fill", "partially_filled", filled_qty="1"))
        await stream.handle_update(
            _wire("fill", "filled", filled_qty="2", price="5.20"))
        assert await sync.wait_idle()
        assert sync.status_snapshot()["persist_queue_coalesced"] == 1
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        assert persisted == ["order-1"]
        assert journal.get("order-1").status == "filled"
        assert journal.get("order-1").price == D("5.20")

    asyncio.run(run())


def test_missing_persist_queue_is_visible_degradation(monkeypatch):
    """非正常生命周期直接调用 handler 时仍更新内存，但必须留下可观测告警。"""
    stream = TradeUpdateStream()
    sync = OrderSyncService(stream, OrderJournal(persist=False), queue_limit=2)
    warnings = []
    monkeypatch.setattr(
        order_sync_module.logger,
        "warning",
        lambda message: warnings.append(message),
    )
    unsubscribe = stream.subscribe(sync.enqueue)
    asyncio.run(stream.handle_update(
        _wire("fill", "filled", filled_qty="2", price="5.20")))
    unsubscribe()

    snapshot = sync.status_snapshot()
    assert snapshot["persist_queue_unavailable"] == 1
    assert any(
        "order_sync_queue_unavailable" in item
        for item in warnings
    )


def test_stream_enriches_known_filled_order_with_later_real_price():
    JOURNAL.submitted("order-1", "SPX", _CODE, "buy", 2, D("5.00"))
    JOURNAL.mark_filled("order-1", price=D("5.00"))
    stream = TradeUpdateStream()
    sync = OrderSyncService(stream, JOURNAL, queue_limit=2)

    async def run():
        task = asyncio.create_task(sync.run_forever())
        assert await sync.wait_ready()
        await stream.handle_update(
            _wire("fill", "filled", filled_qty="2", price="5.25"))
        assert await sync.wait_idle()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())

    assert JOURNAL.get("order-1").status == "filled"
    assert JOURNAL.get("order-1").price == D("5.25")


class _NoPollingBroker:
    def broker_position_qtys(self):
        raise AssertionError("WS 主路径不应读取持仓 REST")

    def get_order(self, _order_id):
        raise AssertionError("WS 主路径不应查询订单 REST")

    def cancel(self, _order_id):
        raise AssertionError("本用例不应撤单")


def _source():
    return SimpleNamespace(
        name="watcher",
        alpaca=SimpleNamespace(option_quote=lambda _code: None),
        underlying_price=lambda _ticker: None,
    )


def test_watchdog_applies_ws_fill_without_rest_polling(monkeypatch):
    pos = ManagedPosition(
        option_code=_CODE,
        ticker="SPX",
        direction="long",
        qty=2,
        entry_mid=D("5.00"),
        entry_time=datetime.now(timezone.utc),
        order_id="order-1",
        status="pending",
    )
    REGISTRY.record(pos)
    JOURNAL.submitted("order-1", "SPX", _CODE, "buy", 2, D("5.00"))
    monkeypatch.setattr(watchdog, "AlpacaTrading", _NoPollingBroker)
    recorded = {}
    monkeypatch.setattr(
        watchdog.trade_recorder,
        "record_filled",
        lambda oid, entry_mid=None, qty=None: recorded.update(
            order_id=oid, entry_mid=entry_mid, qty=qty,
        ),
    )
    ws_order = normalize_trade_update(
        _wire("fill", "filled", filled_qty="2", price="5.25"),
        revision=1,
    ).as_order_dict()

    watchdog._watchdog_tick(
        _source(),
        reconcile_rest=False,
        order_updates={"order-1": ws_order},
    )

    assert pos.status == "open"
    assert pos.entry_mid == D("5.25")
    assert pos.last_owned_buy_fill_at is not None
    assert JOURNAL.get("order-1").status == "filled"
    assert recorded == {"order_id": "order-1", "entry_mid": D("5.25"), "qty": 2}


def test_cached_entry_fill_does_not_overwrite_added_position(monkeypatch):
    """首单 ×1 的缓存终态不能反复把已经加仓到 ×5 的总持仓重置成 ×1。"""
    pos = ManagedPosition(
        option_code=_CODE,
        ticker="SPX",
        direction="long",
        qty=5,
        entry_mid=D("5.10"),
        entry_time=datetime.now(timezone.utc),
        order_id="order-1",
        status="open",
        entry_filled_qty=1,
        entry_fill_mid=D("5.00"),
    )
    REGISTRY.record(pos)
    JOURNAL.submitted("order-1", "SPX", _CODE, "buy", 1, D("5.00"))
    JOURNAL.mark_filled("order-1", price=D("5.00"))
    monkeypatch.setattr(watchdog, "AlpacaTrading", _NoPollingBroker)
    persisted = []
    monkeypatch.setattr(
        watchdog.trade_recorder,
        "record_position_sync",
        lambda *args, **kwargs: persisted.append((args, kwargs)),
    )
    cached_entry = normalize_trade_update(
        _wire("fill", "filled", filled_qty="1", price="5.00"),
        revision=1,
    ).as_order_dict()

    watchdog._watchdog_tick(
        _source(),
        reconcile_rest=False,
        order_updates={"order-1": cached_entry},
    )

    assert pos.qty == 5
    assert pos.entry_mid == D("5.10")
    assert persisted == []


def test_open_legacy_position_baselines_cached_entry_without_overwrite(monkeypatch):
    """升级前没有 entry_filled_qty 的 open 对象，首拍只建基线、不改当前总仓。"""
    pos = ManagedPosition(
        option_code=_CODE,
        ticker="SPX",
        direction="long",
        qty=5,
        entry_mid=D("5.10"),
        entry_time=datetime.now(timezone.utc),
        order_id="order-1",
        status="open",
    )
    REGISTRY.record(pos)
    monkeypatch.setattr(watchdog, "AlpacaTrading", _NoPollingBroker)
    cached_entry = normalize_trade_update(
        _wire("fill", "filled", filled_qty="1", price="5.00"),
        revision=1,
    ).as_order_dict()

    watchdog._watchdog_tick(
        _source(),
        reconcile_rest=False,
        order_updates={"order-1": cached_entry},
    )

    assert pos.qty == 5
    assert pos.entry_filled_qty == 1
    assert pos.entry_fill_mid == D("5.00")


def test_watchdog_applies_ws_cancel_without_order_query(monkeypatch):
    pos = ManagedPosition(
        option_code=_CODE,
        ticker="SPX",
        direction="long",
        qty=1,
        entry_mid=D("5.00"),
        entry_time=datetime.now(timezone.utc),
        order_id="order-1",
        status="pending",
    )
    REGISTRY.record(pos)
    JOURNAL.submitted("order-1", "SPX", _CODE, "buy", 1, D("5.00"))
    monkeypatch.setattr(watchdog, "AlpacaTrading", _NoPollingBroker)
    monkeypatch.setattr(watchdog.trade_recorder, "record_cancelled", lambda _oid: None)
    ws_order = normalize_trade_update(
        _wire("canceled", "new"),
        revision=1,
    ).as_order_dict()

    watchdog._watchdog_tick(
        _source(),
        reconcile_rest=False,
        order_updates={"order-1": ws_order},
    )

    assert pos.status == "cancelled"
    assert JOURNAL.get("order-1").status == "cancelled"


def test_ws_fill_updates_journal_after_registry_was_lost(monkeypatch):
    """热重启后只剩 JOURNAL pending 时，WS 终态也不能被 watchdog 丢掉。"""
    JOURNAL.submitted("order-1", "SPX", _CODE, "buy", 2, D("5.00"))
    monkeypatch.setattr(watchdog, "AlpacaTrading", _NoPollingBroker)
    recorded = {}
    monkeypatch.setattr(
        watchdog.trade_recorder,
        "record_filled",
        lambda oid, entry_mid=None, qty=None: recorded.update(
            order_id=oid, entry_mid=entry_mid, qty=qty,
        ),
    )
    ws_order = normalize_trade_update(
        _wire("fill", "filled", filled_qty="2", price="5.25"),
        revision=1,
    ).as_order_dict()

    watchdog._watchdog_tick(
        _source(),
        reconcile_rest=False,
        order_updates={"order-1": ws_order},
    )

    assert JOURNAL.get("order-1").status == "filled"
    assert recorded == {"order_id": "order-1", "entry_mid": D("5.25"), "qty": 2}


def test_cancel_after_partial_fill_keeps_filled_position(monkeypatch):
    """若 partial_fill 与 canceled 连续到达且缓存只保留末帧，已成交张数也不能被当成零。"""
    pos = ManagedPosition(
        option_code=_CODE,
        ticker="SPX",
        direction="long",
        qty=2,
        entry_mid=D("5.00"),
        entry_time=datetime.now(timezone.utc),
        order_id="order-1",
        status="pending",
    )
    REGISTRY.record(pos)
    JOURNAL.submitted("order-1", "SPX", _CODE, "buy", 2, D("5.00"))
    monkeypatch.setattr(watchdog, "AlpacaTrading", _NoPollingBroker)
    monkeypatch.setattr(watchdog.trade_recorder, "record_filled", lambda *_a, **_k: None)
    terminal = normalize_trade_update(
        _wire("canceled", "canceled", filled_qty="1", price="5.15"),
        revision=2,
    ).as_order_dict()

    watchdog._watchdog_tick(
        _source(),
        reconcile_rest=False,
        order_updates={"order-1": terminal},
    )

    assert pos.status == "open"
    assert pos.qty == 1
    assert pos.entry_mid == D("5.15")
    assert JOURNAL.get("order-1").status == "filled"
