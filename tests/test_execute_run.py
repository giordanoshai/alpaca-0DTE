"""execute_run：跑 execute()（原样）→ 按结果回填 BuyIntent；成功/拒绝/异常三态；跑完释放在途锁。"""
from types import SimpleNamespace

import app.exec_entry as order_exec
from app.order_state import BUY_INTENTS


def _req(ticker="QQQ", direction="long"):
    return SimpleNamespace(ticker=ticker, direction=direction, qty=1, price_type=None, limit_price=None)


def _src():
    return SimpleNamespace(name="watcher", alpaca=SimpleNamespace())


def _clear():
    order_exec._BUY_INFLIGHT.clear()
    BUY_INTENTS.clear()


def _submit_and_get_id():
    out = order_exec.execute_submit(_src(), _req())
    return out["intent_id"]


def test_run_success_resolves_submitted(monkeypatch):
    _clear()
    iid = _submit_and_get_id()
    fake_result = {"executed": True, "gate": {"allowed": True, "qty": 1}, "contract": "QQQ260716C00500000",
                   "limit": "1.05", "order": {"id": "o1"}, "delta_source": "alpaca"}
    monkeypatch.setattr(order_exec, "execute", lambda *a, **k: fake_result)
    order_exec.execute_run(_src(), _req(), iid)
    it = BUY_INTENTS.get(iid)
    assert it.status == "submitted"
    assert it.result == fake_result
    assert ("QQQ", "long") not in order_exec._BUY_INFLIGHT   # 跑完释放在途锁
    _clear()


def test_run_gate_rejected_resolves_rejected(monkeypatch):
    _clear()
    iid = _submit_and_get_id()
    fake_result = {"executed": False, "gate": {"allowed": False, "reason": "开盘冷却中", "rule": "blackout", "qty": 0}}
    monkeypatch.setattr(order_exec, "execute", lambda *a, **k: fake_result)
    order_exec.execute_run(_src(), _req(), iid)
    it = BUY_INTENTS.get(iid)
    assert it.status == "rejected"
    assert it.reason == "开盘冷却中"
    assert it.result == fake_result
    _clear()


def test_run_exception_resolves_error(monkeypatch):
    _clear()
    iid = _submit_and_get_id()
    def boom(*a, **k):
        raise RuntimeError("券商挂了")
    monkeypatch.setattr(order_exec, "execute", boom)
    order_exec.execute_run(_src(), _req(), iid)
    it = BUY_INTENTS.get(iid)
    assert it.status == "error"
    assert "券商挂了" in it.reason
    assert ("QQQ", "long") not in order_exec._BUY_INFLIGHT   # 异常也要释放在途锁
    _clear()


def test_run_releases_inflight_lock_allows_resubmit(monkeypatch):
    """跑完（无论成功/拒绝）后，同标的+方向应能重新受理（不永久卡死在途态）。"""
    _clear()
    iid = _submit_and_get_id()
    monkeypatch.setattr(order_exec, "execute", lambda *a, **k: {"executed": False, "gate": {"reason": "x"}})
    order_exec.execute_run(_src(), _req(), iid)
    again = order_exec.execute_submit(_src(), _req())
    assert again["spawn"] is True
    assert again["intent_id"] != iid
    _clear()
