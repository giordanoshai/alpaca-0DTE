"""交易终端 /ws 实时状态 WebSocket 路由。

后台每 0.5 秒检查一次共享状态；仅有 section 变化时才向前端发送版本化帧。
断开 → 前端触发「心跳丢失」红色横幅。
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.services.market_feed import MARKET_FEED
from app.services.option_candidates import OPTION_CANDIDATES
from app.trading_state import TRADING_STATE

router = APIRouter()

_ET = ZoneInfo("America/New_York")

_SCHEMA_VERSION = 2
_QUEUE_LIMIT = 32
_TRADING_SECTIONS = ("orders", "positions", "risk", "broker_sync")
_MISSING = object()


class TradingWsHub:
    """共享状态编码器：单次序列化，多个客户端各自异步发送。"""

    def __init__(self, *, interval_s: float = 0.5, queue_limit: int = _QUEUE_LIMIT) -> None:
        self.interval_s = interval_s
        self.queue_limit = queue_limit
        self._subscribers: set[asyncio.Queue[str]] = set()
        self._sections: dict[str, Any] = {}
        self._source_tokens: dict[str, Any] = {}
        self._revisions = {
            "feed": 0,
            "orders": 0,
            "positions": 0,
            "risk": 0,
            "broker_sync": 0,
            "options": 0,
            "walls": 0,
            "tide": 0,
        }
        self._seq = 0
        self._full_cache_seq = -1
        self._full_cache_payload: str | None = None

    @staticmethod
    def _serialize(frame: dict) -> str:
        return json.dumps(frame, ensure_ascii=False, separators=(",", ":"))

    def _capture(self) -> tuple[dict[str, Any], dict[str, Any]]:
        feed = MARKET_FEED.snapshot()
        trading = TRADING_STATE.snapshot()
        trading_revisions = trading.get("revisions") or {}
        options = OPTION_CANDIDATES.snapshot()
        from app.services.gex_wall_service import snapshot as walls_snapshot, generation as walls_gen
        walls = walls_snapshot()
        # 0DTE 资金潮徽章：与距墙同一形态的「纯展示 section」—— 服务自己维护状态，
        # 这里只读快照，不发请求、不做累加（market_tide 的统一出口约定）。
        # `with_series=False`：顶栏徽章只显示当前净额，那 390 个点的走势数组给了
        # 也没人画（主交易页没引图表库），白占每一帧的带宽。
        from app.services.market_tide_service import current_accumulator, tide_snapshot
        tide = tide_snapshot(current_accumulator(), datetime.now(_ET), with_series=False)
        sections = {
            "feed": feed,
            "orders": trading.get("orders"),
            "positions": trading.get("positions"),
            "risk": trading.get("risk"),
            "broker_sync": trading.get("broker_sync"),
            "options": options,
            "walls": walls,
            "tide": tide,
        }
        tokens = {
            # generation 覆盖成功刷新，id 覆盖失败时 stale/error 快照的原子替换。
            "feed": (MARKET_FEED.generation, id(feed)),
            **{
                name: trading_revisions.get(name, 0)
                for name in _TRADING_SECTIONS
            },
            # 候选服务暂不暴露 generation；使用结构比较，仍避免 JSON 编解码。
            "options": options,
            # 距墙用 generation 当 token：比深比较整个 dict 便宜，
            # 且服务保证每轮更新后必 +1。
            "walls": walls_gen(),
            # ⚠️ **必须挑字段，不能拿整个 tide 当 token**：`tide_snapshot` 里的 `ts`
            # 每次 capture 都是新值（0.5s 一次），整体比较等于每 0.5 秒推一帧
            # tide patch —— 而这个值每分钟才真的变一次。`label` 由 diff+direction
            # 派生，不必入 token。
            "tide": (tide["diff"], tide["direction"], tide["seeded"], tide["session"]),
        }
        return sections, tokens

    def _frame(self, frame_type: str, changed: set[str] | None = None) -> dict:
        selected = set(self._revisions) if changed is None else changed
        frame: dict[str, Any] = {
            "schema_version": _SCHEMA_VERSION,
            "type": frame_type,
            "seq": self._seq,
            "revisions": dict(self._revisions),
        }
        if "feed" in selected and "feed" in self._sections:
            frame.update(self._sections["feed"])
        trading = {
            name: self._sections[name]
            for name in _TRADING_SECTIONS
            if name in selected and name in self._sections
        }
        if trading:
            frame["trading"] = trading
        if "options" in selected and "options" in self._sections:
            frame["options"] = self._sections["options"]
        if "walls" in selected and "walls" in self._sections:
            frame["walls"] = self._sections["walls"]
        if "tide" in selected and "tide" in self._sections:
            frame["tide"] = self._sections["tide"]
        return frame

    def _advance(self) -> str | None:
        sections, tokens = self._capture()
        changed: set[str] = set()
        for name, value in sections.items():
            token = tokens[name]
            previous = self._source_tokens.get(name, _MISSING)
            if previous is _MISSING or previous != token:
                self._sections[name] = value
                self._source_tokens[name] = token
                self._revisions[name] += 1
                changed.add(name)
        if not changed:
            return None
        self._seq += 1
        self._full_cache_seq = -1
        self._full_cache_payload = None
        return self._serialize(self._frame("patch", changed))

    def _snapshot_payload(self) -> str:
        if not self._sections:
            self._advance()
        if self._full_cache_payload is None or self._full_cache_seq != self._seq:
            self._full_cache_payload = self._serialize(self._frame("snapshot"))
            self._full_cache_seq = self._seq
        return self._full_cache_payload

    @staticmethod
    def _drain(queue: asyncio.Queue[str]) -> None:
        while True:
            try:
                queue.get_nowait()
                queue.task_done()
            except asyncio.QueueEmpty:
                return

    def _broadcast(self, payload: str) -> None:
        full_payload: str | None = None
        for queue in tuple(self._subscribers):
            if queue.full():
                self._drain(queue)
                if full_payload is None:
                    full_payload = self._snapshot_payload()
                queue.put_nowait(full_payload)
            else:
                queue.put_nowait(payload)

    def subscribe(self) -> asyncio.Queue[str]:
        pending = self._advance()
        if pending is not None:
            self._broadcast(pending)
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=self.queue_limit)
        self._subscribers.add(queue)
        queue.put_nowait(self._snapshot_payload())
        return queue

    def unsubscribe(self, queue: asyncio.Queue[str]) -> None:
        self._subscribers.discard(queue)
        self._drain(queue)

    async def run(self) -> None:
        while True:
            payload = self._advance()
            if payload is not None and self._subscribers:
                self._broadcast(payload)
            await asyncio.sleep(self.interval_s)


TRADING_WS_HUB = TradingWsHub()


@router.websocket("/ws")
async def ws_feed(ws: WebSocket) -> None:
    """版本化实时推送：首帧全量，之后只推发生变化的 section。

    前端把 ticks 合并进 TICKERS 让数字跳动；连接断开 → 前端触发
    「心跳丢失」红色横幅（真实反映数据通路存活）。

    断开检测：Windows proactor 下向半关闭 socket send 不抛可捕获异常、只被底层
    刷「socket.send() raised exception.」日志，致 while-send 永不退出（每秒刷屏 + 阻塞优雅关停）。
    故另起 receive 循环主动感知 websocket.disconnect，一断即取消推送 pump。
    """
    await ws.accept()
    send_lock = asyncio.Lock()
    queue = TRADING_WS_HUB.subscribe()

    async def _send_text(payload: str) -> None:
        # 行情泵与浏览器心跳 Pong 共用同一发送锁，避免并发写同一 WebSocket。
        async with send_lock:
            await ws.send_text(payload)

    async def _writer() -> None:
        try:
            while True:
                payload = await queue.get()
                try:
                    await _send_text(payload)
                finally:
                    queue.task_done()
        except (WebSocketDisconnect, RuntimeError, ConnectionError, asyncio.CancelledError):
            pass

    writer = asyncio.create_task(_writer())
    try:
        while True:
            message = await ws.receive()  # 无消息时阻塞；浏览器每 5s 发应用层 Ping
            if message.get("type") == "websocket.disconnect":
                break
            text = message.get("text")
            if not text:
                continue
            try:
                payload = json.loads(text)
            except (TypeError, ValueError):
                continue
            if isinstance(payload, dict) and payload.get("type") == "ping":
                pong = json.dumps(
                    {"type": "pong", "id": payload.get("id")},
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                await _send_text(pong)
    except (WebSocketDisconnect, RuntimeError, ConnectionError):
        pass
    finally:
        TRADING_WS_HUB.unsubscribe(queue)
        writer.cancel()
        await asyncio.gather(writer, return_exceptions=True)
