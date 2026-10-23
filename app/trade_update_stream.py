"""Alpaca 交易账户事件流：TradingStream WS 为主，REST 定时对账为兜底。

这里只接收 ``trade_updates``，不负责下单或撤单。动作仍由 ``AlpacaTrading``
通过 Trading REST 完成；本模块把成交、部分成交、撤单、拒单等事件规范化后放入
线程安全的最新状态缓存，并唤醒 watchdog 立即消费。

alpaca-py 0.43 的 ``TradingStream`` 仍使用不支持 SOCKS5 的 legacy websocket
连接器，因此子类只覆写传输连接：协议、认证、订阅和 TradeUpdate 模型仍全部复用
官方 ``TradingStream``，底层连接则与期权行情流一样显式走 SOCKS5h，禁止静默直连。
"""
from __future__ import annotations

import asyncio
import json
import math
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from app.config import ALPACA_TRADING_STREAM_URL
from app.core.dailylogger import get_logger
from app.integrations.alpaca.client import (
    alpaca_proxy_url,
    is_loopback_url,
    read_keys,
    trading_stream_endpoint_override,
)
from app.core.market_session import order_session_window

logger = get_logger("tt.trade_stream")

_CACHE_LIMIT = 1000
_BACKOFF_MAX_S = 10.0


def _value(value: Any) -> Any:
    """Enum/Pydantic 字段统一取 wire value。"""
    return getattr(value, "value", value)


