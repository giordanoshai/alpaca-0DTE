# -*- coding: utf-8 -*-
"""UW WebSocket 连接池与分组路由的测试。

背景（2026-08-06 盘外实测，见 docs/superpowers/specs/2026-08-06-uw-ws-multi-connection-design.md）：
中转站的 12 条频道限额是**每连接**的（服务端错误原文 `Maximum 12 channels per
connection.`），3 条并发连接可用且互不踢。故按用途分组多连接，既隔离故障也扩预算。
"""
import asyncio
import logging

import app.integrations.uw_ws as uw_ws


# ── 分组路由 ──────────────────────────────────────────────────────────────────

def test_洪水频道归flood组():
    """高频洪水必须独占连接：async for 是串行的，它们会堵死同连接上的其他频道。"""
    for ch in ("option_trades", "lit_trades", "off_lit_trades",
               "gex_strike", "gex_strike_expiry"):
        assert uw_ws.group_of(ch) == uw_ws.GROUP_FLOOD, ch


def test_单标的期权流仍归flood组而不是ticker组():
    """option_trades:SPY 带冒号，但 0DTE 日单标的流量依然巨大，
    落 ticker 组会把 price 那条连接堵死，必须靠前缀表挑出来。"""
    assert uw_ws.group_of("option_trades:SPY") == uw_ws.GROUP_FLOOD


def test_单标的gex_strike变体也归flood组而不是ticker组():
    """gex_strike:SPY / gex_strike_expiry:SPY 是「每个 strike 的实时 GEX 更新」，
    与 option_trades 同理都是高频推送。落进 ticker 组会堵死盯盘池那条承载
    price:{ticker} 的连接，必须靠前缀表挑出来归 flood 组。"""
    assert uw_ws.group_of("gex_strike:SPY") == uw_ws.GROUP_FLOOD
    assert uw_ws.group_of("gex_strike_expiry:SPY") == uw_ws.GROUP_FLOOD


def test_全局低频频道归global组():
    for ch in ("market_tide", "flow-alerts", "news", "trading_halts",
               "gex", "interval_flow", "contract_screener", "custom_alerts",
               "periscope"):
        assert uw_ws.group_of(ch) == uw_ws.GROUP_GLOBAL, ch


def test_带ticker的频道归ticker组():
    for ch in ("price:SPY", "net_flow:QQQ", "gex:SPY", "periscope:SPX"):
        assert uw_ws.group_of(ch) == uw_ws.GROUP_TICKER, ch


def test_未知频道兜底进ticker组():
    """新频道加进来时不该崩，兜底到标的组（那组预算最灵活）。"""
    assert uw_ws.group_of("brand_new_channel_2027") == uw_ws.GROUP_TICKER


def test_真实频道名是连字符的flow_alerts():
    """flow-alerts 是全项目唯一用连字符的频道名，写成下划线会被服务端拒。
    路由表里只能有连字符那个，否则错误写法会被静默当成已知频道。"""
    assert "flow-alerts" in uw_ws._EXACT_GROUP
    assert "flow_alerts" not in uw_ws._EXACT_GROUP


# ── 连接池 ────────────────────────────────────────────────────────────────────

def test_同组只建一个client实例():
    """一组一条连接。重复建实例 = 重复建连接 = 白烧一份 12 条预算。"""
    pool = uw_ws.UwWsPool()
    a = pool.client_for("price:SPY")
    b = pool.client_for("price:QQQ")
    assert a is b


def test_不同组建不同client():
    pool = uw_ws.UwWsPool()
    flood = pool.client_for("option_trades")
    glob = pool.client_for("market_tide")
    ticker = pool.client_for("price:SPY")
    assert len({id(flood), id(glob), id(ticker)}) == 3


def test_没人订阅的组不建client实例():
    """惰性建连的前半段：只订全局频道时，池子里不该冒出洪水组和标的组的分片。"""
    pool = uw_ws.UwWsPool()
    pool.client_for("market_tide")
    assert set(pool._clients) == {"global#0"}
    assert uw_ws.GROUP_FLOOD not in pool._shards
    assert uw_ws.GROUP_TICKER not in pool._shards





