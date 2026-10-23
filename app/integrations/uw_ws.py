# -*- coding: utf-8 -*-
"""UW (Unusual Whales) WebSocket 多 Channel 复用客户端。

⚠️ 2026-08-15 起本模块**没有默认启用的消费者**：market_tide 已迁回 REST 轮询，
唯一剩下的 `price:{ticker}` 支路要 `DATA_SOURCE=uw` 才起（默认 yahoo/mock）。
代码按用户决定保留待用，不是遗漏。

⚠️ 2026-08-17 曾短暂改直连又改回代理：两次各跑满 10 分钟的真实对照显示成功率
几乎打平（代理 90.7% vs 直连 90.3%），但直连成功请求延迟明显更高（中位数
2844ms vs 代理侧单轮 33 并发多在 5~8s 内跑完），代理留的余量更大，改回代理。

职责：
1. 按用途分组维持多条长连接（`UwWsPool`：`flood`/`global`/`ticker` 三组），组内复用
   TCP / SOCKS5 代理资源、避免同组内无谓地多开连接；
2. 支持动态添加/移除 Channel 订阅，消息自动按 Channel 派发；
3. 断线指数退避重连，并自动重新 `join` 恢复已订阅的所有 Channel；
4. 暴露极简的 `subscribe(channel)` 异步生成器接口给上层模块，内部经 `group_of()`
   路由到对应分组连接。

连接协议（官方文档 2026-08）：
    URL:     中转站 ticket 驱动（POST /socket-ticket 换 `websocket_url`）。
             官方直连的 `?token={KEY}` 写法**中转站不认**（实测 403），换不到票
             就退避重试换票，不拿它去连——见 `_get_ticket_ws_url`。
    订阅帧:  {"channel":"<name>","msg_type":"join"}
    响应帧:  ["<name>",{"response":{},"status":"ok"}]
    数据帧:  ["<name>", <payload>]
    错误帧:  {"error":"<code>","message":"<人类可读原因>"}   ← **dict 而非 list**

错误帧是唯一的 dict 形态下行帧，早期实现只解 list、把它整个丢掉了，导致超预算
这类硬失败表现为「订阅了但一直没数据」。实测超预算的原文是
`{"error":"subscription_limit","message":"Maximum 12 channels per connection."}`
——注意 **per connection**：12 条是每连接的额度，这正是分组多连接的立足点。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable, Dict, Set
from urllib.parse import urlparse

import requests
import websockets
from websockets.protocol import State

from app.config import UW_ENDPOINT, UW_API_KEY, SOCKS5_PROXY, HTTP_PROXY
from app.integrations.uw import proxy_url  # 复用 socks5h 规范化

logger = logging.getLogger("tt.uw_ws")

# ── 重连退避参数 ──────────────────────────────────────────────────────────────
_BACKOFF_INIT = 2.0
_BACKOFF_MAX = 60.0
_BACKOFF_FACTOR = 2.0
_QUEUE_MAXSIZE = 1000
# monitored 频道发出 join 后必须在有限时间内取得 ACK；否则这个连接上的订阅事实
# 不可信，主动断开并让既有 handle 随下一 connection generation 重新 join。
#
# **别再调回 5.0**：这个看门狗只负责抓「永远不 ACK 的死订阅」，不该充当上游慢速的
# 裁判。2026-08-13 中转站劣化当天实测（log/ttt.log 全天 132 次 join）：join→ACK
# 中位 4.55s、p90 13.45s、最大 15.50s，而这 132 次**全部**最终收到了 ACK——慢，
# 但一次都没真丢。当时阈值 5.0 正好压在中位数上，49% 的 join 触发它、全天误杀 17
# 次正常连接（03:25:25 发 join → 03:25:30 强拆 → 03:25:32 ACK 才到），而每次误杀
# 都强制重新换票（_TICKET_HTTP_TIMEOUT_S=15s），把「上游变慢」放大成「换票风暴」。
# 30.0 ≈ 实测最慢成功 ACK 的 2 倍；对健康期（p90 1.2~2.0s）仍留着 15 倍余量。
_MONITORED_ACK_TIMEOUT_S = 30.0

# monitored subscription 对上层暴露的控制事件。保持纯字符串，便于直接落日志/JSON，
# 也避免业务层为了判断状态被迫依赖本模块的 Enum 类型。
JOIN_PENDING = "JOIN_PENDING"
ACKED = "ACKED"
DISCONNECTED = "DISCONNECTED"
RECONNECTING = "RECONNECTING"
QUEUE_DROPPED = "QUEUE_DROPPED"
REJECTED = "REJECTED"
CLOSED = "CLOSED"

_PAYLOAD_END = object()
_CONTROL_END = object()
# 换票（POST /socket-ticket）的 HTTP 超时。**别再调回 5s**：实测走 socks5h 代理
# 空载单程就要 1.6s，5s 只有 3 倍余量，而启动瞬间 market_tide 播种、SOS screener
# 扫池、各组 WS 换票全挤在同一个本地 SOCKS 上，捅穿 5s 是常态。
# 本值与 market_tide 的 `_SEED_HTTP_TIMEOUT_S` 同源于那次实测；后者已于 2026-08-18
# 随 0DTE 切源提到 30.0，这里**没有跟着提** —— 换票是启动路径、慢就该早失败早重试。
_TICKET_HTTP_TIMEOUT_S = 15.0
# 中转站硬限制：**单连接最多订阅 12 个频道**。
# 2026-08-06 盘外实测确认，服务端超限时返回：
#   {"error":"subscription_limit","message":"Maximum 12 channels per connection."}
# 注意是 **per connection**——这是**每连接**预算，不是全局预算。全系统总预算 =
# 连接数 × 12（实测 3 条并发连接 24/24 频道全部 ACK、互不踢）。连接按用途分组，
# 见本文件的 GROUP_* 与 UwWsPool。
# 客户端仍做预检、超预算拒绝订阅：省一次必然失败的往返，并把错误说清楚。
MAX_CHANNELS = 12
# 全局连接硬顶，**跨组合计**。中转站限的是「这个 token 开了几条连接」而非按组算，
# 所以顶设在全局才对应真实约束对象。
# 2026-08-06 实测只验证过 3 条并发可用，8 是留了余量的保守取值、**不是实证结论**。
# 容量账：flood / global 各占 1 条 ⇒ ticker 组可用 6 条 × 12 = 72 只标的。
# 真实盯盘池 153 只装不进 UW 的 price 通道（需 15 条连接），到顶时明确报错拒绝，
# 提示改用 DATA_SOURCE=les/yahoo，绝不静默只订前 N 只。
MAX_CONNECTIONS = 8
# 连接活过这么久才算「连稳过」，才允许把退避重置回初值。低于它说明是「握手即断」
# （鉴权被拒最常见这形态），必须让退避继续涨，否则会以固定 2s 节奏无限重连。
_HEALTHY_UPTIME_S = 30.0

# ── 连接分组 ──────────────────────────────────────────────────────────────────
# 12 条限额是**每连接**的（2026-08-06 实测，服务端错误原文
# "Maximum 12 channels per connection."），所以按用途分组开多条连接，
# 既把 12 条预算扩成 N×12，也把洪水频道隔离开——`_run_loop` 的
# `async for raw_msg in ws` 是串行的，option_trades（约 427 条/秒均值）
# 和 market_tide 挤同一条连接等于拿全市场期权流去堵资金潮徽章。
GROUP_FLOOD = "flood"      # 高频洪水，独占连接
GROUP_GLOBAL = "global"    # 全局低频，一类一条
GROUP_TICKER = "ticker"    # 带 :TICKER 的按标的频道

_EXACT_GROUP: dict[str, str] = {
    "option_trades":     GROUP_FLOOD,
    "lit_trades":        GROUP_FLOOD,
    "off_lit_trades":    GROUP_FLOOD,
    "gex_strike":        GROUP_FLOOD,
    "gex_strike_expiry": GROUP_FLOOD,
    "market_tide":       GROUP_GLOBAL,
    "flow-alerts":       GROUP_GLOBAL,   # 连字符！全项目唯一一个，勿写成 flow_alerts
    "news":              GROUP_GLOBAL,
    "trading_halts":     GROUP_GLOBAL,
    "gex":               GROUP_GLOBAL,
    "interval_flow":     GROUP_GLOBAL,
    "contract_screener": GROUP_GLOBAL,
    "custom_alerts":     GROUP_GLOBAL,
    "periscope":         GROUP_GLOBAL,
}

# 带冒号但仍属洪水的：单标的期权流、单标的 GEX strike 更新在 0DTE 日流量依然巨大。
# `_EXACT_GROUP` 里的 gex_strike / gex_strike_expiry 是无标的的全局变体（若存在），
# 这里补的是带 `:TICKER` 的按标的变体（官方文档确认两个频道都有此变体）——不补的话
# `group_of("gex_strike:SPY")` 会兜底落进 ticker 组，跟 `price:{ticker}` 挤同一条连接，
# 「每个 strike 的实时 GEX 更新」正是高频推送，恰好是 flood 组存在的理由要防的场景。
_PREFIX_GROUP: dict[str, str] = {
    "option_trades:": GROUP_FLOOD,
    "gex_strike_expiry:": GROUP_FLOOD,
    "gex_strike:": GROUP_FLOOD,
}


def group_of(channel: str) -> str:
    """把频道名映射到连接组。未知频道兜底进标的组（那组预算最灵活）。"""
    exact = _EXACT_GROUP.get(channel)
    if exact is not None:
        return exact
    # 按前缀长度降序做「最长匹配优先」遍历，不依赖 dict 插入顺序：
    # `_PREFIX_GROUP` 里可能存在互为前缀关系的键（例如未来新增的细分频道），
    # 谁先匹配就不能取决于字典的插入序这种隐式约定——哪怕当前所有前缀都
    # 指向同一个组、换了顺序也测不出差异，这个耦合迟早会在新增频道时炸。
    for prefix, group in sorted(
        _PREFIX_GROUP.items(), key=lambda kv: len(kv[0]), reverse=True
    ):
        if channel.startswith(prefix):
            return group
    return GROUP_TICKER


def _is_open(ws) -> bool:
    """判连接是否可写。

    websockets 14+ 的 `ClientConnection` **没有** `.closed` 属性（旧的
    `WebSocketClientProtocol` 才有），直接读会抛 AttributeError；这里统一走
    `.state`，并对老实现保留回退分支。
    """
    state = getattr(ws, "state", None)
    if state is not None:
        return state is State.OPEN
    return not getattr(ws, "closed", True)


def _resolve_proxy() -> str | None:
    """解析代理 URL，复用 uw.py 的 socks5h 规范化逻辑。"""
    raw = SOCKS5_PROXY or HTTP_PROXY
    return proxy_url(raw)


class _TicketError(RuntimeError):
    """换票失败。中转站是 ticket 驱动的，拿不到票就没有能连的 URL。"""


def _get_ticket_ws_url(proxy_str: str | None) -> str:
    """向中转站换取一次性 ticket WS URL。**失败即抛**，绝不回落 `?token=`。

    中转站不认官方直连的 `?token={KEY}` 写法（实测 HTTP 403）。旧实现换不到票就
    拿它去连，代价是每次白烧一轮握手 + 一次退避翻倍，而日志上只留下一条刺眼的
    403 —— 真正失败的换票记在 `logger.debug` 上（root logger 是 INFO ⇒ 一行都不
    打），非 200 分支更是连 debug 都没有，直接 return None。

    2026-08-07 盘中现场就是这么来的：22:23:14 连 `socket?token...` 吃 403、退避
    32s，22:23:56 连 `socket?ticke...` 一切正常。一次静默的换票超时被放大成一条
    误导性的 403 加一分钟断线。所以这里把每种失败都说清楚、并交给调用方退避重试。
    """
    rest_base = UW_ENDPOINT.rstrip("/")
    ticket_url = f"{rest_base}/socket-ticket"
    headers = {
        "X-API-Key": UW_API_KEY,
        "Authorization": f"Bearer {UW_API_KEY}",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) tt-trading/1.0",
        "Accept": "application/json",
    }
    proxies = {"http": proxy_str, "https": proxy_str} if proxy_str else None
    try:
        resp = requests.post(ticket_url, headers=headers, proxies=proxies,
                             timeout=_TICKET_HTTP_TIMEOUT_S)
    except Exception as e:  # noqa: BLE001 — 网络层什么都可能抛，一律归一成换票失败
        raise _TicketError(f"POST {ticket_url} 失败: {type(e).__name__}: {e}") from e
    if resp.status_code != 200:
        raise _TicketError(f"POST {ticket_url} 返回 HTTP {resp.status_code}: "
                           f"{(resp.text or '')[:200]}")
    try:
        data = resp.json()
    except Exception as e:  # noqa: BLE001 — 中转站故障时可能返回 HTML 错误页
        raise _TicketError(f"{ticket_url} 响应不是 JSON: "
                           f"{(resp.text or '')[:200]}") from e
    ws_url = data.get("websocket_url") if isinstance(data, dict) else None
    if not ws_url:
        raise _TicketError(f"{ticket_url} 响应里没有 websocket_url: {str(data)[:200]}")
    return ws_url


def _build_ws_url_and_headers(proxy_str: str | None) -> tuple[str, dict[str, str]]:
    """生成连接所需的 URL 和 Headers（包含 Origin 防火墙标头）。

    换票失败时**直接抛 `_TicketError`**，由 `_run_loop` 退避后重试换票。
    """
    ws_url = _get_ticket_ws_url(proxy_str)

    parsed = urlparse(ws_url)
    origin_scheme = "https" if parsed.scheme in ("wss", "https") else "http"
    origin = f"{origin_scheme}://{parsed.netloc}"

    headers = {
        "Origin": origin,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) tt-trading/1.0",
    }
    return ws_url, headers


@dataclass
class _ChannelMonitorState:
    """一个频道在当前连接代次中的共享状态。"""

    state: str = JOIN_PENDING
    connection_generation: int = 0
    join_requested_at_monotonic: float | None = None
    acked_at_monotonic: float | None = None
    last_payload_at_monotonic: float | None = None


class MonitoredSubscription:
    """带控制面可观测性的单 listener 订阅句柄。

    `payloads()` 与 `controls()` 使用相互独立的队列：某个慢 listener 的 payload
    队列溢出，只会丢该 listener 自己最旧的一条数据并增加自己的 drop count；不会
    阻塞底层收帧，也不会污染同频道的其他 listener。调用方结束使用时须 `await close()`。
    """

    def __init__(
        self,
        channel: str,
        client: "UwWebSocketClient | None",
        *,
        payload_queue_maxsize: int | None = None,
    ):
        self.channel = channel
        self._client = client
        maxsize = _QUEUE_MAXSIZE if payload_queue_maxsize is None else payload_queue_maxsize
        self._payload_queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        # 控制事件远低于 payload 频率；使用独立无界队列，避免 payload 洪水把 ACK、断线
        # 等关键状态挤掉。QUEUE_DROPPED 的累计真值同时保存在 status() 中。
        self._control_queue: asyncio.Queue = asyncio.Queue()
        self._state = JOIN_PENDING
        self._connection_generation = 0
        self._join_requested_at_monotonic: float | None = None
        self._acked_at_monotonic: float | None = None
        self._last_payload_at_monotonic: float | None = None
        self._listener_drop_count = 0
        self._closed = False
        self._closing = False
        self._payload_ended = False
        self._controls_ended = False
        self._pending_drop_event: dict[str, Any] | None = None

    def status(self) -> dict[str, Any]:
        """返回本 listener 的无副作用状态快照。所有时刻均为 monotonic。"""
        return {
            "channel": self.channel,
            "state": self._state,
            "connection_generation": self._connection_generation,
            "join_requested_at_monotonic": self._join_requested_at_monotonic,
            "acked_at_monotonic": self._acked_at_monotonic,
            "last_payload_at_monotonic": self._last_payload_at_monotonic,
            "listener_drop_count": self._listener_drop_count,
            "closed": self._closed,
        }

    async def payloads(self) -> AsyncIterator[dict]:
        """只迭代数据帧；控制事件永远不会混入此流。"""
        while True:
            item = await self._payload_queue.get()
            if item is _PAYLOAD_END:
                return
            yield item

    async def controls(self) -> AsyncIterator[dict[str, Any]]:
        """迭代 JOIN/ACK/断线/丢包/关闭等控制事件。"""
        while True:
            item = await self._control_queue.get()
            if item is _CONTROL_END:
                return
            if item is self._pending_drop_event:
                self._pending_drop_event = None
            yield item

    def __aiter__(self) -> AsyncIterator[dict]:
        """便捷兼容：直接 async for handle 等价于迭代 payloads()。"""
        return self.payloads()

    async def __aenter__(self) -> "MonitoredSubscription":
        return self

    async def __aexit__(self, *_exc) -> None:
        await self.close()

    def _sync_channel_state(self, state: _ChannelMonitorState) -> None:
        self._connection_generation = state.connection_generation
        self._join_requested_at_monotonic = state.join_requested_at_monotonic
        self._acked_at_monotonic = state.acked_at_monotonic

    def _emit_control(
        self,
        event_type: str,
        state: _ChannelMonitorState,
        *,
        occurred_at: float | None = None,
        detail: str | None = None,
    ) -> None:
        if ((self._closed or self._closing) and event_type != CLOSED) or self._controls_ended:
            return
        self._sync_channel_state(state)
        self._state = event_type
        event = self.status()
        event["type"] = event_type
        event["occurred_at_monotonic"] = (
            time.monotonic() if occurred_at is None else occurred_at
        )
        if detail:
            event["detail"] = detail
        if event_type == QUEUE_DROPPED and self._pending_drop_event is not None:
            # payload 持续洪水而调用方尚未取走上一条 drop 通知时，就地更新队列里
            # 那个 dict；累计数的权威值仍在 status()，控制队列不会随丢包数无限增长。
            self._pending_drop_event.clear()
            self._pending_drop_event.update(event)
            return
        if event_type == QUEUE_DROPPED:
            self._pending_drop_event = event
        self._control_queue.put_nowait(event)

    def _offer_payload(
        self,
        payload: Any,
        state: _ChannelMonitorState,
        *,
        received_at: float,
    ) -> None:
        if self._closed or self._closing or self._payload_ended:
            return
        self._sync_channel_state(state)
        # last_payload 是 listener 自己实际收到分发的时刻，不能继承同频道其他 listener
        # 加入前的历史值。
        self._last_payload_at_monotonic = received_at
        try:
            self._payload_queue.put_nowait(payload)
        except asyncio.QueueFull:
            # 只操作本 handle 的队列。其他 listener 各有独立 Queue，不受牵连。
            try:
                self._payload_queue.get_nowait()
                self._payload_queue.put_nowait(payload)
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                return
            self._listener_drop_count += 1
            self._emit_control(
                QUEUE_DROPPED,
                state,
                occurred_at=received_at,
                detail="payload 队列已满，丢弃本 listener 最旧的一条数据",
            )

    def _end_payloads(self) -> None:
        if self._payload_ended:
            return
        self._payload_ended = True
        # 终止信号不能因满队列而丢失；关闭时数据已不再有价值，先清一个位置。
        try:
            self._payload_queue.put_nowait(_PAYLOAD_END)
        except asyncio.QueueFull:
            try:
                self._payload_queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            self._payload_queue.put_nowait(_PAYLOAD_END)

    def _reject(self, detail: str, *, generation: int = 0) -> None:
        state = _ChannelMonitorState(state=REJECTED, connection_generation=generation)
        self._emit_control(REJECTED, state, detail=detail)
        # 拒绝是 payload 侧终态；controls 仍保留到显式 close，以便调用方读到原因。
        self._end_payloads()

    async def close(self) -> None:
        """幂等关闭此 listener；最后一个 listener 关闭时由 client 发送 leave。"""
        if self._closed or self._closing:
            return
        self._closing = True
        client = self._client
        if client is not None:
            await client._close_monitored_subscription(self)
            self._client = None

        state = _ChannelMonitorState(
            state=CLOSED,
            connection_generation=self._connection_generation,
            join_requested_at_monotonic=self._join_requested_at_monotonic,
            acked_at_monotonic=self._acked_at_monotonic,
            last_payload_at_monotonic=self._last_payload_at_monotonic,
        )
        self._closed = True
        self._emit_control(CLOSED, state)
        self._end_payloads()
        if not self._controls_ended:
            self._controls_ended = True
            self._control_queue.put_nowait(_CONTROL_END)


class UwWebSocketClient:
    """单条长连接多 Channel 复用客户端。"""

    def __init__(self, group_name: str = "default",
                 on_release: Callable[[str], None] | None = None):
        self._group_name = group_name
        self._tag = f"[UW-WS][{group_name}]"
        self._on_release = on_release
        self._listeners: Dict[str, Set[asyncio.Queue]] = {}
        self._monitored_listeners: Dict[str, Set[MonitoredSubscription]] = {}
        self._channels: Set[str] = set()
        self._channel_monitor_states: Dict[str, _ChannelMonitorState] = {}
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._loop_task: asyncio.Task | None = None
        self._lock: asyncio.Lock | None = None
        self._send_lock: asyncio.Lock | None = None
        self._loop_id: int | None = None
        self._last_frame_at: float | None = None   # time.monotonic()，供上层判活
        self._connection_generation = 0
        self._connection_state = DISCONNECTED
        self._ack_deadline_tasks: Dict[str, asyncio.Task] = {}

    def _ensure_locks(self):
        """惰性绑定并发锁，防止 pytest 跨多期 asyncio.run() 触发 different loop 异常。"""
        try:
            current_loop = asyncio.get_running_loop()
            loop_id = id(current_loop)
        except RuntimeError:
            loop_id = None
        if self._lock is None or self._send_lock is None or self._loop_id != loop_id:
            self._lock = asyncio.Lock()
            self._send_lock = asyncio.Lock()
            self._loop_id = loop_id

    async def _send_frame(
        self,
        ws,
        channel: str,
        msg_type: str,
        *,
        raise_on_error: bool = False,
    ) -> bool:
        """向 WS 发送 join/leave 帧（以专属写互斥锁保护，杜绝并发调用 ws.send 崩溃）。"""
        try:
            frame = json.dumps({"channel": channel, "msg_type": msg_type})
            self._ensure_locks()
            async with self._send_lock:
                await ws.send(frame)
            logger.info(f"{self._tag} 已发送 {msg_type} 帧 channel={channel}")
            return True
        except Exception as e:
            logger.warning(f"{self._tag} 发送 {msg_type} 帧失败 channel={channel}: {e}")
            if raise_on_error:
                raise
            return False

    async def _send_join(
        self, ws, channel: str, *, raise_on_error: bool = False
    ) -> bool:
        self._mark_join_pending(channel)
        return await self._send_frame(
            ws, channel, "join", raise_on_error=raise_on_error)

    async def _send_leave(self, ws, channel: str):
        await self._send_frame(ws, channel, "leave")

    async def _ensure_loop_started(self):
        """确保底层单连接循环已在后台运行。"""
        self._ensure_locks()
        async with self._lock:
            if self._loop_task is None or self._loop_task.done():
                self._loop_task = asyncio.create_task(self._run_loop())

    def _monitor_state_for(self, channel: str) -> _ChannelMonitorState:
        state = self._channel_monitor_states.get(channel)
        if state is None:
            state = _ChannelMonitorState(
                state=JOIN_PENDING,
                connection_generation=self._connection_generation,
            )
            self._channel_monitor_states[channel] = state
        return state

    def _broadcast_control(
        self,
        channel: str,
        event_type: str,
        *,
        occurred_at: float | None = None,
        detail: str | None = None,
    ) -> None:
        state = self._monitor_state_for(channel)
        for handle in list(self._monitored_listeners.get(channel, ())):
            handle._emit_control(
                event_type,
                state,
                occurred_at=occurred_at,
                detail=detail,
            )

    def _cancel_ack_deadline(self, channel: str) -> None:
        task = self._ack_deadline_tasks.pop(channel, None)
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    def _cancel_all_ack_deadlines(self) -> None:
        current = asyncio.current_task()
        tasks, self._ack_deadline_tasks = self._ack_deadline_tasks, {}
        for task in tasks.values():
            if task is not current:
                task.cancel()

    def _arm_ack_deadline(self, channel: str) -> None:
        """仅 monitored listener 需要 ACK 时限；旧 subscribe 保持原兼容语义。"""
        self._cancel_ack_deadline(channel)
        if (
            channel not in self._monitored_listeners
            or self._connection_generation <= 0
            or self._ws is None
            or not _is_open(self._ws)
        ):
            return
        state = self._monitor_state_for(channel)
        requested_at = state.join_requested_at_monotonic
        if requested_at is None:
            return
        task = asyncio.create_task(
            self._wait_for_ack_deadline(
                channel,
                generation=state.connection_generation,
                requested_at=requested_at,
            ),
            name=f"uw-ws-ack:{self._group_name}:{channel}",
        )
        self._ack_deadline_tasks[channel] = task

    async def _wait_for_ack_deadline(
        self,
        channel: str,
        *,
        generation: int,
        requested_at: float,
    ) -> None:
        current = asyncio.current_task()
        try:
            await asyncio.sleep(_MONITORED_ACK_TIMEOUT_S)
            state = self._channel_monitor_states.get(channel)
            if (
                state is None
                or state.state != JOIN_PENDING
                or state.connection_generation != generation
                or state.join_requested_at_monotonic != requested_at
                or channel not in self._monitored_listeners
            ):
                return
            detail = (
                f"频道 {channel} 在 {_MONITORED_ACK_TIMEOUT_S:.1f}s 内未收到 ACK，"
                f"connection_generation={generation}"
            )
            logger.warning(f"{self._tag} {detail}，强制重连并重建 monitored 订阅")
            ws_ref = self._ws
            await self._force_reconnect(ws_ref, detail)
        except asyncio.CancelledError:
            return
        finally:
            if self._ack_deadline_tasks.get(channel) is current:
                self._ack_deadline_tasks.pop(channel, None)

    async def _force_reconnect(self, ws_ref, detail: str) -> None:
        """使当前连接退出收帧循环；listener/handle 留存，下一代次自动重新 join。"""
        self._broadcast_connection_event(DISCONNECTED, detail)
        self._broadcast_connection_event(RECONNECTING, f"{detail}；正在重建订阅")
        if ws_ref is None or not _is_open(ws_ref):
            return
        try:
            await ws_ref.close()
        except Exception as exc:  # noqa: BLE001 — close 失败再中止底层 transport
            logger.warning(f"{self._tag} 强制关闭失效连接失败: {exc}")
            transport = getattr(ws_ref, "transport", None)
            abort = getattr(transport, "abort", None)
            if callable(abort):
                abort()

    def _mark_join_pending(self, channel: str) -> None:
        now = time.monotonic()
        state = self._monitor_state_for(channel)
        state.state = JOIN_PENDING
        state.connection_generation = self._connection_generation
        state.join_requested_at_monotonic = now
        state.acked_at_monotonic = None
        self._broadcast_control(channel, JOIN_PENDING, occurred_at=now)
        self._arm_ack_deadline(channel)

    def _mark_acked(self, channel: str) -> None:
        self._cancel_ack_deadline(channel)
        now = time.monotonic()
        state = self._monitor_state_for(channel)
        state.state = ACKED
        state.connection_generation = self._connection_generation
        state.acked_at_monotonic = now
        self._broadcast_control(channel, ACKED, occurred_at=now)

    def _mark_rejected(self, channel: str, detail: str) -> None:
        self._cancel_ack_deadline(channel)
        now = time.monotonic()
        state = self._monitor_state_for(channel)
        state.state = REJECTED
        state.connection_generation = self._connection_generation
        self._broadcast_control(
            channel, REJECTED, occurred_at=now, detail=detail)

    def _broadcast_connection_event(self, event_type: str, detail: str | None = None) -> None:
        if event_type in {DISCONNECTED, RECONNECTING}:
            self._cancel_all_ack_deadlines()
        now = time.monotonic()
        self._connection_state = event_type
        for channel in list(self._channels):
            state = self._monitor_state_for(channel)
            state.state = event_type
            state.connection_generation = self._connection_generation
            self._broadcast_control(
                channel,
                event_type,
                occurred_at=now,
                detail=detail,
            )

    def _reject_server_frame(self, data: dict) -> None:
        """把无 channel 的服务端错误保守归因到本代次所有 pending join。"""
        explicit_channel = data.get("channel")
        if isinstance(explicit_channel, str) and explicit_channel in self._channels:
            targets = [explicit_channel]
        else:
            targets = [
                channel
                for channel in self._channels
                if self._monitor_state_for(channel).state == JOIN_PENDING
            ]
        # 服务端错误帧通常不带 channel；若没有 pending，仍广播给本连接所有频道，
        # 不能让硬错误再次只剩日志、上层毫无感知。
        if not targets:
            targets = list(self._channels)
        detail = json.dumps(data, ensure_ascii=False, sort_keys=True)
        for channel in targets:
            self._mark_rejected(channel, detail)

    def _dispatch_payload(self, channel: str, payload: Any) -> None:
        """按 listener 独立派发 payload，并维护每 listener 的 last/drop 状态。"""
        received_at = time.monotonic()
        state = self._monitor_state_for(channel)
        state.connection_generation = self._connection_generation
        state.last_payload_at_monotonic = received_at

        # 旧 subscribe() 的 payload-only Queue 保持原行为。
        for queue in list(self._listeners.get(channel, ())):
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                try:
                    queue.get_nowait()
                    queue.put_nowait(payload)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass

        # monitored listener 各自记 drop，绝不共享计数或队列。
        for handle in list(self._monitored_listeners.get(channel, ())):
            handle._offer_payload(payload, state, received_at=received_at)

    async def _run_loop(self):
        """底层单一 WS 连接与消息分发循环。"""
        ws_proxy = _resolve_proxy()
        backoff = _BACKOFF_INIT
        loop = asyncio.get_running_loop()
        self._broadcast_connection_event(RECONNECTING, "正在建立连接")

        while True:
            connected_at: float | None = None
            try:
                ws_url, headers = await asyncio.to_thread(_build_ws_url_and_headers, ws_proxy)
                logger.info(
                    f"{self._tag} 正在建立复用长连接 {ws_url[:50]}..."
                    + (f" proxy={ws_proxy}" if ws_proxy else " (直连)")
                )

                async with websockets.connect(
                    ws_url,
                    proxy=ws_proxy,
                    additional_headers=headers,
                    open_timeout=15,
                    # **完全不发协议层 PING**，对齐官方参考实现
                    # （docs/uw/unusual_whales_websocket_full_docs.md L114-146）：
                    # `run_forever(dispatcher=rel, reconnect=5)` 既不传 ping_interval
                    # 也不传 ping_timeout —— websocket-client 不传即 0，一帧 PING 都
                    # 不发，死连接只靠「对端关闭 → 重连」。全文 1921 行零处提到
                    # heartbeat/ping/pong/keepalive，协议只有 join 与 ACK。
                    #
                    # 两步演进（2026-08-13 当天实测驱动）：
                    # ① 原 ping_timeout=10 → 全天 185 次断线里 74 次是「PONG 10s 没回
                    #    → 自己 close」，日志 `sent 1011 ... keepalive ping timeout`；
                    # ② 改 ping_timeout=None 后措辞变成 `received 1011 ...; then sent
                    #    1011` —— 我方不再自杀，却换成**中转站因收不到我方 PONG 而踢
                    #    连接**。说明 PING/PONG 控制帧在 SOCKS 代理或中转站那层本身就
                    #    不可靠，「只发不等」反而给中间层塞了它处理不好的帧。故一并停发。
                    #
                    # 代价（已知并接受）：无协议层保活，空闲连接依赖中转站自身策略；
                    # 连接级死链检测仍缺位（last_payload_at_monotonic 无看门狗消费）。
                    # 若观测到挂死连接，应补**区分盘中/盘后**的数据静默看门狗，
                    # 而不是把 PING 加回来（盘后本就无数据，naive 实现会整夜空转重连）。
                    ping_interval=None,
                    ping_timeout=None,
                    max_size=2 ** 20,
                ) as ws:
                    try:
                        self._ws = ws
                        connected_at = loop.time()
                        self._connection_generation += 1
                        self._connection_state = "CONNECTED"

                        # ── 自动批量发送当前已注册的所有频道 ──
                        self._ensure_locks()
                        async with self._lock:
                            channels_to_join = list(self._channels)
                            monitored_channels = set(self._monitored_listeners)

                        for ch in channels_to_join:
                            await self._send_join(
                                ws,
                                ch,
                                raise_on_error=ch in monitored_channels,
                            )

                        # ── 消息分发循环 ──
                        async for raw_msg in ws:
                            try:
                                data = json.loads(raw_msg)
                            except (json.JSONDecodeError, ValueError):
                                continue

                            self._last_frame_at = time.monotonic()

                            # 服务端错误帧是 **dict**（不是 list）。此前这里只认 list，
                            # 错误帧被静默吞掉——超预算、频道名写错全都无声无息。
                            # 实测形态：{"error":"subscription_limit",
                            #           "message":"Maximum 12 channels per connection."}
                            if isinstance(data, dict) and ("error" in data or "message" in data):
                                logger.error(f"{self._tag} 服务端错误帧: {data}")
                                self._reject_server_frame(data)
                                if data.get("error") == "subscription_limit":
                                    logger.error(
                                        f"{self._tag} 本组频道已达单连接上限 "
                                        f"{MAX_CHANNELS} 条，已订: {sorted(self._channels)}。"
                                        f"请关掉本组某个 feed，或把频道改路由到别的组。"
                                    )
                                continue

                            # 格式1: 订阅响应确认 ["channel_name", {"status": "ok", ...}]
                            # 格式2: 数据帧 ["channel_name", payload_dict]
                            if isinstance(data, list) and len(data) >= 2:
                                ch_name, payload = data[0], data[1]
                                if isinstance(payload, dict) and payload.get("status") == "ok":
                                    logger.info(f"{self._tag} 频道订阅成功确认 ACK: {ch_name}")
                                    if isinstance(ch_name, str) and ch_name in self._channels:
                                        self._mark_acked(ch_name)
                                    continue

                                if isinstance(ch_name, str):
                                    self._dispatch_payload(ch_name, payload)
                    finally:
                        # 确保即使服务处于平滑终止，同样可靠清理死链接引用
                        self._ws = None

                # 对端「正常关闭」时 websockets 的 `async for` 是 return 而非抛异常，
                # 会一路走到这里。这条路径以前不退避直接重连，服务端只要一握手就正常
                # 关连接（鉴权被拒的典型形态）就会变成全速轰炸中转站、招封 IP。
                reason = "对端正常关闭连接"
            except asyncio.CancelledError:
                self._broadcast_connection_event(DISCONNECTED, "连接循环已取消")
                logger.info(f"{self._tag} 单连接循环收到取消信号，关闭")
                break
            except _TicketError as exc:
                # 必须排在 Exception 之前。换票失败**根本没发起过连接**，套用下面
                # 那句「复用连接异常」会把排查引到 WS 链路上去——2026-08-07 就是被
                # 一条 403 带偏的，真凶在换票这步。
                reason = f"换票失败（中转站只认 ticket，不回落 ?token=）: {exc}"
                logger.error(f"{self._tag} {reason}")
            except Exception as exc:
                reason = f"连接异常: {exc}"
                logger.error(f"{self._tag} 复用{reason}")

            # 只有真正连稳过才重置退避；「握手即断」必须让退避继续涨。
            if connected_at is not None:
                self._broadcast_connection_event(DISCONNECTED, reason)
            if connected_at is not None and loop.time() - connected_at >= _HEALTHY_UPTIME_S:
                backoff = _BACKOFF_INIT
            logger.info(f"{self._tag} {reason}，{backoff:.0f}s 后重连并全量重新订阅...")
            self._broadcast_connection_event(
                RECONNECTING, f"{reason}；{backoff:.0f}s 后重连")
            try:
                await asyncio.sleep(backoff)
            except asyncio.CancelledError:
                self._broadcast_connection_event(DISCONNECTED, "重连等待已取消")
                logger.info(f"{self._tag} 重连等待收到取消信号，关闭")
                break
            backoff = min(backoff * _BACKOFF_FACTOR, _BACKOFF_MAX)

    async def open_monitored_subscription(self, channel: str) -> MonitoredSubscription:
        """立即注册并启动带控制面的订阅，返回每 listener 独立的 handle。"""
        handle = MonitoredSubscription(channel, self)
        if not UW_API_KEY:
            logger.error(f"{self._tag} 缺少 UW_API_KEY，无法连接 WebSocket")
            # 模块级池可能已经为该新频道做了同步预留，拒绝时必须归还。
            if channel not in self._channels and self._on_release is not None:
                self._on_release(channel)
            handle._reject("缺少 UW_API_KEY")
            return handle

        self._ensure_locks()
        async with self._lock:
            is_new_channel = channel not in self._channels
            rejected = is_new_channel and len(self._channels) >= MAX_CHANNELS
            if not rejected:
                self._monitored_listeners.setdefault(channel, set()).add(handle)
                self._channels.add(channel)
                state = self._monitor_state_for(channel)
                ws_ref = self._ws
            else:
                state = None
                ws_ref = None

        if rejected:
            detail = (
                f"频道预算已满（{len(self._channels)}/{MAX_CHANNELS}），"
                f"拒绝订阅 {channel}；已订阅: {sorted(self._channels)}"
            )
            logger.error(f"{self._tag} {detail}")
            if self._on_release is not None:
                self._on_release(channel)
            handle._reject(detail, generation=self._connection_generation)
            return handle

        assert state is not None
        if not is_new_channel:
            # 同频道共享一条上行 join；新 listener 立即拿到当前频道状态，不重复 join。
            if state.state != JOIN_PENDING or state.join_requested_at_monotonic is not None:
                handle._emit_control(
                    state.state,
                    state,
                    detail="复用同频道现有订阅状态",
                )
        elif self._connection_state == RECONNECTING:
            state.state = RECONNECTING
            state.connection_generation = self._connection_generation
            handle._emit_control(
                RECONNECTING,
                state,
                detail="连接正在建立或重连",
            )

        await self._ensure_loop_started()

        # 与旧 subscribe 的惰性生成器不同：本 API 已完成 listener 注册；若连接在线，
        # await 返回前就发送 join，适合 Alert 后立即启动 option_trades:{ticker} 缓冲。
        if is_new_channel and ws_ref is not None and _is_open(ws_ref):
            try:
                await self._send_join(
                    ws_ref, channel, raise_on_error=True)
            except Exception as exc:  # noqa: BLE001 — handle 留存，强制连接重建
                await self._force_reconnect(
                    ws_ref,
                    f"monitored join 发送失败 channel={channel}: {exc}",
                )
        return handle

    async def _close_monitored_subscription(
        self, handle: MonitoredSubscription
    ) -> None:
        """移除一个 monitored listener；仅总引用归零时 leave/release。"""
        channel = handle.channel
        self._ensure_locks()
        async with self._lock:
            released = False
            listeners = self._monitored_listeners.get(channel)
            if listeners is not None:
                listeners.discard(handle)
                if not listeners:
                    del self._monitored_listeners[channel]
                    self._cancel_ack_deadline(channel)
            if (
                channel not in self._listeners
                and channel not in self._monitored_listeners
            ):
                self._channels.discard(channel)
                self._channel_monitor_states.pop(channel, None)
                released = True
            ws_ref = self._ws

        if released:
            if self._on_release is not None:
                self._on_release(channel)
            if ws_ref is not None and _is_open(ws_ref):
                await self._send_leave(ws_ref, channel)

    async def subscribe(self, channel: str) -> AsyncIterator[dict]:
        """上层订阅指定 channel，返回 AsyncIterator 流式接收数据。

        支持多模块/多 Task 同时订阅相同或不同的 channel，共享底层单一 WS 连接。
        超出 `MAX_CHANNELS` 频道预算时**拒绝订阅并直接返回**（不 yield 任何数据），
        省掉一次必然失败的往返：硬发出去的话服务端会明确返回
        `{"error":"subscription_limit","message":"Maximum 12 channels per connection."}`
        错误帧，而不是像早前误以为的那样静默丢弃。
        """
        if not UW_API_KEY:
            logger.error(f"{self._tag} 缺少 UW_API_KEY，无法连接 WebSocket")
            return

        queue: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)

        self._ensure_locks()
        async with self._lock:
            is_new_channel = channel not in self._channels
            if is_new_channel and len(self._channels) >= MAX_CHANNELS:
                logger.error(
                    f"{self._tag} 频道预算已满（{len(self._channels)}/{MAX_CHANNELS}），"
                    f"拒绝订阅 {channel}；已订阅: {sorted(self._channels)}"
                )
                return
            self._listeners.setdefault(channel, set()).add(queue)
            self._channels.add(channel)
            is_first_listener = is_new_channel
            ws_ref = self._ws

        await self._ensure_loop_started()

        # 如果长连接已经建立，且该 channel 为新加入频路，立即补充上行订阅
        if is_first_listener and ws_ref is not None and _is_open(ws_ref):
            await self._send_join(ws_ref, channel)

        try:
            while True:
                payload = await queue.get()
                yield payload
        finally:
            # 取消订阅/退出时的清理工作
            self._ensure_locks()
            async with self._lock:
                released = False
                if channel in self._listeners:
                    self._listeners[channel].discard(queue)
                    if not self._listeners[channel]:
                        del self._listeners[channel]
                if (
                    channel not in self._listeners
                    and channel not in self._monitored_listeners
                ):
                    self._channels.discard(channel)
                    self._channel_monitor_states.pop(channel, None)
                    released = True
                ws_ref = self._ws
            # 最后一个订阅者退出 → 归还池内预留，并向服务端 leave 把频道预算还回去。
            # 顺序要紧：先归还预留（同步、必成功），再发 leave（走网络、可能失败）。
            # 反过来的话 leave 抛异常会让预留永远漏掉，分片假性占满。
            if released:
                if self._on_release is not None:
                    self._on_release(channel)
                if ws_ref is not None and _is_open(ws_ref):
                    await self._send_leave(ws_ref, channel)


class UwWsPool:
    """按用途分组持有连接，**组内按需扩容** —— 一个分片一条连接、各自独立 12 条预算。

    惰性建连：没人订阅的组不建分片、不占连接。
    频道**粘住分片**：同一频道 leave 后再 join 回原分片，不会被重新分配。
    """

    def __init__(self):
        self._shards: Dict[str, list[str]] = {}            # 组名 → 分片名列表
        self._clients: Dict[str, UwWebSocketClient] = {}   # 分片名 → 连接
        self._reserved: Dict[str, Set[str]] = {}           # 分片名 → 已预留的频道
        self._owner: Dict[str, str] = {}                   # 频道名 → 分片名

    def client_for(self, channel: str) -> "UwWebSocketClient | None":
        """给频道分配一条连接。到全局硬顶时返回 None（调用方须处理）。

        **占用计数必须走 `_reserved`，绝不能读 `client._channels`。** 本函数是同步的、
        中间没有 await，所以「判断 + 预留」是原子的；而 `_channels.add()` 发生在
        `UwWebSocketClient.subscribe()` 里 `async with self._lock` 之后，与本函数之间
        隔着 await —— 拿它判满没满，两个 task 并发订阅时会双双读到 11、双双塞进同一
        分片变成 13 条，然后被服务端拒掉第 13 个。
        """
        shard = self._owner.get(channel)
        if shard is not None:
            return self._clients[shard]

        group = group_of(channel)
        names = self._shards.setdefault(group, [])
        for name in names:
            if len(self._reserved[name]) < MAX_CHANNELS:
                break
        else:
            if len(self._clients) >= MAX_CONNECTIONS:
                detail = " ".join(
                    f"{n}({len(self._reserved[n])})" for n in sorted(self._clients))
                logger.error(
                    f"[UW-WS] 连接数已达全局上限 {MAX_CONNECTIONS} 条，"
                    f"拒绝订阅 {channel}。当前分片占用: {detail}。"
                    f"每连接 {MAX_CHANNELS} 条频道，要扩容需先实测更多并发连接可用。"
                )
                return None
            name = f"{group}#{len(names)}"
            names.append(name)
            self._reserved[name] = set()
            self._clients[name] = UwWebSocketClient(
                group_name=name, on_release=self._release)

        self._reserved[name].add(channel)
        self._owner[channel] = name
        return self._clients[name]

    def _release(self, channel: str) -> None:
        """频道引用计数归零时归还预留（由 client 的 on_release 回调驱动，见 Task 2）。

        不归还的话频道只 leave 不释放预留，分片会假性占满、逼池子白开新连接。
        """
        shard = self._owner.pop(channel, None)
        if shard is not None:
            self._reserved[shard].discard(channel)

    def status(self) -> Dict[str, dict]:
        """每组的连接状态与已订频道，供上层判活与排查。

        `last_frame_age_s`：距上次收帧过了多少秒，换算自内部的
        `time.monotonic()` 时刻再交出去——`time.monotonic()` 的返回值相对于
        一个未定义起点，直接把它当绝对时间戳暴露给进程外消费者（前端 JSON、
        运维接口、日志）毫无意义，必须在这里做完减法。
        """
        out: Dict[str, dict] = {}
        for group, client in self._clients.items():
            ws = client._ws
            last = client._last_frame_at
            out[group] = {
                "connected": ws is not None and _is_open(ws),
                "channels": sorted(client._channels),
                "last_frame_age_s": None if last is None else time.monotonic() - last,
            }
        return out

    def remaining_capacity(self, group: str) -> int:
        """该组还能再接多少条频道 = 现有分片空位 + 还能开的连接数 × 每连接上限。

        这是**申报制的反面**：不是各模块告诉池子「我要用多少」（每加一个模块就要记得
        同步登记，2026-08-07 SOS 接入时就漏了），而是模块问池子「还剩多少」，
        池子是唯一权威，新增模块无需任何登记。

        **已知时序局限**：惰性建连意味着调用时其他组可能尚未建分片，此刻会把它们将来
        要占的连接也算进来（空池查 ticker 得 96 而非 72）。后果是略微偏乐观的池子能
        通过前置检查，随后在实际订到超出的那一条时被 `client_for()` 拒绝并打 ERROR
        ——仍然不会静默半跑。前置检查是「早期告警」，到顶 ERROR 才是最终防线。
        """
        names = self._shards.get(group, [])
        free_in_existing = sum(
            MAX_CHANNELS - len(self._reserved[n]) for n in names)
        new_connections = MAX_CONNECTIONS - len(self._clients)
        return free_in_existing + new_connections * MAX_CHANNELS


# 全局连接池（取代此前的单条全局连接）
UW_WS_POOL = UwWsPool()

def remaining_capacity(group: str) -> int:
    """模块级快捷入口，形态与 `subscribe()` 一致（便于测试 monkeypatch）。"""
    return UW_WS_POOL.remaining_capacity(group)


async def open_monitored_subscription(channel: str) -> MonitoredSubscription:
    """按频道路由并立即启动 monitored subscription。

    与 payload-only 的 `subscribe()` 不同，本函数必须被 await；返回时 listener 已注册，
    在线连接上的动态频道也已发送 join。分配/API Key 失败不会抛空指针，而是返回一个
    可从 `controls()` 读取 `REJECTED` 原因的终态 handle。
    """
    if not UW_API_KEY:
        handle = MonitoredSubscription(channel, None)
        handle._reject("缺少 UW_API_KEY")
        return handle
    client = UW_WS_POOL.client_for(channel)
    if client is None:
        handle = MonitoredSubscription(channel, None)
        handle._reject(
            f"连接数已达全局上限 {MAX_CONNECTIONS}，无法分配频道 {channel}"
        )
        return handle
    return await client.open_monitored_subscription(channel)


async def _rejected_stream(channel: str) -> AsyncIterator[dict]:
    """分配失败时的空流：立即结束。

    消费方（quote_loops / market_tide_service）的 `async for` 会正常收尾，
    各自走已有的重订阅退避，不会崩也不会空转烧 CPU。
    """
    return
    yield       # pragma: no cover — 使其成为异步生成器


def subscribe(channel: str) -> AsyncIterator[dict]:
    """快捷入口：按频道名路由到对应连接组的分片并订阅。

    签名与模块级形态**不得改变**——`quote_loops.py`、`market_tide_service.py`
    都是函数内局部导入它，测试也靠 monkeypatch 本模块属性来替换。

    连接数到全局硬顶时返回**空流**（`client_for` 已打过 ERROR），语义与既有的
    「超预算拒绝订阅、不 yield 任何数据」一致。
    """
    client = UW_WS_POOL.client_for(channel)
    if client is None:
        return _rejected_stream(channel)
    return client.subscribe(channel)
