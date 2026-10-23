# -*- coding: utf-8 -*-
"""盯盘 UW 实时正股行情（`price:{ticker}`）回归测试。

锁死 2026-08-05 实测查出的三处缺陷——旧实现三条全错，且第三条最阴：
前两条错了会直接连不上、一眼看出来；第三条会连上并跑得好好的，只是量能数据全是错的。

  1. 订阅帧发 Phoenix 五元组 → 中转站直接拒 `Expected a JSON object.`
  2. 频道名用 `stock_trades:SPY` → `Invalid payload`；正确是 `price:SPY`
  3. 把 payload 的 `vol`（**当日累计成交量**）当单笔量喂进 `_ingest_tick`
"""
import asyncio

import pytest

import app.watcher.quote_loops as ql


class _Harness(ql.QuoteLoopsMixin):
    """只装配 _uw_price_channel / _uw_ws_quote_loop 需要的那几个协作者。"""

    def __init__(self, tickers):
        self._watchers = {t: object() for t in tickers}
        self._running = True
        self.ingested: list[tuple[str, float, int]] = []

    async def _ingest_tick(self, ticker, price, tick_vol):
        self.ingested.append((ticker, price, tick_vol))


def _fake_subscribe(monkeypatch, payloads_by_channel):
    """替换 uw_ws.subscribe，按频道回放固定报文后结束。"""
    import app.integrations.uw_ws as uw_ws

    async def _sub(channel):
        for p in payloads_by_channel.get(channel, []):
            yield p

    monkeypatch.setattr(uw_ws, "subscribe", _sub)


def _px(close, vol):
    # 真机形状：close 是字符串，time 是 epoch 毫秒，没有 ticker 字段
    return {"close": str(close), "time": 1785941525000, "vol": vol}


# ── 缺陷 3：vol 是当日累计量，必须差分 ────────────────────────────────────────

def test_累计成交量差分成单笔量而不是直接当单笔量用(monkeypatch):
    """旧实现会把 12,720,002 当成一笔的成交量灌进量能计算。"""
    _fake_subscribe(monkeypatch, {"price:SPY": [
        _px(773.85, 12_720_002),
        _px(773.88, 12_722_869),
        _px(773.96, 12_724_589),
    ]})
    h = _Harness(["SPY"])
    asyncio.run(h._uw_price_channel("SPY"))

    assert h.ingested == [
        ("SPY", 773.85, 0),        # 首条没有增量基准 → 只记价不记量
        ("SPY", 773.88, 2_867),    # 12,722,869 - 12,720,002
        ("SPY", 773.96, 1_720),    # 12,724,589 - 12,722,869
    ]
    # 回归红线：绝不能把累计量原样当单笔量传出去
    assert all(v < 10_000 for _, _, v in h.ingested), h.ingested


def test_累计量回退视为跨交易日重置(monkeypatch):
    _fake_subscribe(monkeypatch, {"price:SPY": [
        _px(700.0, 90_000_000),    # 昨日尾盘
        _px(701.0, 120_000),       # 今日开盘，累计量重置
        _px(702.0, 125_000),
    ]})
    h = _Harness(["SPY"])
    asyncio.run(h._uw_price_channel("SPY"))

    assert [v for _, _, v in h.ingested] == [0, 120_000, 5_000]


def test_字段异常与非正价格的报文被跳过(monkeypatch):
    _fake_subscribe(monkeypatch, {"price:SPY": [
        _px(773.85, 1_000),
        {"time": 1, "vol": 2},            # 缺 close
        {"close": "abc", "vol": 3},       # 坏值
        _px(0, 5_000),                    # 价格非正
        _px(774.00, 1_500),
    ]})
    h = _Harness(["SPY"])
    asyncio.run(h._uw_price_channel("SPY"))

    assert [t for t, _, _ in h.ingested] == ["SPY", "SPY"]
    assert h.ingested[-1] == ("SPY", 774.0, 500)


def test_ticker来自频道名而非payload(monkeypatch):
    """payload 里根本没有 ticker 字段，只能靠订阅时的频道名带。"""
    _fake_subscribe(monkeypatch, {"price:AAPL": [_px(310.51, 100), _px(310.55, 250)]})
    h = _Harness(["AAPL"])
    asyncio.run(h._uw_price_channel("AAPL"))

    assert {t for t, _, _ in h.ingested} == {"AAPL"}
    assert "ticker" not in _px(1, 1)


# ── 容量硬约束：单连接 12 条频道，price 是一标的一条 ──────────────────────────

