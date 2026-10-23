# -*- coding: utf-8 -*-
"""reconcile_pending_journal：启动时核对 JOURNAL 里 pending 买单在 Alpaca 侧是否仍真实在途。

修「07-23 幽灵订单」：测试污染（见 tests/test_api_delta_source.py 等曾写真库）或进程重启丢失
REGISTRY 后，DB 里滞留的假/孤儿 pending 订单会被 rebuild_from_trades 永久重建成「待成交」，
撤单必 404（cancel() 靠 REGISTRY.by_order_id 找不到），且 Alpaca 侧根本没有这笔单。
启动时主动核对一遍：券商查无此单/单已终态 → 本地一并了结，不再永久卡住撤不掉。
"""
from decimal import Decimal as D

import app.exec_reconcile as order_exec
from app.order_state import OrderJournal


class _FakeEC:
    def __init__(self, orders):
        self._orders = orders  # order_id -> status str | Exception

    def get_order(self, order_id):
        v = self._orders.get(order_id)
        if isinstance(v, Exception):
            raise v
        return {"status": v}


def _seed(order_id="e1"):
    j = OrderJournal()
    j.submitted(order_id, "QQQ", "QQQ260716C00500000", "buy", 1, D("1.00"))
    return j


def test_unknown_order_id_gets_cancelled(monkeypatch):
    """券商查无此单（如测试污染写入的假 order_id）→ 本地也置已取消，不再永久待成交。"""
    j = _seed("fake-order-reuse")
    monkeypatch.setattr(order_exec, "JOURNAL", j)
    monkeypatch.setattr(order_exec, "AlpacaTrading",
                        lambda: _FakeEC({"fake-order-reuse": RuntimeError("404 not found")}))
    calls = []
    monkeypatch.setattr(order_exec.trade_recorder, "record_cancelled", lambda oid: calls.append(oid))
    result = order_exec.reconcile_pending_journal()
    assert result == {"checked": 1, "resolved": 1}
    assert j._events["fake-order-reuse"].status == "cancelled"
    assert calls == ["fake-order-reuse"]


def test_network_error_keeps_pending(monkeypatch):
    """网络类异常（超时/断网/代理故障）≠ 券商查无此单：必须保留 pending，
    否则启动时一次网络抖动就会把真实在途买单本地误标已撤，随后券商成交、系统失去跟踪。"""
    j = _seed("real-net")
    monkeypatch.setattr(order_exec, "JOURNAL", j)
    monkeypatch.setattr(order_exec, "AlpacaTrading",
                        lambda: _FakeEC({"real-net": ConnectionError("proxy timeout")}))
    calls = []
    monkeypatch.setattr(order_exec.trade_recorder, "record_cancelled", lambda oid: calls.append(oid))
    result = order_exec.reconcile_pending_journal()
    assert result == {"checked": 1, "resolved": 0}
    assert j._events["real-net"].status == "pending"
    assert calls == []


def test_api_404_status_code_gets_cancelled(monkeypatch):
    """带 status_code=404 的 SDK 异常（Alpaca APIError 形态）→ 确认孤儿单，照常了结。"""
    class _ApiErr(Exception):
        status_code = 404

    j = _seed("real-404")
    monkeypatch.setattr(order_exec, "JOURNAL", j)
    monkeypatch.setattr(order_exec, "AlpacaTrading",
                        lambda: _FakeEC({"real-404": _ApiErr("order not found")}))
    monkeypatch.setattr(order_exec.trade_recorder, "record_cancelled", lambda oid: None)
    result = order_exec.reconcile_pending_journal()
    assert result == {"checked": 1, "resolved": 1}
    assert j._events["real-404"].status == "cancelled"


def test_broker_filled_marks_filled(monkeypatch):
    j = _seed("real-1")
    monkeypatch.setattr(order_exec, "JOURNAL", j)
    monkeypatch.setattr(order_exec, "AlpacaTrading", lambda: _FakeEC({"real-1": "filled"}))
    monkeypatch.setattr(order_exec.trade_recorder, "record_filled", lambda oid: None)
    result = order_exec.reconcile_pending_journal()
    assert result == {"checked": 1, "resolved": 1}
    assert j._events["real-1"].status == "filled"


def test_broker_cancelled_marks_cancelled(monkeypatch):
    j = _seed("real-2")
    monkeypatch.setattr(order_exec, "JOURNAL", j)
    monkeypatch.setattr(order_exec, "AlpacaTrading", lambda: _FakeEC({"real-2": "canceled"}))
    monkeypatch.setattr(order_exec.trade_recorder, "record_cancelled", lambda oid: None)
    result = order_exec.reconcile_pending_journal()
    assert j._events["real-2"].status == "cancelled"


def test_broker_still_open_stays_pending(monkeypatch):
    """券商侧确实仍在排队（new/accepted 等）→ 保留 pending，不误撤。"""
    j = _seed("real-3")
    monkeypatch.setattr(order_exec, "JOURNAL", j)
    monkeypatch.setattr(order_exec, "AlpacaTrading", lambda: _FakeEC({"real-3": "new"}))
    result = order_exec.reconcile_pending_journal()
    assert result == {"checked": 1, "resolved": 0}
    assert j._events["real-3"].status == "pending"


def test_no_pending_events_short_circuits(monkeypatch):
    """无 pending 单不必构造 AlpacaTrading（省一次券商鉴权往返）。"""
    j = OrderJournal()
    monkeypatch.setattr(order_exec, "JOURNAL", j)
    built = []
    monkeypatch.setattr(order_exec, "AlpacaTrading", lambda: built.append(1) or _FakeEC({}))
    result = order_exec.reconcile_pending_journal()
    assert result == {"checked": 0, "resolved": 0}
    assert built == []