def test_每组各自独立的12条预算():
    """核心：限额是每连接的，各组各订满 12 条互不干扰、都不触发扩容。"""
    pool = uw_ws.UwWsPool()
    for i in range(12):
        pool.client_for(f"price:T{i}")
    pool.client_for("market_tide")

    assert len(pool._reserved["ticker#0"]) == 12
    assert len(pool._reserved["global#0"]) == 1
    assert pool._shards[uw_ws.GROUP_TICKER] == ["ticker#0"], "12 条正好装下，不该扩容"


def test_模块级subscribe按频道路由到对应组(monkeypatch):
    """subscribe() 的签名与模块级形态不能变——两个消费方都靠局部导入它。"""
    routed: list[tuple[str, str]] = []

    class _FakeClient:
        def __init__(self, group_name="default", on_release=None):
            self._group_name = group_name

        def subscribe(self, channel):
            routed.append((self._group_name, channel))

            async def _empty():
                return
                yield       # pragma: no cover — 使其成为异步生成器
            return _empty()

    monkeypatch.setattr(uw_ws, "UwWebSocketClient", _FakeClient)
    monkeypatch.setattr(uw_ws, "UW_WS_POOL", uw_ws.UwWsPool())

    uw_ws.subscribe("option_trades")
    uw_ws.subscribe("market_tide")
    uw_ws.subscribe("price:SPY")

    assert routed == [
        ("flood#0", "option_trades"),
        ("global#0", "market_tide"),
        ("ticker#0", "price:SPY"),
    ]


def test_status按分片汇报而不是按组合并():
    """多分片下按组汇总会把「哪条连接断了」这个唯一有用的信息抹平。"""
    pool = uw_ws.UwWsPool()
    for i in range(13):
        pool.client_for(f"price:T{i}")
    pool._clients["ticker#0"]._channels.add("price:T0")

    st = pool.status()
    assert set(st) == {"ticker#0", "ticker#1"}
    assert st["ticker#0"]["channels"] == ["price:T0"]
    assert st["ticker#0"]["connected"] is False       # 没真连过


def test_到顶时subscribe产出空流而不是崩(monkeypatch, caplog):
    """client_for 返回 None 时，模块级 subscribe 直接 .subscribe() 会 AttributeError。
    必须给出立即结束的空流，让消费方的 async for 正常收尾、走自己的重订阅退避。"""
    pool = uw_ws.UwWsPool()
    monkeypatch.setattr(uw_ws, "UW_WS_POOL", pool)
    for i in range(uw_ws.MAX_CONNECTIONS * uw_ws.MAX_CHANNELS):
        pool.client_for(f"price:T{i}")

    async def _drain():
        return [x async for x in uw_ws.subscribe("price:OVERFLOW")]

    with caplog.at_level(logging.ERROR, logger="tt.uw_ws"):
        assert asyncio.run(_drain()) == []
    assert any("连接数已达全局上限" in r.message for r in caplog.records)


def test_组内订满12条后自动开新分片():
    """12 条是**每连接**预算。订满就该开第二条连接，而不是拒绝——
    这是本次改造的核心：预算不再是需要提前算的稀缺资源。"""
    pool = uw_ws.UwWsPool()
    first = [pool.client_for(f"price:T{i}") for i in range(12)]
    assert len({id(c) for c in first}) == 1, "前 12 条必须挤在同一条连接上"

    thirteenth = pool.client_for("price:T12")
    assert thirteenth is not None
    assert thirteenth is not first[0], "第 13 条必须落到新分片"
    assert sorted(pool._shards[uw_ws.GROUP_TICKER]) == ["ticker#0", "ticker#1"]


def test_分片名带序号用于日志区分():
    pool = uw_ws.UwWsPool()
    c = pool.client_for("option_trades")
    assert c._group_name == "flood#0"
    assert "flood#0" in c._tag


def test_频道粘住分片不会被重新分配():
    """同一频道第二次 client_for 必须回原分片，否则重连后 rejoin 会乱套。"""
    pool = uw_ws.UwWsPool()
    for i in range(12):
        pool.client_for(f"price:T{i}")
    pool.client_for("price:T12")          # 开了 ticker#1

    assert pool.client_for("price:T0") is pool._clients["ticker#0"]
    assert pool.client_for("price:T12") is pool._clients["ticker#1"]