def test_盯盘池超出频道预算时拒绝启动而不是只订前N只(monkeypatch, caplog):
    """静默只订前 N 只 = 看着在跑、实际大部分标的没行情，比不跑更危险。"""
    import app.integrations.uw_ws as uw_ws
    monkeypatch.setattr(uw_ws, "remaining_capacity", lambda group: 72)
    monkeypatch.setattr(ql, "UW_API_KEY", "fake-key")
    monkeypatch.setattr(ql, "_UW_POOL_RECONCILE_S", 0.01)

    started: list[str] = []

    async def _never_ending(self, ticker):
        started.append(ticker)
        await asyncio.Event().wait()

    monkeypatch.setattr(ql.QuoteLoopsMixin, "_uw_price_channel", _never_ending)

    h = _Harness([f"T{i}" for i in range(153)])   # 真实盯盘池规模

    async def _run():
        task = asyncio.create_task(h._uw_ws_quote_loop())
        await asyncio.sleep(0.05)
        h._running = False
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(_run())

    assert started == [], f"超预算时不该订阅任何频道，实际订了 {started[:5]}"
    assert any("拒绝启动" in r.message for r in caplog.records), \
        [r.message for r in caplog.records]


def test_池子在预算内时逐只订阅price频道(monkeypatch):
    import app.integrations.uw_ws as uw_ws
    monkeypatch.setattr(uw_ws, "remaining_capacity", lambda group: 72)
    monkeypatch.setattr(ql, "UW_API_KEY", "fake-key")
    monkeypatch.setattr(ql, "_UW_POOL_RECONCILE_S", 0.01)

    started: list[str] = []

    async def _never_ending(self, ticker):
        started.append(ticker)
        await asyncio.Event().wait()

    monkeypatch.setattr(ql.QuoteLoopsMixin, "_uw_price_channel", _never_ending)

    h = _Harness(["SPY", "QQQ", "^VIX"])          # ^VIX 是指数，不该订

    async def _run():
        task = asyncio.create_task(h._uw_ws_quote_loop())
        await asyncio.sleep(0.05)
        h._running = False
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(_run())

    assert sorted(started) == ["QQQ", "SPY"], started


def test_满12只标的现在在预算内(monkeypatch):
    """回归锁死 off-by-one：`market_tide` 迁到 `global` 组后，`ticker` 组不必再为它
    预留，盯盘池上限从 11 只回到满 12 只。修复前第 12 只会被判超预算、整路拒绝启动。"""
    import app.integrations.uw_ws as uw_ws
    monkeypatch.setattr(uw_ws, "remaining_capacity", lambda group: 72)
    monkeypatch.setattr(ql, "UW_API_KEY", "fake-key")
    monkeypatch.setattr(ql, "_UW_POOL_RECONCILE_S", 0.01)

    started: list[str] = []

    async def _never_ending(self, ticker):
        started.append(ticker)
        await asyncio.Event().wait()

    monkeypatch.setattr(ql.QuoteLoopsMixin, "_uw_price_channel", _never_ending)

    tickers = [f"T{i}" for i in range(12)]
    h = _Harness(tickers)

    async def _run():
        task = asyncio.create_task(h._uw_ws_quote_loop())
        await asyncio.sleep(0.05)
        h._running = False
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(_run())

    assert sorted(started) == sorted(tickers), \
        f"满 12 只应全部订上，实际只订了 {len(started)} 只"


def test_缺key时直接返回不启动(monkeypatch):
    monkeypatch.setattr(ql, "UW_API_KEY", "")
    h = _Harness(["SPY"])
    asyncio.run(asyncio.wait_for(h._uw_ws_quote_loop(), timeout=1.0))


def test_盯盘池72只在容量内而73只到顶被拒(monkeypatch, caplog):
    """锁死新阈值：本次改造把可行区间从 12 只扩到 72 只，但**不是无限**。
    真实盯盘池 153 只（见本文件 test_盯盘池超出频道预算时拒绝启动 的用例规模）
    装不进 UW 的 price 通道，必须明确报错而不是半跑。"""
    import app.integrations.uw_ws as uw_ws
    monkeypatch.setattr(uw_ws, "remaining_capacity", lambda group: 72)
    monkeypatch.setattr(ql, "UW_API_KEY", "fake-key")
    monkeypatch.setattr(ql, "_UW_POOL_RECONCILE_S", 0.01)

    started: list[str] = []

    async def _never_ending(self, ticker):
        started.append(ticker)
        await asyncio.Event().wait()

    monkeypatch.setattr(ql.QuoteLoopsMixin, "_uw_price_channel", _never_ending)

    async def _run(h):
        task = asyncio.create_task(h._uw_ws_quote_loop())
        await asyncio.sleep(0.05)
        h._running = False
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(_run(_Harness([f"T{i}" for i in range(72)])))
    assert len(started) == 72, f"72 只应全部订上，实际 {len(started)}"

    started.clear()
    asyncio.run(_run(_Harness([f"T{i}" for i in range(73)])))
    assert started == [], f"73 只超容量时不该订阅任何频道，实际订了 {started[:5]}"
    assert any("拒绝启动" in r.message for r in caplog.records)
