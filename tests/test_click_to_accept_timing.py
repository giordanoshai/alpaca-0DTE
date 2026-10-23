# -*- coding: utf-8 -*-
"""补上「点击 → trade_intent_accepted」这段唯一的盲区。

现状：事件链从 `trade_intent_accepted` 起是完整的（08-11 实测一笔 29 个事件毫秒级衔接），
但它是在**工作线程里** `execute_submit` 开头才打的。在那之前的
`click → fetch → uvicorn → FastAPI 路由 → asyncio.to_thread 调度 → 工作线程启动`
全程无记录。本机网络确实 <1ms，但事件循环与默认线程池是全局共享的
（`_positions_loop` 0.5s、`TradingStateHub.run()` 0.5s、`option_candidates._discover`、
WS hub 广播、以及 `execute_run` 自己最长占用工作线程 8.5s），负载下是否还是 1ms 没有数据。

两个数，都**只在各自的钟内自减**，绝不跨钟相减（同 `broker_ts` 那条纪律）：

  route_wait_ms     服务端 perf_counter：路由入口 → 工作线程真正开始跑 execute_submit
                    = asyncio.to_thread 的调度排队
  client_submit_ms  浏览器 performance.now：fetch 发出 → 响应到达
                    = 网络 + uvicorn + 事件循环 + route_wait + execute_submit 本身

两者相减即可把「事件循环排队」与「线程池排队」分开。
"""
from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app.exec_entry as exec_entry
import app.main as m
import app.routers.orders as orders
from app.buy_intent import BUY_INTENTS
from frontend_src import app_js

client = TestClient(m.app)


@pytest.fixture()
def audits(monkeypatch) -> list[dict]:
    events: list[dict] = []

    def _audit(event: str, **fields):
        events.append({"event": event, **fields})

    # exec_entry 与 orders 都是 `from app import trade_recorder`（同一个模块对象），
    # 且在调用点才解析 .audit，故打这一处即可覆盖两边。
    monkeypatch.setattr("app.trade_recorder.audit", _audit)
    return events


def _wire(monkeypatch, *, route_delay_s: float = 0.0):
    """让 to_thread 的调度真的慢一点，验证耗时记到了 route_wait_ms 上。"""
    src = SimpleNamespace(name="watcher")
    monkeypatch.setattr(orders, "current_source", lambda: src)
    monkeypatch.setattr(orders, "_schedule_bg", lambda *_a, **_k: None)

    real = exec_entry.execute_submit

    def _slow(source, req, route_started=None):
        if route_delay_s:
            time.sleep(route_delay_s)
        return real(source, req, route_started)

    monkeypatch.setattr(exec_entry, "execute_submit", _slow)
    return src


def _accepted(events):
    hits = [e for e in events if e["event"] == "trade_intent_accepted"]
    assert len(hits) == 1, f"应恰好一条 trade_intent_accepted，实得 {len(hits)}"
    return hits[0]


# ── A. 服务端：线程池调度排队 ───────────────────────────────────────────────
def test_受理事件带出线程池调度耗时(monkeypatch, audits):
    _wire(monkeypatch)

    r = client.post("/api/execute-submit",
                    json={"ticker": "QQQ", "direction": "long", "qty": 1})
    assert r.status_code == 200, r.text

    ev = _accepted(audits)
    assert "route_wait_ms" in ev, "缺 route_wait_ms 就等于这段盲区还在"
    assert isinstance(ev["route_wait_ms"], (int, float))
    assert ev["route_wait_ms"] >= 0


def test_调度被拖慢时耗时记在route_wait_ms上(monkeypatch, audits):
    _wire(monkeypatch, route_delay_s=0.06)

    client.post("/api/execute-submit",
                json={"ticker": "IWM", "direction": "long", "qty": 1})

    assert _accepted(audits)["route_wait_ms"] >= 50


