"""close_submit：快返回受理态、设 closing、幂等；不触达 atomic_close。"""
from datetime import datetime, timezone
from decimal import Decimal as D
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import app.exec_close as order_exec
from app.order_state import REGISTRY, ManagedPosition


def _req(code="QQQ260716C00500000", qty=None):
    return SimpleNamespace(option_code=code, reason="手动平仓", qty=qty,
                           price_type=None, limit_price=None)


def _src():
    return SimpleNamespace(name="watcher", alpaca=SimpleNamespace())


def _seed(status="open", err="prev"):
    REGISTRY._pos.clear()
    p = ManagedPosition(option_code="QQQ260716C00500000", ticker="QQQ", direction="long",
                        qty=2, entry_mid=D("1.00"), entry_time=datetime.now(timezone.utc),
                        order_id="e1", status=status)
    p.close_error = err
    REGISTRY.record(p)
    return p


def test_submit_sets_closing_and_spawns(monkeypatch):
    called = {"n": 0}
    audits = []
    monkeypatch.setattr(order_exec, "atomic_close", lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    monkeypatch.setattr(
        order_exec.trade_recorder, "audit",
        lambda event, **fields: audits.append((event, fields)),
    )
    p = _seed(status="open", err="prev")
    out = order_exec.close_submit(_src(), _req())
    assert out == {"accepted": True, "status": "closing", "spawn": True}
    assert p.status == "closing"
    assert p.close_error is None            # 起新平仓清空旧错误
    assert called["n"] == 0                  # 不触达 atomic_close
    assert audits == [(
        "trade_close_requested",
        {
            "order_id": "e1",
            "ticker": "QQQ",
            "direction": "long",
            "contract": "QQQ260716C00500000",
            "qty": 2,
            "reason": "手动平仓",
            "price_type": "bid",
            "source": "watcher",
            "duplicate": False,
        },
    )]
    REGISTRY._pos.clear()


def test_submit_idempotent_when_already_closing():
    _seed(status="closing", err=None)
    out = order_exec.close_submit(_src(), _req())
    assert out == {"accepted": True, "status": "closing", "spawn": False}
    REGISTRY._pos.clear()


def test_submit_rejects_non_exec_source():
    _seed()
    with pytest.raises(HTTPException):
        order_exec.close_submit(SimpleNamespace(name="real", alpaca=None), _req())
    REGISTRY._pos.clear()


def test_submit_rejects_unknown_position():
    REGISTRY._pos.clear()
    with pytest.raises(HTTPException):
        order_exec.close_submit(_src(), _req(code="NOPE"))


def test_submit_concurrent_only_one_spawns(monkeypatch):
    """并发同标的平仓：check-and-set 原子（加锁），只放行一个 close_run（防券商侧双卖）。

    放大「查 status→设 closing」窗口：set_status 内 sleep。无锁时多线程都读到 open → 都 spawn；
    有锁时仅首个 spawn，其余幂等 spawn=False。
    """
    import threading
    import time as _t

    _seed(status="open", err=None)
    orig_set = order_exec.REGISTRY.set_status

    def slow_set_status(code, status):
        _t.sleep(0.02)
        orig_set(code, status)

    monkeypatch.setattr(order_exec.REGISTRY, "set_status", slow_set_status)
    results = []

    def call():
        results.append(order_exec.close_submit(_src(), _req()))

    threads = [threading.Thread(target=call) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sum(1 for r in results if r["spawn"]) == 1     # 仅一个起 worker
    assert all(r["accepted"] for r in results)
    REGISTRY._pos.clear()
