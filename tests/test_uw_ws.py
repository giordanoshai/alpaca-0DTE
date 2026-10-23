# -*- coding: utf-8 -*-
"""UW WebSocket 复用客户端的回归测试。

锁死三个曾经真实存在的缺陷：
1. `subscribe()` 里用 `ws.closed` 判活 —— websockets 14+ 的 `ClientConnection`
   根本没有这个属性，连接已建立后再订阅新 channel 必抛 AttributeError；
2. 对端「正常关闭」时不退避直接重连 —— 服务端一握手就正常关连接（鉴权被拒的
   典型形态）会变成全速轰炸中转站；
3. 换票失败时回落 `?token={KEY}` —— 中转站不认这条官方直连写法（实测 403），
   回落等于白烧一轮握手，还把真因（换票失败）盖在一条刺眼的 403 底下。
"""
import asyncio
import logging

import pytest
import requests
from websockets.protocol import State

import app.integrations.uw_ws as uw_ws


class _Stop(Exception):
    """把被测的无限重连循环从外部停下来。"""


class _FakeWS:
    """websockets 14+ 的真实形态：有 `.state`，**没有** `.closed`。

    `__anext__` 立刻 StopAsyncIteration，模拟「握手成功后对端正常关闭」——
    这条路径下 websockets 的 `async for` 是 return 而不是抛异常。
    """

    def __init__(self):
        self.state = State.OPEN
        self.sent: list[str] = []

    async def send(self, msg):
        self.sent.append(msg)

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


# ── 1. _is_open / subscribe 不再依赖 .closed ──────────────────────────────────

def test_is_open_读state而不是closed():
    ws = _FakeWS()
    assert not hasattr(ws, "closed"), "前提：新版连接对象没有 .closed"
    assert uw_ws._is_open(ws) is True

    ws.state = State.CLOSED
    assert uw_ws._is_open(ws) is False


def test_is_open_对老版protocol保留closed回退():
    class _Legacy:
        closed = False

    assert uw_ws._is_open(_Legacy()) is True
    legacy = _Legacy()
    legacy.closed = True
    assert uw_ws._is_open(legacy) is False


def test_连接已建立后订阅新频道立即补发join而不是崩在closed上(monkeypatch):
    """回归锁死：修复前这里必抛 AttributeError: 'ClientConnection' has no 'closed'。"""
    monkeypatch.setattr(uw_ws, "UW_API_KEY", "fake-key")
    client = uw_ws.UwWebSocketClient()
    fake = _FakeWS()

    async def _run():
        client._ws = fake            # 模拟长连接已经建好

        async def _noop():           # 别让 subscribe 真去起连接循环
            return None

        monkeypatch.setattr(client, "_ensure_loop_started", _noop)

        agen = client.subscribe("flow-alerts")
        task = asyncio.create_task(agen.__anext__())   # 跑到阻塞在 queue.get() 为止
        for _ in range(10):
            await asyncio.sleep(0)
        if task.done() and task.exception() is not None:
            raise task.exception()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await agen.aclose()

    asyncio.run(_run())
    assert any("flow-alerts" in s and "join" in s for s in fake.sent), fake.sent


# ── 2. 正常关闭也要退避，且「握手即断」时退避必须继续涨 ────────────────────────

