"""券商订单事件消费服务。

``trade_update_stream`` 只负责传输与规范化；本服务订阅事件、更新订单读模型并落库。
"""
from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable

from app.core.dailylogger import get_logger
from app.order_journal import JOURNAL, OrderJournal
from app.trade_update_stream import (
    TRADE_UPDATE_STREAM,
    TradeOrderUpdate,
    TradeUpdateStream,
)

logger = get_logger("tt.order_sync")

_QUEUE_LIMIT = 1024
_WARNING_INTERVAL_S = 5.0


class OrderSyncService:
    """异步消费券商订单事件，并按订单合并数据库写入。"""

    def __init__(
        self,
        stream: TradeUpdateStream,
        journal: OrderJournal,
        *,
        queue_limit: int = _QUEUE_LIMIT,
    ) -> None:
        self._stream = stream
        self._journal = journal
        self._queue_limit = queue_limit
        self._queue: asyncio.Queue[TradeOrderUpdate] | None = None
        self._unsubscribe: Callable[[], None] | None = None
        self._resync_needed = False
        self._dropped = 0
        self._unavailable = 0
        self._coalesced = 0
        self._last_warning_at = 0.0

    def _warn(self, event: str, order_id: str) -> None:
        now = time.monotonic()
        if now - self._last_warning_at < _WARNING_INTERVAL_S:
            return
        self._last_warning_at = now
        queue = self._queue
        logger.warning(json.dumps({
            "event": event,
            "order_id": order_id,
            "queue_depth": queue.qsize() if queue is not None else 0,
            "queue_limit": self._queue_limit,
            "dropped": self._dropped,
            "unavailable": self._unavailable,
        }, ensure_ascii=False))

    def enqueue(self, update: TradeOrderUpdate) -> None:
        """非阻塞入队；溢出时标记用缓存全量重同步。"""
        queue = self._queue
        if queue is None:
            self._unavailable += 1
            self._resync_needed = True
            self._warn("order_sync_queue_unavailable", update.order_id)
            return
        try:
            queue.put_nowait(update)
        except asyncio.QueueFull:
            self._dropped += 1
            self._resync_needed = True
            self._warn("order_sync_queue_full", update.order_id)

    def _drain_batch(self, first: TradeOrderUpdate) -> list[TradeOrderUpdate]:
        queue = self._queue
        if queue is None:
            return [first]
        batch = [first]
        while True:
            try:
                batch.append(queue.get_nowait())
            except asyncio.QueueEmpty:
                return batch

    async def _apply_batch(self, batch: list[TradeOrderUpdate]) -> None:
        latest: dict[str, TradeOrderUpdate] = {}
        for update in batch:
            previous = latest.get(update.order_id)
            if previous is not None:
                self._coalesced += 1
            if previous is None or update.revision > previous.revision:
                latest[update.order_id] = update

        if self._resync_needed:
            orders = list(self._stream.orders_snapshot().values())
            self._resync_needed = False
        else:
            orders = [update.as_order_dict() for update in latest.values()]
        if not orders:
            return

        self._journal.upsert_broker_orders(orders, persist=False)
        order_ids = {
            str(order.get("id") or order.get("order_id") or "")
            for order in orders
        }
        for order_id in order_ids:
            if not order_id:
                continue
            try:
                await asyncio.to_thread(self._journal.persist, order_id)
            except Exception as exc:  # noqa: BLE001 — 单笔失败不阻断后续订单
                logger.warning(json.dumps({
                    "event": "order_sync_persist_error",
                    "order_id": order_id,
                    "error": str(exc),
                }, ensure_ascii=False))

    async def run_forever(self) -> None:
        """FastAPI lifespan 后台任务。"""
        self._queue = asyncio.Queue(maxsize=self._queue_limit)
        self._unsubscribe = self._stream.subscribe(self.enqueue)
        try:
            while True:
                first = await self._queue.get()
                batch = self._drain_batch(first)
                try:
                    await self._apply_batch(batch)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 — 消费服务必须自愈
                    self._resync_needed = True
                    logger.warning(json.dumps({
                        "event": "order_sync_batch_error",
                        "error": str(exc),
                    }, ensure_ascii=False))
                finally:
                    for _item in batch:
                        self._queue.task_done()
        finally:
            if self._unsubscribe is not None:
                self._unsubscribe()
            self._unsubscribe = None
            self._queue = None

    async def wait_ready(self, timeout_s: float = 2.0) -> bool:
        """等待消费队列完成创建与订阅，供传输流启动前建立顺序屏障。"""
        async def _wait() -> None:
            while self._queue is None or self._unsubscribe is None:
                await asyncio.sleep(0)

        try:
            await asyncio.wait_for(_wait(), timeout=max(0.0, timeout_s))
            return True
        except asyncio.TimeoutError:
            return False

    async def wait_idle(self, timeout_s: float = 2.0) -> bool:
        queue = self._queue
        if queue is None:
            return True
        try:
            await asyncio.wait_for(queue.join(), timeout=max(0.0, timeout_s))
            return True
        except asyncio.TimeoutError:
            return False

    def status_snapshot(self) -> dict:
        queue = self._queue
        return {
            "persist_queue_depth": queue.qsize() if queue is not None else 0,
            "persist_queue_limit": self._queue_limit,
            "persist_queue_dropped": self._dropped,
            "persist_queue_unavailable": self._unavailable,
            "persist_queue_coalesced": self._coalesced,
            "persist_resync_needed": self._resync_needed,
        }


ORDER_SYNC = OrderSyncService(TRADE_UPDATE_STREAM, JOURNAL)