def test_到全局连接硬顶时拒绝分配并打ERROR(caplog):
    """绝不静默半跑：到顶要明确拒绝，且日志要能看出差多少。"""
    pool = uw_ws.UwWsPool()
    for i in range(uw_ws.MAX_CONNECTIONS * uw_ws.MAX_CHANNELS):
        assert pool.client_for(f"price:T{i}") is not None
    assert len(pool._clients) == uw_ws.MAX_CONNECTIONS

    with caplog.at_level(logging.ERROR, logger="tt.uw_ws"):
        assert pool.client_for("price:OVERFLOW") is None

    msgs = [r.message for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("连接数已达全局上限" in m for m in msgs), msgs
    assert any("price:OVERFLOW" in m for m in msgs), msgs
    assert any("ticker#0(12)" in m for m in msgs), "错误日志必须带分片占用明细"


def test_并发分配不会把同一分片塞成13条():
    """client_for 是同步的、中间无 await，所以它自身原子。本条锁死的是
    「占用计数走 _reserved 而不是 client._channels」——后者在 subscribe() 的
    await 之后才写，两个 task 并发时会双双读到 11、双双塞进去。"""
    pool = uw_ws.UwWsPool()
    for i in range(11):
        pool.client_for(f"price:T{i}")

    a = pool.client_for("price:X")        # 第 12 条，填满 ticker#0
    b = pool.client_for("price:Y")        # 第 13 条，必须去 ticker#1
    assert a is pool._clients["ticker#0"]
    assert b is pool._clients["ticker#1"]
    assert len(pool._reserved["ticker#0"]) == 12


def test_洪水频道与全局频道永不落同一分片():
    """扩容不能破坏隔离：async for 串行，option_trades（427 条/秒均值）
    和 market_tide 挤同一条连接等于拿全市场期权流去堵资金潮徽章。"""
    pool = uw_ws.UwWsPool()
    flood = pool.client_for("option_trades")
    glob = pool.client_for("market_tide")
    assert flood is not glob
    assert pool._owner["option_trades"].startswith(uw_ws.GROUP_FLOOD)
    assert pool._owner["market_tide"].startswith(uw_ws.GROUP_GLOBAL)


def test_最后一个订阅者退出后归还频道预留():
    """不归还的话频道只 leave 不释放预留，分片假性占满、逼池子白开新连接。"""
    pool = uw_ws.UwWsPool()
    client = pool.client_for("market_tide")
    assert pool._reserved["global#0"] == {"market_tide"}

    client._on_release("market_tide")

    assert pool._reserved["global#0"] == set()
    assert "market_tide" not in pool._owner


def test_归还后该分片能再接新频道():
    """归还的预算要能真正被复用，否则等于没归还。"""
    pool = uw_ws.UwWsPool()
    for i in range(12):
        pool.client_for(f"price:T{i}")
    assert pool._shards[uw_ws.GROUP_TICKER] == ["ticker#0"]

    pool._clients["ticker#0"]._on_release("price:T0")
    pool.client_for("price:NEW")

    assert pool._shards[uw_ws.GROUP_TICKER] == ["ticker#0"], "有空位就不该开新分片"
    assert pool._owner["price:NEW"] == "ticker#0"


def test_订阅者退出时经由回调归还池内预留(monkeypatch):
    """tests/test_uw_ws.py:176 已验「退出时向服务端发 leave」；本条验池侧的预留
    也一并归还——不归还的话频道只 leave 不释放预留，分片假性占满、白开新连接。"""
    monkeypatch.setattr(uw_ws, "UW_API_KEY", "fake-key")
    pool = uw_ws.UwWsPool()
    client = pool.client_for("price:SPY")
    fake = _FrameWS([])
    assert pool._reserved["ticker#0"] == {"price:SPY"}

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
    assert pool._reserved["ticker#0"] == set(), "预留必须归还"
    assert "price:SPY" not in pool._owner


def test_剩余容量按现有空位加可开连接数计算():
    """取代申报制的反向查询：模块问池子还剩多少，池子是唯一权威。"""
    pool = uw_ws.UwWsPool()
    assert pool.remaining_capacity(uw_ws.GROUP_TICKER) == \
        uw_ws.MAX_CONNECTIONS * uw_ws.MAX_CHANNELS      # 空池 = 8×12 = 96

    pool.client_for("off_lit_trades")     # flood#0
    pool.client_for("market_tide")        # global#0
    assert pool.remaining_capacity(uw_ws.GROUP_TICKER) == 72, \
        "flood/global 各占 1 条后，ticker 组只剩 6 条连接 × 12 = 72"


def test_剩余容量在组订满后归零():
    pool = uw_ws.UwWsPool()
    for i in range(uw_ws.MAX_CONNECTIONS * uw_ws.MAX_CHANNELS):
        pool.client_for(f"price:T{i}")
    assert pool.remaining_capacity(uw_ws.GROUP_TICKER) == 0


def test_模块级remaining_capacity转发到全局池(monkeypatch):
    pool = uw_ws.UwWsPool()
    monkeypatch.setattr(uw_ws, "UW_WS_POOL", pool)
    pool.client_for("market_tide")
    assert uw_ws.remaining_capacity(uw_ws.GROUP_TICKER) == 84   # 7 条 × 12


# ── 服务端错误帧 ──────────────────────────────────────────────────────────────

import pytest
from websockets.protocol import State


class _Stop(Exception):
    """把被测的无限重连循环从外部停下来。"""


class _FrameWS:
    """吐出预置帧后即结束迭代（模拟对端正常关闭）。"""

    def __init__(self, frames):
        self.state = State.OPEN
        self.sent: list[str] = []
        self._frames = list(frames)

    async def send(self, msg):
        self.sent.append(msg)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._frames:
            return self._frames.pop(0)
        raise StopAsyncIteration

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _run_loop_once(monkeypatch, client, frames):
    """让 client._run_loop() 收完 frames 后在第一次退避处停下。"""
    async def _fake_sleep(delay):
        raise _Stop

    monkeypatch.setattr(uw_ws.asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(uw_ws, "_build_ws_url_and_headers",
                        lambda proxy: ("wss://example.test/socket?token=k", {}))
    monkeypatch.setattr(uw_ws, "_resolve_proxy", lambda: None)
    monkeypatch.setattr(uw_ws.websockets, "connect",
                        lambda *a, **kw: _FrameWS(frames))
    with pytest.raises(_Stop):
        asyncio.run(client._run_loop())


def test_超预算错误帧必须打ERROR而不是被静默吞掉(monkeypatch, caplog):
    """2026-08-06 实测：超预算时服务端返回的是 dict 形态错误帧。
    修复前 _run_loop 只处理 list，这条帧被 json 解析后直接落地无人过问——
    超预算、频道名写错全都无声无息。"""
    client = uw_ws.UwWebSocketClient(group_name="ticker")
    frame = ('{"error":"subscription_limit",'
             '"message":"Maximum 12 channels per connection."}')
    with caplog.at_level(logging.ERROR, logger="tt.uw_ws"):
        _run_loop_once(monkeypatch, client, [frame])

    errors = [r.message for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("subscription_limit" in m for m in errors), errors
    assert any("ticker" in m for m in errors), "错误日志必须带组名，否则多连接下无从归因"


def test_频道名写错的错误帧也要打出来(monkeypatch, caplog):
    """把 flow-alerts 误写成 flow_alerts 时服务端的应答。"""
    client = uw_ws.UwWebSocketClient(group_name="global")
    frame = '{"error":"Invalid payload, ignoring message"}'
    with caplog.at_level(logging.ERROR, logger="tt.uw_ws"):
        _run_loop_once(monkeypatch, client, [frame])

    assert any("Invalid payload" in r.message for r in caplog.records)


def test_正常数据帧仍然照常分发不受错误帧分支影响(monkeypatch):
    """回归护栏：加了 dict 分支别把 list 数据帧带沟里。"""
    client = uw_ws.UwWebSocketClient(group_name="global")
    q: asyncio.Queue = asyncio.Queue()
    client._listeners["market_tide"] = {q}
    frame = '["market_tide",{"net_volume":2408}]'
    _run_loop_once(monkeypatch, client, [frame])

    assert q.get_nowait() == {"net_volume": 2408}


def test_收帧后记录时刻供事件类频道判活(monkeypatch):
    """EVENT 类频道（news/trading_halts）不能按数据 age 判活——
    最近一条新闻是 2 小时前很正常。判活要看连接层有没有在收帧。"""
    client = uw_ws.UwWebSocketClient(group_name="global")
    assert client._last_frame_at is None
    _run_loop_once(monkeypatch, client, ['["market_tide",{"net_volume":1}]'])
    assert client._last_frame_at is not None