def _patch_connect_to_instantly_close(monkeypatch, sleeps, stop_after=3):
    async def _fake_sleep(delay):
        sleeps.append(delay)
        if len(sleeps) >= stop_after:
            raise _Stop

    monkeypatch.setattr(uw_ws.asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(uw_ws, "_build_ws_url_and_headers",
                        lambda proxy: ("wss://example.test/socket?token=k", {}))
    monkeypatch.setattr(uw_ws, "_resolve_proxy", lambda: None)
    monkeypatch.setattr(uw_ws.websockets, "connect", lambda *a, **kw: _FakeWS())


def test_对端正常关闭也退避且握手即断时退避指数增长(monkeypatch):
    """修复前：正常关闭走不到任何 sleep，while True 立刻重连 → 全速轰炸中转站。"""
    sleeps: list[float] = []
    _patch_connect_to_instantly_close(monkeypatch, sleeps)

    client = uw_ws.UwWebSocketClient()
    with pytest.raises(_Stop):
        asyncio.run(client._run_loop())

    assert sleeps == [2.0, 4.0, 8.0], f"应指数退避，实际 {sleeps}"


def test_连稳过之后退避重置回初值(monkeypatch):
    """连接活过 _HEALTHY_UPTIME_S 才算连稳过，此时退避该回到初值。"""
    sleeps: list[float] = []
    _patch_connect_to_instantly_close(monkeypatch, sleeps)
    monkeypatch.setattr(uw_ws, "_HEALTHY_UPTIME_S", 0.0)   # 视作每次都连稳了

    client = uw_ws.UwWebSocketClient()
    with pytest.raises(_Stop):
        asyncio.run(client._run_loop())

    assert sleeps == [2.0, 2.0, 2.0], f"连稳过应重置退避，实际 {sleeps}"


# ── 3. 频道预算（中转站单连接上限 12 条）────────────────────────────────────────

def test_频道预算满时拒绝订阅而不是硬发出去(monkeypatch):
    """客户端预检拒绝，省掉一次必然失败的往返——超预算硬发的话服务端会明确返回
    `subscription_limit` 错误帧，不是静默丢弃。"""
    monkeypatch.setattr(uw_ws, "UW_API_KEY", "fake-key")
    monkeypatch.setattr(uw_ws, "MAX_CHANNELS", 3)
    client = uw_ws.UwWebSocketClient()

    async def _run():
        async def _noop():
            return None
        monkeypatch.setattr(client, "_ensure_loop_started", _noop)

        gens, tasks = [], []
        for i in range(5):
            agen = client.subscribe(f"price:T{i}")
            gens.append(agen)
            tasks.append(asyncio.create_task(agen.__anext__()))
        for _ in range(10):
            await asyncio.sleep(0)
        # 预算内的 3 条挂住等数据；超出的 2 条生成器直接结束（StopAsyncIteration）
        refused = sum(1 for t in tasks if t.done() and isinstance(
            t.exception(), StopAsyncIteration))
        # 快照必须在关闭生成器之前取——aclose() 会跑 finally 把频道清掉
        snapshot = set(client._channels)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for g in gens:
            await g.aclose()
        return refused, snapshot

    refused, channels = asyncio.run(_run())
    assert refused == 2, f"应拒绝 2 条，实际 {refused}"
    assert len(channels) == 3, channels


def test_最后一个订阅者退出时发leave把预算还回去(monkeypatch):
    monkeypatch.setattr(uw_ws, "UW_API_KEY", "fake-key")
    client = uw_ws.UwWebSocketClient()
    fake = _FakeWS()

    async def _run():
        client._ws = fake

        async def _noop():
            return None
        monkeypatch.setattr(client, "_ensure_loop_started", _noop)

        agen = client.subscribe("price:SPY")
        task = asyncio.create_task(agen.__anext__())
        for _ in range(10):
            await asyncio.sleep(0)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await agen.aclose()          # 最后一个订阅者退出

    asyncio.run(_run())
    assert any("leave" in s and "price:SPY" in s for s in fake.sent), fake.sent
    assert "price:SPY" not in client._channels   # 预算已释放


# ── 4. 换票（socket-ticket）失败：不回落、不静默 ───────────────────────────────
#
# 2026-08-07 盘中现场：22:23:14 连 `socket?token...` 吃 403、退避 32s，22:23:56 连
# `socket?ticke...` 一切正常。真正失败的是那次换票，可它记在 `logger.debug` 上
# （root logger 是 INFO ⇒ 一行都不打），非 200 分支更是连 debug 都没有。一次静默
# 的换票超时就这样被放大成一条刺眼的 403 加一分钟断线。


class _FakeResp:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("响应不是 JSON")
        return self._payload


def _patch_post(monkeypatch, *, resp=None, exc=None, seen=None):
    def _post(url, **kw):
        if seen is not None:
            seen.append((url, kw))
        if exc is not None:
            raise exc
        return resp

    monkeypatch.setattr(uw_ws.requests, "post", _post)


def test_换票成功时用中转站给的ticket_url(monkeypatch):
    _patch_post(monkeypatch, resp=_FakeResp(
        payload={"websocket_url": "wss://relay.test/socket?ticket=abc"}))
    url, headers = uw_ws._build_ws_url_and_headers(None)
    assert url == "wss://relay.test/socket?ticket=abc"
    assert headers["Origin"] == "https://relay.test"


def test_换票失败不再回落token_URL(monkeypatch):
    """回落 `?token=` 对中转站必吃 403：白烧一轮握手，还盖住真因。"""
    _patch_post(monkeypatch, exc=requests.exceptions.ReadTimeout("read timeout=15.0"))
    with pytest.raises(uw_ws._TicketError):
        uw_ws._build_ws_url_and_headers(None)


def test_换票超时抛错并带上原因(monkeypatch):
    _patch_post(monkeypatch, exc=requests.exceptions.ReadTimeout("read timeout=15.0"))
    with pytest.raises(uw_ws._TicketError, match="read timeout=15.0"):
        uw_ws._get_ticket_ws_url(None)


def test_换票非200不再零日志而是带状态码抛错(monkeypatch):
    """修复前 `if resp.status_code == 200` 之外的分支直接 return None —— 429/502
    这类硬失败连一行 debug 都没有，排查时无从下手。"""
    _patch_post(monkeypatch, resp=_FakeResp(status_code=429, text="rate limited"))
    with pytest.raises(uw_ws._TicketError, match="429"):
        uw_ws._get_ticket_ws_url(None)


def test_换票响应缺websocket_url也要抛错(monkeypatch):
    _patch_post(monkeypatch, resp=_FakeResp(payload={"detail": "no ticket for you"}))
    with pytest.raises(uw_ws._TicketError):
        uw_ws._get_ticket_ws_url(None)


def test_换票超时预算不得低于15秒(monkeypatch):
    """实测走 socks5h 代理空载就要 1.6s，5s 只有 3 倍余量 —— 启动瞬间 tide 播种、
    screener 扫池、两条 WS 各自换票挤在同一个本地 SOCKS 上，捅穿 5s 太容易了。
    market_tide 的播种超时早已是 15.0s，这里对齐。"""
    seen: list = []
    _patch_post(monkeypatch, seen=seen, resp=_FakeResp(
        payload={"websocket_url": "wss://relay.test/socket?ticket=abc"}))
    uw_ws._get_ticket_ws_url(None)
    assert seen and seen[0][1]["timeout"] >= 15.0, seen


def test_换票失败时不发起WS连接而是退避重试(monkeypatch, caplog):
    """拿不到票就没有能连的 URL，直接退避重来；日志必须说清是换票失败。"""
    sleeps: list[float] = []

    async def _fake_sleep(delay):
        sleeps.append(delay)
        if len(sleeps) >= 3:
            raise _Stop

    def _never_connect(*a, **kw):
        raise AssertionError("换不到票就不该发起 WS 连接")

    def _no_ticket(proxy):
        raise uw_ws._TicketError("read timeout=15.0")

    monkeypatch.setattr(uw_ws.asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(uw_ws, "_resolve_proxy", lambda: None)
    monkeypatch.setattr(uw_ws.websockets, "connect", _never_connect)
    monkeypatch.setattr(uw_ws, "_build_ws_url_and_headers", _no_ticket)

    client = uw_ws.UwWebSocketClient()
    with caplog.at_level(logging.ERROR, logger="tt.uw_ws"):
        with pytest.raises(_Stop):
            asyncio.run(client._run_loop())

    assert sleeps == [2.0, 4.0, 8.0], f"应指数退避，实际 {sleeps}"
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "换票" in text, text
    assert "read timeout=15.0" in text, text


# ── 5. monitored subscription 控制面 ─────────────────────────────────────────

class _ScriptedWS(_FakeWS):
    """依次吐出预置帧后正常关闭，用于验证 ACK 与重连代次。"""

    def __init__(self, frames):
        super().__init__()
        self._frames = list(frames)

    async def __anext__(self):
        if self._frames:
            return self._frames.pop(0)
        raise StopAsyncIteration


def _patch_scripted_transport(monkeypatch, connections, fake_sleep):
    sockets = iter(_ScriptedWS(frames) for frames in connections)
    monkeypatch.setattr(uw_ws, "_build_ws_url_and_headers",
                        lambda proxy: ("wss://example.test/socket?ticket=k", {}))
    monkeypatch.setattr(uw_ws, "_resolve_proxy", lambda: None)
    monkeypatch.setattr(uw_ws.websockets, "connect", lambda *a, **kw: next(sockets))
    monkeypatch.setattr(uw_ws.asyncio, "sleep", fake_sleep)


def test_monitored订阅可观察join_ack_断线_重连与关闭(monkeypatch):
    monkeypatch.setattr(uw_ws, "UW_API_KEY", "fake-key")
    channel = "option_trades:SPY"
    ack = f'["{channel}",{{"response":{{}},"status":"ok"}}]'

    async def _stop_on_backoff(_delay):
        raise _Stop

    _patch_scripted_transport(monkeypatch, [[ack]], _stop_on_backoff)

    async def _run():
        client = uw_ws.UwWebSocketClient(group_name="flood#test")

        async def _noop():
            return None

        monkeypatch.setattr(client, "_ensure_loop_started", _noop)
        handle = await client.open_monitored_subscription(channel)
        with pytest.raises(_Stop):
            await client._run_loop()
        await handle.close()
        events = [event async for event in handle.controls()]
        return events, handle.status()

    events, status = asyncio.run(_run())
    event_types = [event["type"] for event in events]
    assert event_types == [
        uw_ws.RECONNECTING,
        uw_ws.JOIN_PENDING,
        uw_ws.ACKED,
        uw_ws.DISCONNECTED,
        uw_ws.RECONNECTING,
        uw_ws.CLOSED,
    ]
    pending = next(event for event in events if event["type"] == uw_ws.JOIN_PENDING)
    acked = next(event for event in events if event["type"] == uw_ws.ACKED)
    assert pending["connection_generation"] == 1
    assert pending["join_requested_at_monotonic"] is not None
    assert acked["connection_generation"] == 1
    assert acked["acked_at_monotonic"] >= pending["join_requested_at_monotonic"]
    assert status["state"] == uw_ws.CLOSED
    assert status["closed"] is True


def test_monitored重连后重新join并产生新的connection_generation(monkeypatch):
    monkeypatch.setattr(uw_ws, "UW_API_KEY", "fake-key")
    channel = "option_trades:QQQ"
    ack = f'["{channel}",{{"response":{{}},"status":"ok"}}]'
    sleeps: list[float] = []

    async def _allow_one_reconnect_then_stop(delay):
        sleeps.append(delay)
        if len(sleeps) >= 2:
            raise _Stop

    _patch_scripted_transport(
        monkeypatch,
        [[ack], [ack]],
        _allow_one_reconnect_then_stop,
    )

    async def _run():
        client = uw_ws.UwWebSocketClient(group_name="flood#test")

        async def _noop():
            return None

        monkeypatch.setattr(client, "_ensure_loop_started", _noop)
        handle = await client.open_monitored_subscription(channel)
        with pytest.raises(_Stop):
            await client._run_loop()
        await handle.close()
        return [event async for event in handle.controls()]

    events = asyncio.run(_run())
    ack_generations = [
        event["connection_generation"]
        for event in events
        if event["type"] == uw_ws.ACKED
    ]
    join_generations = [
        event["connection_generation"]
        for event in events
        if event["type"] == uw_ws.JOIN_PENDING
    ]
    assert ack_generations == [1, 2]
    assert join_generations == [1, 2]
    assert any(
        event["type"] == uw_ws.DISCONNECTED
        and event["connection_generation"] == 1
        for event in events
    )


def test_monitored慢listener丢包不污染同频道快listener(monkeypatch):
    monkeypatch.setattr(uw_ws, "UW_API_KEY", "fake-key")
    monkeypatch.setattr(uw_ws, "_QUEUE_MAXSIZE", 1)

    async def _run():
        client = uw_ws.UwWebSocketClient(group_name="flood#test")

        async def _noop():
            return None

        monkeypatch.setattr(client, "_ensure_loop_started", _noop)
        slow = await client.open_monitored_subscription("option_trades:SPY")
        fast = await client.open_monitored_subscription("option_trades:SPY")
        client._connection_generation = 1
        client._mark_join_pending("option_trades:SPY")
        client._mark_acked("option_trades:SPY")

        client._dispatch_payload("option_trades:SPY", {"id": 1})
        fast_payloads = fast.payloads()
        assert await fast_payloads.__anext__() == {"id": 1}

        # slow 仍压着 id=1，第二帧只挤掉 slow 自己的旧数据；fast 有空位不丢。
        client._dispatch_payload("option_trades:SPY", {"id": 2})
        slow_payloads = slow.payloads()
        assert await slow_payloads.__anext__() == {"id": 2}
        assert await fast_payloads.__anext__() == {"id": 2}
        slow_status = slow.status()
        fast_status = fast.status()

        await slow.close()
        await fast.close()
        slow_events = [event async for event in slow.controls()]
        fast_events = [event async for event in fast.controls()]
        return slow_status, fast_status, slow_events, fast_events

    slow_status, fast_status, slow_events, fast_events = asyncio.run(_run())
    assert slow_status["listener_drop_count"] == 1
    assert fast_status["listener_drop_count"] == 0
    assert any(event["type"] == uw_ws.QUEUE_DROPPED for event in slow_events)
    assert not any(event["type"] == uw_ws.QUEUE_DROPPED for event in fast_events)


def test_monitored动态频道await返回前join且最后一个listener才leave(monkeypatch):
    monkeypatch.setattr(uw_ws, "UW_API_KEY", "fake-key")
    fake = _FakeWS()

    async def _run():
        client = uw_ws.UwWebSocketClient(group_name="flood#test")
        client._ws = fake

        async def _noop():
            return None

        monkeypatch.setattr(client, "_ensure_loop_started", _noop)
        first = await client.open_monitored_subscription("option_trades:SPY")
        second = await client.open_monitored_subscription("option_trades:SPY")
        sent_after_open = list(fake.sent)
        await first.close()
        sent_after_first_close = list(fake.sent)
        await second.close()
        return sent_after_open, sent_after_first_close, list(fake.sent)

    sent_after_open, sent_after_first_close, sent_after_all_close = asyncio.run(_run())
    assert sum('"msg_type": "join"' in frame for frame in sent_after_open) == 1
    assert not any('"msg_type": "leave"' in frame for frame in sent_after_first_close)
    assert sum('"msg_type": "leave"' in frame for frame in sent_after_all_close) == 1


def test_monitored动态join发送失败会强制重连且保留handle(monkeypatch):
    monkeypatch.setattr(uw_ws, "UW_API_KEY", "fake-key")

    class _FailingWS(_FakeWS):
        def __init__(self):
            super().__init__()
            self.close_calls = 0

        async def send(self, _msg):
            raise ConnectionError("socket write failed")

        async def close(self):
            self.close_calls += 1
            self.state = State.CLOSED

    async def _run():
        client = uw_ws.UwWebSocketClient(group_name="flood#test")
        fake = _FailingWS()
        client._ws = fake
        client._connection_generation = 7

        async def _noop():
            return None

        monkeypatch.setattr(client, "_ensure_loop_started", _noop)
        handle = await client.open_monitored_subscription("option_trades:SPY")
        status_before_close = handle.status()
        still_registered = handle in client._monitored_listeners["option_trades:SPY"]
        await handle.close()
        events = [event async for event in handle.controls()]
        return fake.close_calls, status_before_close, still_registered, events

    close_calls, status, still_registered, events = asyncio.run(_run())
    assert close_calls == 1
    assert still_registered is True, "发送失败后不能销毁 handle，重连代次还要复用它"
    assert status["state"] == uw_ws.RECONNECTING
    assert [event["type"] for event in events] == [
        uw_ws.JOIN_PENDING,
        uw_ws.DISCONNECTED,
        uw_ws.RECONNECTING,
        uw_ws.CLOSED,
    ]


def test_monitored_join超过ack_deadline会强制重连(monkeypatch):
    monkeypatch.setattr(uw_ws, "UW_API_KEY", "fake-key")
    monkeypatch.setattr(uw_ws, "_MONITORED_ACK_TIMEOUT_S", 0.01)

    class _ClosableWS(_FakeWS):
        def __init__(self):
            super().__init__()
            self.close_calls = 0

        async def close(self):
            self.close_calls += 1
            self.state = State.CLOSED

    async def _run():
        client = uw_ws.UwWebSocketClient(group_name="flood#test")
        fake = _ClosableWS()
        client._ws = fake
        client._connection_generation = 3

        async def _noop():
            return None

        monkeypatch.setattr(client, "_ensure_loop_started", _noop)
        handle = await client.open_monitored_subscription("option_trades:QQQ")
        assert handle.status()["state"] == uw_ws.JOIN_PENDING
        await asyncio.sleep(0.03)
        status_after_timeout = handle.status()
        await handle.close()
        events = [event async for event in handle.controls()]
        return fake.close_calls, status_after_timeout, events

    close_calls, status, events = asyncio.run(_run())
    assert close_calls == 1
    assert status["state"] == uw_ws.RECONNECTING
    assert any(
        event["type"] == uw_ws.RECONNECTING and "未收到 ACK" in event.get("detail", "")
        for event in events
    )


# 2026-08-13 中转站网关劣化当天的实测（log/ttt.log 全天 132 次 join）：
# join→ACK 中位 4.55s、p90 13.45s、最大 15.50s，而这 132 次 join 最终**全部**收到了
# ACK —— 慢，但没有一次是真丢的。当时阈值 5.0s 正好压在中位数上，49% 的 join 触发它、
# 全天误杀 17 次正常连接（时序上能直接看到 03:25:25 发 join → 03:25:30 强拆 →
# 03:25:32 ACK 才到），而每次误杀都强制重新换票（_TICKET_HTTP_TIMEOUT_S=15s），
# 于是「上游变慢」被我们自己放大成「换票风暴」。
_OBSERVED_SLOWEST_SUCCESSFUL_ACK_S = 15.5


def _capture_connect_kwargs(monkeypatch):
    """跑一轮 _run_loop，录下真正传给 websockets.connect 的参数。"""
    captured: dict = {}
    sleeps: list[float] = []

    async def _fake_sleep(delay):
        sleeps.append(delay)
        raise _Stop

    def _fake_connect(*a, **kw):
        captured.update(kw)
        return _FakeWS()

    monkeypatch.setattr(uw_ws.asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(uw_ws, "_build_ws_url_and_headers",
                        lambda proxy: ("wss://example.test/socket?ticket=k", {}))
    monkeypatch.setattr(uw_ws, "_resolve_proxy", lambda: None)
    monkeypatch.setattr(uw_ws.websockets, "connect", _fake_connect)

    client = uw_ws.UwWebSocketClient()
    with pytest.raises(_Stop):
        asyncio.run(client._run_loop())
    assert captured, "没能捕获到 websockets.connect 的参数"
    return captured


def test_不得因pong迟到自杀式关闭连接(monkeypatch):
    """协议层保活是我们自己加的，官方参考实现根本不发 PING。

    docs/uw/unusual_whales_websocket_full_docs.md L114-146 的官方 Python 示例：
        ws.run_forever(dispatcher=rel, reconnect=5)
    既没传 ping_interval 也没传 ping_timeout —— websocket-client 不传即
    `ping_interval=0`，**一个 PING 都不发**，死连接只靠「对端关闭 → 5s 后重连」。
    全文 1921 行零处提到 heartbeat/ping/pong/keepalive，协议只有 join 与 ACK。

    我们却设了 ping_timeout=10：10 秒收不到 PONG 就自己 close(1011)。2026-08-13
    全天 185 次断线里 **74 次**是这么自杀的（日志原文 `sent 1011 ... keepalive ping
    timeout`，`sent` 说明是我方发起）。而我们走的是第三方中转站，协议控制帧正是这类
    中间层最容易吞掉或延迟的东西 —— 官方客户端压根不会有这个失败模式。

    每次自杀都要重新换票（_TICKET_HTTP_TIMEOUT_S=15s），于是「上游慢」被我们自己
    放大成「换票风暴」，与 ACK 看门狗那次是同一个形态（见上方 5.0s 误杀的教训）。
    """
    kwargs = _capture_connect_kwargs(monkeypatch)
    assert kwargs.get("ping_timeout") is None, (
        f"ping_timeout={kwargs.get('ping_timeout')!r}：PONG 迟到就自杀式关连接。"
        f"官方参考实现不发 PING，中转站又常年慢于该阈值 —— 必须设为 None"
    )


def test_完全不发ping以对齐官方参考实现(monkeypatch):
    """一并停发 PING —— 本条**推翻**了同日更早的「保留 ping_interval」判断。

    当时的理由是「PING 是廉价保活，别把保活流量也删掉」。23:14 的实测把它证伪了：
    改成 ping_timeout=None 之后，断连日志的措辞从
        `sent 1011 (internal error) keepalive ping timeout`
    变成
        `received 1011 (internal error) keepalive ping timeout; then sent 1011`
    —— `sent`→`received`：我方确实不再自杀了，但换成**中转站主动踢我们**，理由是
    它收不到我们的 PONG。也就是说 PING/PONG 控制帧在 SOCKS 代理或中转站那一层
    本身就传不可靠；我们「只发 PING 不等 PONG」反而给中间层塞了它处理不好的帧。

    官方参考实现（docs/uw/unusual_whales_websocket_full_docs.md L114-146）
    `run_forever(dispatcher=rel, reconnect=5)` 不传 ping_interval，即 websocket-client
    的 0 —— **一帧 PING 都不发**。全文 1921 行零处提 heartbeat/ping/pong/keepalive。
    彻底对齐它。

    代价（已知并接受）：没有协议层保活，空闲连接依赖中转站自身策略；且连接级死链
    检测仍缺位（last_payload_at_monotonic 无看门狗消费）。若观测到挂死连接，补的应是
    **区分盘中/盘后**的数据静默看门狗，而不是把 PING 加回来。
    """
    kwargs = _capture_connect_kwargs(monkeypatch)
    assert kwargs.get("ping_interval") is None, (
        f"ping_interval={kwargs.get('ping_interval')!r}：仍在发 PING。中转站会因收不到"
        f"我方 PONG 而回 1011 踢连接（23:08/23:12 实测），官方参考实现一帧都不发"
    )


def test_ack看门狗阈值必须高于实测最慢的成功ack():
    """看门狗只该抓「永远不 ACK 的死订阅」，不该充当上游慢速的裁判。"""
    assert uw_ws._MONITORED_ACK_TIMEOUT_S > _OBSERVED_SLOWEST_SUCCESSFUL_ACK_S, (
        f"阈值 {uw_ws._MONITORED_ACK_TIMEOUT_S}s 低于实测最慢的**成功** ACK "
        f"{_OBSERVED_SLOWEST_SUCCESSFUL_ACK_S}s：中转站一变慢就会误杀正常连接，"
        f"并触发换票风暴"
    )


def test_ack在deadline之前到达就不该强拆连接(monkeypatch):
    """慢 ACK 只要没越过 deadline 就必须放行——2026-08-13 误杀 17 次的直接教训。"""
    monkeypatch.setattr(uw_ws, "UW_API_KEY", "fake-key")
    monkeypatch.setattr(uw_ws, "_MONITORED_ACK_TIMEOUT_S", 0.20)

    class _ClosableWS(_FakeWS):
        def __init__(self):
            super().__init__()
            self.close_calls = 0

        async def close(self):
            self.close_calls += 1
            self.state = State.CLOSED

    async def _run():
        client = uw_ws.UwWebSocketClient(group_name="global#test")
        fake = _ClosableWS()
        client._ws = fake
        client._connection_generation = 1

        async def _noop():
            return None

        monkeypatch.setattr(client, "_ensure_loop_started", _noop)
        handle = await client.open_monitored_subscription("flow-alerts")
        await asyncio.sleep(0.10)      # 慢，但还没到 deadline
        client._mark_acked("flow-alerts")
        await asyncio.sleep(0.20)      # 越过原 deadline，确认看门狗不会迟到开火
        status_after = handle.status()
        await handle.close()
        return fake.close_calls, status_after

    close_calls, status = asyncio.run(_run())
    assert close_calls == 0, "ACK 已经到了还强拆连接，就是误杀"
    assert status["state"] == uw_ws.ACKED


def test_旧subscribe的join发送失败仍保持payload流兼容(monkeypatch):
    monkeypatch.setattr(uw_ws, "UW_API_KEY", "fake-key")

    class _FailingWS(_FakeWS):
        async def send(self, _msg):
            raise ConnectionError("legacy send failed")

    async def _run():
        client = uw_ws.UwWebSocketClient(group_name="global#test")
        client._ws = _FailingWS()

        async def _noop():
            return None

        monkeypatch.setattr(client, "_ensure_loop_started", _noop)
        stream = client.subscribe("flow-alerts")
        pending = asyncio.create_task(stream.__anext__())
        for _ in range(5):
            await asyncio.sleep(0)
        assert not pending.done(), "旧接口发送失败后仍应是等待 payload 的异步流"
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        await stream.aclose()
        return client._ack_deadline_tasks

    assert asyncio.run(_run()) == {}


def test_monitored分配失败返回可观察rejected而非空流(monkeypatch):
    monkeypatch.setattr(uw_ws, "UW_API_KEY", "")
    pool = uw_ws.UwWsPool()
    monkeypatch.setattr(uw_ws, "UW_WS_POOL", pool)

    async def _run():
        handle = await uw_ws.open_monitored_subscription("option_trades:SPY")
        rejected_status = handle.status()
        payloads = [payload async for payload in handle.payloads()]
        await handle.close()
        controls = [event async for event in handle.controls()]
        return rejected_status, payloads, controls

    rejected_status, payloads, controls = asyncio.run(_run())
    assert rejected_status["state"] == uw_ws.REJECTED
    assert payloads == []
    assert [event["type"] for event in controls] == [uw_ws.REJECTED, uw_ws.CLOSED]
    assert pool._clients == {}, "缺 key 时不应先占连接池预留"


def test_旧subscribe仍是纯payload流且不会混入控制事件(monkeypatch):
    monkeypatch.setattr(uw_ws, "UW_API_KEY", "fake-key")

    async def _run():
        client = uw_ws.UwWebSocketClient()

        async def _noop():
            return None

        monkeypatch.setattr(client, "_ensure_loop_started", _noop)
        stream = client.subscribe("flow-alerts")
        first = asyncio.create_task(stream.__anext__())
        for _ in range(5):
            await asyncio.sleep(0)
        client._dispatch_payload("flow-alerts", {"id": "alert-1"})
        payload = await first
        await stream.aclose()
        return payload

    assert asyncio.run(_run()) == {"id": "alert-1"}
