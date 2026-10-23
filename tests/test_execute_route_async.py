"""POST /api/execute-submit：秒返回 accepted；spawn=True 时后台调度 execute_run（不同步跑gate/提交）。
GET /api/execute-status/{intent_id}：回显受理号当前状态；未知号 404。
"""
from types import SimpleNamespace

from fastapi.testclient import TestClient

import app.main as m
import app.exec_entry as order_exec
import app.routers.orders as orders
from app.order_state import BUY_INTENTS

client = TestClient(m.app)


def test_execute_submit_returns_accepted_and_schedules_bg(monkeypatch):
    scheduled = {"n": 0, "fn": None}
    monkeypatch.setattr(orders, "current_source",
                        lambda: SimpleNamespace(name="watcher", alpaca=SimpleNamespace()))
    monkeypatch.setattr(order_exec, "execute_submit",
                        lambda s, r, route_started=None: {"accepted": True, "intent_id": "BIabc", "status": "processing", "spawn": True})
    monkeypatch.setattr(order_exec, "execute_run",
                        lambda *a, **k: scheduled.__setitem__("ran_sync", True))
    monkeypatch.setattr(orders, "_schedule_bg",
                        lambda fn, *a: (scheduled.__setitem__("n", scheduled["n"] + 1),
                                        scheduled.__setitem__("fn", fn)))
    r = client.post("/api/execute-submit", json={"ticker": "QQQ", "direction": "long"})
    assert r.status_code == 200
    assert r.json() == {"accepted": True, "intent_id": "BIabc", "status": "processing", "spawn": True}
    assert scheduled["n"] == 1
    assert scheduled["fn"] is order_exec.execute_run
    assert "ran_sync" not in scheduled       # 路由不同步跑 gate/提交


def test_execute_submit_no_spawn_when_already_inflight(monkeypatch):
    scheduled = {"n": 0}
    monkeypatch.setattr(orders, "current_source",
                        lambda: SimpleNamespace(name="watcher", alpaca=SimpleNamespace()))
    monkeypatch.setattr(order_exec, "execute_submit",
                        lambda s, r, route_started=None: {"accepted": True, "intent_id": "BIabc", "status": "processing", "spawn": False})
    monkeypatch.setattr(orders, "_schedule_bg", lambda fn, *a: scheduled.__setitem__("n", scheduled["n"] + 1))
    r = client.post("/api/execute-submit", json={"ticker": "QQQ", "direction": "long"})
    assert r.status_code == 200
    assert scheduled["n"] == 0


def test_execute_status_returns_intent(monkeypatch):
    BUY_INTENTS.clear()
    intent = BUY_INTENTS.create("QQQ", "long")
    BUY_INTENTS.resolve(intent.intent_id, status="submitted", result={"executed": True})
    r = client.get(f"/api/execute-status/{intent.intent_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "submitted"
    assert body["result"] == {"executed": True}
    BUY_INTENTS.clear()


def test_execute_status_unknown_intent_404():
    r = client.get("/api/execute-status/BInope")
    assert r.status_code == 404