# ── A2. 平仓侧同一段盲区 ────────────────────────────────────────────────────
# /api/close 走的是同一条 `await asyncio.to_thread(...)`，调度排队的暴露面完全一样。
def _wire_close(monkeypatch, *, already_closing: bool = False, delay_s: float = 0.0):
    import app.exec_close as exec_close

    pos = SimpleNamespace(
        order_id="o-1", ticker="QQQ", direction="long",
        option_code="QQQ260716C00500000", qty=2,
        close_error="旧错误", close_timeout_policy=None,
        close_submitted_at=object(), cancel_requested=True,
    )
    registry = SimpleNamespace(
        get=lambda _c: pos,
        compare_and_set_status=lambda _c, _a, _b: not already_closing,
    )
    monkeypatch.setattr(exec_close, "REGISTRY", registry)
    monkeypatch.setattr(orders, "current_source",
                        lambda: SimpleNamespace(name="watcher"))
    monkeypatch.setattr(orders, "_schedule_bg", lambda *_a, **_k: None)

    real = exec_close.close_submit

    def _slow(source, req, route_started=None):
        if delay_s:
            time.sleep(delay_s)
        return real(source, req, route_started)

    monkeypatch.setattr(exec_close, "close_submit", _slow)
    return pos


def _close_requested(events):
    hits = [e for e in events if e["event"] == "trade_close_requested"]
    assert len(hits) == 1, f"应恰好一条 trade_close_requested，实得 {len(hits)}"
    return hits[0]


def test_平仓受理事件带出线程池调度耗时(monkeypatch, audits):
    _wire_close(monkeypatch, delay_s=0.06)

    r = client.post("/api/close", json={"option_code": "QQQ260716C00500000",
                                        "reason": "手动平仓 F"})
    assert r.status_code == 200, r.text

    ev = _close_requested(audits)
    assert ev.get("route_wait_ms", 0) >= 50


def test_平仓重复点击分支同样带调度耗时(monkeypatch, audits):
    _wire_close(monkeypatch, already_closing=True)

    r = client.post("/api/close", json={"option_code": "QQQ260716C00500000",
                                        "reason": "手动平仓 F"})
    assert r.status_code == 200, r.text

    ev = _close_requested(audits)
    assert ev["duplicate"] is True
    assert "route_wait_ms" in ev, "幂等分支漏埋点，重复点击时就看不到排队"


# ── B. 浏览器侧 RTT 回传 ────────────────────────────────────────────────────
def _make_intent(status="submitted"):
    intent = BUY_INTENTS.create("QQQ", "long")
    if status != "processing":
        BUY_INTENTS.resolve(intent.intent_id, status=status, result={"ok": True})
    return intent.intent_id


def test_终态轮询回传浏览器RTT并落一条日志(audits):
    iid = _make_intent()

    r = client.get(f"/api/execute-status/{iid}",
                   params={"client_submit_ms": 3.7, "poll_attempt": 2})
    assert r.status_code == 200

    hits = [e for e in audits if e["event"] == "client_entry_timing"]
    assert len(hits) == 1
    assert hits[0]["client_submit_ms"] == pytest.approx(3.7)
    assert hits[0]["poll_attempt"] == 2
    assert hits[0]["intent_id"] == iid


def test_同一受理号只落一条_重复轮询不刷屏(audits):
    iid = _make_intent()

    for _ in range(5):
        client.get(f"/api/execute-status/{iid}", params={"client_submit_ms": 3.7})

    assert len([e for e in audits if e["event"] == "client_entry_timing"]) == 1


def test_未终态不落日志(audits):
    iid = _make_intent(status="processing")

    client.get(f"/api/execute-status/{iid}", params={"client_submit_ms": 3.7})

    assert [e for e in audits if e["event"] == "client_entry_timing"] == []


def test_前端没带参数时照常工作不落日志(audits):
    iid = _make_intent()

    r = client.get(f"/api/execute-status/{iid}")

    assert r.status_code == 200 and r.json()["status"] == "submitted"
    assert [e for e in audits if e["event"] == "client_entry_timing"] == []


# ── 前端确实把参数发出去了 ──────────────────────────────────────────────────
def test_前端下单路径带上了计时参数():
    """浏览器侧必须用自己的钟自减；参数没发出去，后端那半边就是死代码。"""
    from pathlib import Path

    js = app_js()
    assert "client_submit_ms" in js, "app.js 未回传浏览器侧 RTT"
    assert "poll_attempt" in js, "app.js 未回传轮询拍数"
    body = js[js.index("async doOrder("):js.index("_pollExecuteIntent(intentId")]
    assert "performance.now()" in body, "RTT 必须用 performance.now 单钟自减"
