"""/api/execute 响应携带 delta_source（uw/approx）。"""
from decimal import Decimal
from types import SimpleNamespace

from fastapi.testclient import TestClient

import app.main as m
import app.exec_entry as order_exec
import app.routers.orders as orders
from app.order_gate import MarketContext
from app.integrations.alpaca.data import SelectedContract
from app.services.contract_selection import contract_for_validation
from app.validator import ValidationResult

client = TestClient(m.app)


class _FakeExec:
    def position_qty(self, _symbol):
        """券商侧无仓。开仓前要核对持仓归属（防与 SOS/人工持同一合约）。"""
        return 0

    def submit(self, req):
        return {"id": "fake-order-delta-source", "status": "accepted"}


def test_execute_response_carries_delta_source(monkeypatch):
    sel = SelectedContract("QQQ260716C00500000", Decimal("1.00"), Decimal("1.02"),
                           delta=Decimal("0.47"), delta_source="uw")
    # execute 复用 ctx.selected（闸门选中的完整合约），delta_source 随之透出。
    ctx = MarketContext(
        signal=None,
        account=None,
        contract=contract_for_validation(sel),
        selected=sel,
    )
    src = SimpleNamespace(name="watcher", market_context=lambda t, d: ctx)
    monkeypatch.setattr(orders, "current_source", lambda: src)
    monkeypatch.setattr(order_exec, "evaluate_with_context",
                        lambda *a, **k: (ValidationResult(allowed=True, reason="", rule="", qty=1), ctx))
    monkeypatch.setattr(order_exec, "AlpacaTrading", _FakeExec)
    r = client.post("/api/execute", json={"ticker": "QQQ", "direction": "long", "qty": 1})
    assert r.status_code == 200
    body = r.json()
    assert body["executed"] is True
    assert body["delta_source"] == "uw"
