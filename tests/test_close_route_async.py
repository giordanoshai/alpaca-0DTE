"""POST /api/close：秒返回 accepted；spawn=True 时后台调度 close_run（不同步跑追单）。"""
from types import SimpleNamespace

from fastapi.testclient import TestClient

import app.main as m
import app.exec_close as order_exec
import app.routers.orders as orders

client = TestClient(m.app)


def test_close_returns_accepted_and_schedules_bg(monkeypatch):
    scheduled = {"n": 0, "fn": None}
    monkeypatch.setattr(orders, "current_source",
                        lambda: SimpleNamespace(name="watcher", alpaca=SimpleNamespace()))
    monkeypatch.setattr(order_exec, "close_submit",
                        lambda s, r, route_started=None: {"accepted": True, "status": "closing", "spawn": True})
    # close_run 不应被路由同步调用
    monkeypatch.setattr(order_exec, "close_run",
                        lambda *a, **k: scheduled.__setitem__("ran_sync", True))
    monkeypatch.setattr(orders, "_schedule_bg",
                        lambda fn, *a: (scheduled.__setitem__("n", scheduled["n"] + 1),
                                        scheduled.__setitem__("fn", fn)))
    r = client.post("/api/close", json={"option_code": "QQQ260716C00500000", "reason": "手动平仓"})
    assert r.status_code == 200
    assert r.json() == {"accepted": True, "status": "closing", "spawn": True}
    assert scheduled["n"] == 1
    assert scheduled["fn"] is order_exec.close_run
    assert "ran_sync" not in scheduled       # 路由不同步跑追单


def test_close_no_spawn_when_already_closing(monkeypatch):
    scheduled = {"n": 0}
    monkeypatch.setattr(orders, "current_source",
                        lambda: SimpleNamespace(name="watcher", alpaca=SimpleNamespace()))
    monkeypatch.setattr(order_exec, "close_submit",
                        lambda s, r, route_started=None: {"accepted": True, "status": "closing", "spawn": False})
    monkeypatch.setattr(orders, "_schedule_bg", lambda fn, *a: scheduled.__setitem__("n", scheduled["n"] + 1))
    r = client.post("/api/close", json={"option_code": "QQQ260716C00500000"})
    assert r.status_code == 200
    assert scheduled["n"] == 0                # 幂等：不再起第二个 worker
