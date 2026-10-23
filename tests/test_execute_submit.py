"""execute_submit：快返回受理号；同标的+方向在途时幂等，不重复起 worker（防重复真实买单）。"""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import app.exec_entry as order_exec
from app.order_state import BUY_INTENTS


def _req(ticker="QQQ", direction="long"):
    return SimpleNamespace(ticker=ticker, direction=direction, qty=1, price_type=None, limit_price=None)


def _src(name="watcher"):
    return SimpleNamespace(name=name, alpaca=SimpleNamespace())


def _clear():
    order_exec._BUY_INFLIGHT.clear()
    BUY_INTENTS.clear()


def test_submit_creates_intent_and_spawns(monkeypatch):
    _clear()
    audits = []
    monkeypatch.setattr(
        order_exec.trade_recorder, "audit",
        lambda event, **fields: audits.append((event, fields)),
    )
    out = order_exec.execute_submit(_src(), _req())
    assert out["accepted"] is True
    assert out["status"] == "processing"
    assert out["spawn"] is True
    it = BUY_INTENTS.get(out["intent_id"])
    assert it is not None and it.ticker == "QQQ" and it.direction == "long"
    assert audits == [(
        "trade_intent_accepted",
        {
            "intent_id": out["intent_id"],
            "ticker": "QQQ",
            "direction": "long",
            "source": "watcher",
            "status": "processing",
            "duplicate": False,
        },
    )]
    _clear()


def test_submit_does_not_call_execute():
    """execute_submit 本身不触达 execute()（真正 gate/选约/提交留给后台 execute_run）。"""
    _clear()
    called = {"n": 0}
    import app.order_exec as oe
    orig = oe.execute
    oe.execute = lambda *a, **k: called.__setitem__("n", called["n"] + 1)
    try:
        order_exec.execute_submit(_src(), _req())
    finally:
        oe.execute = orig
    assert called["n"] == 0
    _clear()


def test_submit_idempotent_when_already_inflight():
    _clear()
    first = order_exec.execute_submit(_src(), _req())
    second = order_exec.execute_submit(_src(), _req())
    assert second == {"accepted": True, "intent_id": first["intent_id"], "status": "processing", "spawn": False}
    _clear()


def test_submit_different_ticker_not_deduped():
    _clear()
    a = order_exec.execute_submit(_src(), _req(ticker="QQQ"))
    b = order_exec.execute_submit(_src(), _req(ticker="SPY"))
    assert a["intent_id"] != b["intent_id"]
    assert a["spawn"] is True and b["spawn"] is True
    _clear()


def test_submit_rejects_non_exec_source():
    _clear()
    with pytest.raises(HTTPException):
        order_exec.execute_submit(_src(name="real"), _req())
    _clear()


def test_submit_concurrent_same_key_only_one_spawns():
    """并发同标的+方向：check-and-set 原子（加锁），只放行一个 execute_run 真正提交。"""
    import threading

    _clear()
    results = []

    def call():
        results.append(order_exec.execute_submit(_src(), _req()))

    threads = [threading.Thread(target=call) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sum(1 for r in results if r["spawn"]) == 1
    assert all(r["accepted"] for r in results)
    assert len({r["intent_id"] for r in results}) == 1   # 全部拿到同一个 intent_id
    _clear()