def _field(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _text(value: Any) -> str | None:
    value = _value(value)
    return None if value is None else str(value)


def _integer(value: Any) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


@dataclass(frozen=True)
class TradeOrderUpdate:
    """watchdog 所需的最小交易事件，不保留账户凭据或完整券商响应。"""

    revision: int
    event: str
    order_id: str
    symbol: str
    side: str
    status: str
    order_qty: int
    filled_qty: int
    filled_avg_price: str | None
    position_qty: int | None
    timestamp: str
    submitted_at: str
    updated_at: str
    order_type: str | None
    limit_price: str | None
    client_order_id: str | None

    def as_order_dict(self) -> dict:
        """兼容现有 REST ``get_order`` 返回形状，并额外保留 WS event/revision。"""
        return {
            "id": self.order_id,
            "event": self.event,
            "status": self.status,
            "symbol": self.symbol,
            "side": self.side,
            "qty": str(self.order_qty),
            "filled_qty": str(self.filled_qty),
            "filled_avg_price": self.filled_avg_price,
            "position_qty": self.position_qty,
            "timestamp": self.timestamp,
            "submitted_at": self.submitted_at,
            "updated_at": self.updated_at,
            "order_type": self.order_type,
            "limit_price": self.limit_price,
            "client_order_id": self.client_order_id,
            "revision": self.revision,
            "source": "trading_stream",
        }


def normalize_trade_update(message: Any, *, revision: int = 0) -> TradeOrderUpdate | None:
    """把 alpaca-py ``TradeUpdate``（或等价测试 dict）压成稳定的内部形状。"""
    if isinstance(message, dict) and message.get("stream") == "trade_updates":
        message = message.get("data") or {}
    order = _field(message, "order")
    if order is None:
        return None
    order_id = _text(_field(order, "id"))
    if not order_id:
        return None

    event = (_text(_field(message, "event")) or "").lower()
    status = (_text(_field(order, "status")) or "").lower()
    # event 比订单模型更及时；某些帧中 order.status 仍可能落后一拍。
    if event == "fill":
        status = "filled"
    elif event == "partial_fill":
        status = "partially_filled"
    elif event in {"canceled", "expired", "rejected"}:
        status = event

    timestamp = _iso_timestamp(_field(message, "timestamp"))
    submitted_at = _iso_timestamp(
        _field(order, "submitted_at") or _field(order, "created_at") or timestamp
    )
    updated_at = _iso_timestamp(_field(order, "updated_at") or timestamp)
    position_qty_raw = _field(message, "position_qty")
    return TradeOrderUpdate(
        revision=revision,
        event=event,
        order_id=order_id,
        symbol=(_text(_field(order, "symbol")) or "").upper(),
        side=(_text(_field(order, "side")) or "").lower(),
        status=status,
        order_qty=_integer(_field(order, "qty")),
        filled_qty=_integer(_field(order, "filled_qty")),
        filled_avg_price=_text(_field(order, "filled_avg_price")),
        position_qty=None if position_qty_raw is None else _integer(position_qty_raw),
        timestamp=timestamp,
        submitted_at=submitted_at,
        updated_at=updated_at,
        order_type=(
            _text(_field(order, "order_type", _field(order, "type"))) or ""
        ).lower() or None,
        limit_price=_text(_field(order, "limit_price")),
        client_order_id=_text(_field(order, "client_order_id")),
    )


def _iso_timestamp(value: Any) -> str:
    if isinstance(value, datetime):
        return (
            value.replace(tzinfo=timezone.utc)
            if value.tzinfo is None
            else value.astimezone(timezone.utc)
        ).isoformat()
    text = _text(value)
    if text:
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            return (
                parsed.replace(tzinfo=timezone.utc)
                if parsed.tzinfo is None
                else parsed.astimezone(timezone.utc)
            ).isoformat()
        except ValueError:
            return text
    return datetime.now(timezone.utc).isoformat()


class TradeUpdateStream:
    """TradingStream 生命周期、连接状态与最新订单事件缓存。"""

    def __init__(
        self,
        *,
        client_factory: Callable | None = None,
    ) -> None:
        self._client_factory = client_factory
        self._client = None
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._updates: OrderedDict[str, TradeOrderUpdate] = OrderedDict()
        self._revision = 0
        self._last_prune_at = 0.0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._activity: asyncio.Event | None = None
        self._listeners: set[Callable[[TradeOrderUpdate], None]] = set()
        self.connected = False
        self.connection_generation = 0
        self.last_event_at: str | None = None
        self.last_error: str | None = None
        self.transport = "socks5h"

    def _set_connected(self, connected: bool) -> None:
        with self._condition:
            changed = connected != self.connected
            self.connected = connected
            if connected and changed:
                self.connection_generation += 1
                self.last_error = None
            if changed:
                self._condition.notify_all()
        if changed:
            self._wake()

    def _wake(self) -> None:
        loop, event = self._loop, self._activity
        if loop is None or event is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(event.set)

    def subscribe(
        self,
        listener: Callable[[TradeOrderUpdate], None],
    ) -> Callable[[], None]:
        """注册规范化订单事件消费者，并返回取消订阅函数。"""
        with self._lock:
            self._listeners.add(listener)

        def unsubscribe() -> None:
            with self._lock:
                self._listeners.discard(listener)

        return unsubscribe

    async def handle_update(self, message: Any) -> None:
        """TradingStream handler；只规范化、写缓存并通知消费者。"""
        with self._condition:
            self._revision += 1
            update = normalize_trade_update(message, revision=self._revision)
            if update is None:
                return
            self._updates[update.order_id] = update
            self._updates.move_to_end(update.order_id)
            now = time.monotonic()
            if len(self._updates) > _CACHE_LIMIT or now - self._last_prune_at >= 60.0:
                self._prune_locked()
                self._last_prune_at = now
            while len(self._updates) > _CACHE_LIMIT:
                self._updates.popitem(last=False)
            self.last_event_at = update.timestamp
            listeners = tuple(self._listeners)
            self._condition.notify_all()
        for listener in listeners:
            try:
                listener(update)
            except Exception as exc:  # noqa: BLE001 — 消费者故障不能中断行情流
                logger.warning(json.dumps({
                    "event": "trade_stream_listener_error",
                    "order_id": update.order_id,
                    "error": str(exc),
                }, ensure_ascii=False))
        self._wake()

    def _prune_locked(self) -> None:
        window = order_session_window()
        start = window["session_start"]
        end = window["session_end"]
        stale = []
        for order_id, update in self._updates.items():
            if update.status in {
                "accepted", "accepted_for_bidding", "calculated", "held", "new",
                "partially_filled", "pending_cancel", "pending_new", "pending_replace",
                "stopped",
            }:
                continue
            try:
                updated_at = datetime.fromisoformat(update.updated_at.replace("Z", "+00:00"))
            except ValueError:
                continue
            if updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=timezone.utc)
            if not (start <= updated_at.astimezone(timezone.utc) < end):
                stale.append(order_id)
        for order_id in stale:
            self._updates.pop(order_id, None)

    def order(self, order_id: str) -> dict | None:
        with self._lock:
            update = self._updates.get(str(order_id))
            return None if update is None else update.as_order_dict()

    def order_revision(self, order_id: str) -> int:
        with self._lock:
            update = self._updates.get(str(order_id))
            return 0 if update is None else update.revision

    def wait_for_order(
        self,
        order_id: str,
        *,
        after_revision: int = 0,
        timeout_s: float = 1.0,
    ) -> dict | None:
        """供 ``asyncio.to_thread`` 中的动作流程等待新 WS 终态，不阻塞事件循环。"""
        order_id = str(order_id)
        with self._condition:
            self._condition.wait_for(
                lambda: (
                    (
                        order_id in self._updates
                        and self._updates[order_id].revision > after_revision
                    )
                    or not self.connected
                ),
                timeout=max(0.0, timeout_s),
            )
            update = self._updates.get(order_id)
            if update is None or update.revision <= after_revision:
                return None
            return update.as_order_dict()

    def orders_snapshot(self, order_ids: set[str] | None = None) -> dict[str, dict]:
        with self._lock:
            if order_ids is None:
                items = self._updates.items()
            else:
                items = (
                    (order_id, self._updates[order_id])
                    for order_id in order_ids
                    if order_id in self._updates
                )
            return {order_id: update.as_order_dict() for order_id, update in items}

    async def wait_activity(self, timeout_s: float = 1.0) -> bool:
        """等待新交易事件或连接状态变化；超时仍让 watchdog 执行止损拍。"""
        event = self._activity
        if event is None:
            await asyncio.sleep(timeout_s)
            return False
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout_s)
            return True
        except asyncio.TimeoutError:
            return False

    def clear_activity(self) -> None:
        if self._activity is not None:
            self._activity.clear()

    def status_snapshot(self) -> dict:
        window = order_session_window()
        with self._lock:
            return {
                "connected": self.connected,
                "pong_ms": self._pong_latency_ms(),
                "connection_generation": self.connection_generation,
                "cached_orders": len(self._updates),
                "last_event_at": self.last_event_at,
                "last_error": self.last_error,
                "session_date": window["session_date"],
                "session_start": window["session_start"].isoformat(),
                "session_end": window["session_end"].isoformat(),
                "timezone": window["timezone"],
                "transport": self.transport,
                "channel": "trade_updates",
            }

    def _pong_latency_ms(self) -> int | None:
        """返回后端到 Alpaca TradingStream 最近一次 WS Ping/Pong RTT。

        ``websockets`` 已按 ``ping_interval`` 自动发送协议层 Ping 并把匹配 Pong 的
        往返耗时写入连接 ``latency``；这里仅暴露读数，不另建连接、不额外发请求。
        首次 Pong 尚未到达或连接已断开时返回 ``None``。
        """
        if not self.connected:
            return None
        client = self._client
        ws = getattr(client, "_ws", None)
        try:
            latency_s = float(getattr(ws, "latency", 0) or 0)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(latency_s) or latency_s <= 0:
            return None
        return max(1, round(latency_s * 1000))

    def clear(self) -> None:
        """测试/重启辅助：清事件缓存，不改连接。"""
        with self._lock:
            self._updates.clear()
            self._revision = 0
            self._last_prune_at = 0.0
            self.last_event_at = None

    def _build_client(self):
        if self._client_factory is not None:
            return self._client_factory(self._set_connected)

        import websockets
        from alpaca.trading.stream import TradingStream

        endpoint = ALPACA_TRADING_STREAM_URL or trading_stream_endpoint_override()
        proxy = None if is_loopback_url(endpoint) else alpaca_proxy_url()
        self.transport = "direct" if proxy is None else "socks5h"
        owner = self

        class _Socks5TradingStream(TradingStream):
            def __init__(self, *args, state_handler: Callable[[bool], None], **kwargs):
                self._state_handler = state_handler
                self._proxy = proxy
                super().__init__(*args, **kwargs)

            async def _connect(self):
                endpoint = _value(self._endpoint)
                self._ws = await websockets.connect(
                    endpoint,
                    proxy=self._proxy,
                    **self._websocket_params,
                )

            async def _start_ws(self):
                await super()._start_ws()
                self._state_handler(True)

            async def close(self) -> None:
                try:
                    await super().close()
                finally:
                    self._state_handler(False)

            async def run_async(self) -> None:
                """SDK ``run`` 的 async/可取消版本，增加有上限的断线退避。"""
                self._loop = asyncio.get_running_loop()
                while not self._trade_updates_handler:
                    await asyncio.sleep(0.1)
                self._should_run = True
                backoff = 1.0
                while self._should_run:
                    try:
                        await self._start_ws()
                        self._running = True
                        backoff = 1.0
                        await self._consume()
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:  # noqa: BLE001 — 任意网络/认证错误均走同一重连边界
                        owner.last_error = str(exc)
                        self._state_handler(False)
                        logger.warning(json.dumps({
                            "event": "trade_stream_connection_error",
                            "error": str(exc),
                            "retry_s": backoff,
                        }, ensure_ascii=False))
                    finally:
                        await self.close()
                    if self._should_run:
                        await asyncio.sleep(backoff)
                        backoff = min(backoff * 2, _BACKOFF_MAX_S)

        key, secret = read_keys()
        kwargs = {
            "paper": True,
            "raw_data": False,
            "websocket_params": {
                "ping_interval": 10,
                "ping_timeout": 30,
                "max_queue": 1024,
                "open_timeout": 10,
            },
            "state_handler": self._set_connected,
        }
        if endpoint:
            kwargs["url_override"] = endpoint
        return _Socks5TradingStream(key, secret, **kwargs)

    async def run_forever(self) -> None:
        """FastAPI lifespan 后台任务；连接、订阅、断线重连均在此任务内完成。"""
        self._loop = asyncio.get_running_loop()
        self._activity = asyncio.Event()
        client = None
        try:
            client = self._build_client()
            self._client = client
            client.subscribe_trade_updates(self.handle_update)
            logger.info(json.dumps({
                "event": "trade_stream_start",
                "proxy_mode": self.transport,
                "channel": "trade_updates",
            }, ensure_ascii=False))
            run_async = getattr(client, "run_async", None)
            if run_async is None:
                await client._run_forever()  # alpaca-py 测试替身/向后兼容
            else:
                await run_async()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
            logger.warning(json.dumps(
                {"event": "trade_stream_error", "error": str(exc)},
                ensure_ascii=False,
            ))
        finally:
            self._set_connected(False)
            if client is not None:
                try:
                    await client.stop_ws()
                except Exception:  # noqa: BLE001
                    pass
                try:
                    await client.close()
                except Exception:  # noqa: BLE001
                    pass
            self._client = None


TRADE_UPDATE_STREAM = TradeUpdateStream()
