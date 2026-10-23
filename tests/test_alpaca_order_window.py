"""Alpaca 订单基线：当前 ET 自然日终态 + 全部跨日在途单。"""
from datetime import datetime, timezone
from types import SimpleNamespace

from alpaca.trading.enums import QueryOrderStatus

from app import exec_reconcile as order_exec
from app.integrations.alpaca.trading import AlpacaTrading


def test_broker_order_session_queries_et_day_and_all_open():
    recent = SimpleNamespace(id="recent")
    old_open = SimpleNamespace(id="old-open")

    class FakeClient:
        def __init__(self):
            self.requests = []

        def get_orders(self, request):
            self.requests.append(request)
            return [recent] if request.status == QueryOrderStatus.ALL else [recent, old_open]

    broker = object.__new__(AlpacaTrading)
    broker._tc = FakeClient()

    cutoff = datetime(2026, 7, 25, tzinfo=timezone.utc)
    until = datetime(2026, 7, 25, 20, 0, tzinfo=timezone.utc)
    orders = broker.broker_order_session(after=cutoff, until=until)

    assert {str(order.id) for order in orders} == {"recent", "old-open"}
    assert [request.status for request in broker._tc.requests] == [
        QueryOrderStatus.ALL,
        QueryOrderStatus.OPEN,
    ]
    assert broker._tc.requests[0].after == cutoff
    assert broker._tc.requests[0].until == until


def test_broker_order_session_before_open_only_queries_open():
    old_open = SimpleNamespace(id="old-open")

    class FakeClient:
        def __init__(self):
            self.requests = []

        def get_orders(self, request):
            self.requests.append(request)
            return [old_open]

    broker = object.__new__(AlpacaTrading)
    broker._tc = FakeClient()

    orders = broker.broker_order_session()

    assert [str(order.id) for order in orders] == ["old-open"]
    assert [request.status for request in broker._tc.requests] == [
        QueryOrderStatus.OPEN,
    ]


def test_fetch_broker_order_session_uses_current_et_calendar_day(monkeypatch):
    calls = []

    class FakeTrading:
        def broker_order_session(self, *, after, until):
            calls.append((after, until))
            return []

    monkeypatch.setattr(order_exec, "AlpacaTrading", FakeTrading)

    order_exec.fetch_broker_order_session(
        datetime(2026, 7, 31, 18, 0, tzinfo=timezone.utc)
    )
    order_exec.fetch_broker_order_session(
        datetime(2026, 7, 31, 21, 0, tzinfo=timezone.utc)
    )
    order_exec.fetch_broker_order_session(
        datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)
    )

    assert calls == [
        (
            datetime(2026, 7, 31, 4, 0, tzinfo=timezone.utc),
            datetime(2026, 7, 31, 18, 0, tzinfo=timezone.utc),
        ),
        (
            datetime(2026, 7, 31, 4, 0, tzinfo=timezone.utc),
            datetime(2026, 7, 31, 21, 0, tzinfo=timezone.utc),
        ),
        (
            datetime(2026, 7, 31, 4, 0, tzinfo=timezone.utc),
            datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc),
        ),
    ]
