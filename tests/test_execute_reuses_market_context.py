"""execute() 复用 market_context 选中的合约，不再二次调用 entry_plan（消重复取数 + gate/execute 同约一致）。"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal
import threading
import time
from types import SimpleNamespace

from fastapi.testclient import TestClient

import app.main as m
import app.exec_entry as order_exec
import app.routers.orders as orders
from app.order_gate import MarketContext
from app.order_state import ManagedPosition
from app.integrations.alpaca.data import SelectedContract
from app.services.contract_selection import contract_for_validation
from app.validator import ValidationResult

client = TestClient(m.app)


class _FakeExec:
    def position_qty(self, _symbol):
        """券商侧无仓。开仓前要核对持仓归属（防与 SOS/人工持同一合约）。"""
        return 0

    def submit(self, req):
        return {"id": "fake-order-reuse", "status": "accepted"}


def _entry_plan_boom(ticker, direction):
    raise AssertionError("execute 不应再调用 entry_plan（应复用 market_context 选中的合约）")


def test_execute_reuses_selected_from_market_context(monkeypatch):
    sel = SelectedContract("QQQ260716C00500000", Decimal("1.00"), Decimal("1.02"),
                           delta=Decimal("0.47"), delta_source="uw")
    # signal/account 在本测试不参与断言（evaluate_with_context 已被打桩），只需容器成立。
    ctx = MarketContext(
        signal=None,
        account=None,
        contract=contract_for_validation(sel),
        selected=sel,
    )
    src = SimpleNamespace(name="watcher",
                          market_context=lambda t, d: ctx,
                          entry_plan=_entry_plan_boom)
    monkeypatch.setattr(orders, "current_source", lambda: src)
    monkeypatch.setattr(order_exec, "evaluate_with_context",
                        lambda *a, **k: (ValidationResult(allowed=True, reason="", rule="", qty=1), ctx))
    monkeypatch.setattr(order_exec, "AlpacaTrading", _FakeExec)
    r = client.post(
        "/api/execute",
        json={"ticker": "QQQ", "direction": "long", "qty": 1, "stop_mode": "amount"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["executed"] is True
    assert body["contract"] == "QQQ260716C00500000"
    assert body["delta_source"] == "uw"
    assert order_exec.REGISTRY.get("QQQ260716C00500000").stop_mode == "amount"


def test_execute_rejects_active_same_contract_before_broker_submit(monkeypatch):
    code = "QQQ260716C00500000"
    sel = SelectedContract(
        code,
        Decimal("1.00"),
        Decimal("1.02"),
        delta=Decimal("0.47"),
        delta_source="uw",
    )
    ctx = MarketContext(
        signal=None,
        account=None,
        contract=contract_for_validation(sel),
        selected=sel,
    )
    source = SimpleNamespace(
        name="watcher",
        market_context=lambda *_: ctx,
    )
    original = ManagedPosition(
        option_code=code,
        ticker="QQQ",
        direction="long",
        qty=3,
        entry_mid=Decimal("1.01"),
        entry_time=datetime.now(timezone.utc),
        order_id="existing-order",
        status="open",
    )
    order_exec.REGISTRY.record(original)
    monkeypatch.setattr(
        order_exec,
        "evaluate_with_context",
        lambda *a, **k: (
            ValidationResult(allowed=True, reason="", rule="", qty=3),
            ctx,
        ),
    )

    class NoBrokerSubmit:
        def submit(self, _req):
            raise AssertionError("duplicate contract must be rejected before broker submit")

    monkeypatch.setattr(order_exec, "AlpacaTrading", NoBrokerSubmit)
    result = order_exec.execute(
        source,
        SimpleNamespace(
            ticker="QQQ",
            direction="long",
            qty=3,
            price_type="ask",
            limit_price=None,
            stop_mode="percent",
        ),
    )

    assert result["executed"] is False
    assert result["duplicate_contract"] is True
    assert result["contract"] == code
    assert order_exec.REGISTRY.get(code) is original


def test_concurrent_execute_submits_same_contract_only_once(monkeypatch):
    code = "QQQ260716C00500000"
    sel = SelectedContract(
        code,
        Decimal("1.00"),
        Decimal("1.02"),
        delta=Decimal("0.47"),
        delta_source="uw",
    )
    ctx = MarketContext(
        signal=None,
        account=None,
        contract=contract_for_validation(sel),
        selected=sel,
    )
    source = SimpleNamespace(
        name="watcher",
        market_context=lambda *_: ctx,
    )
    req = SimpleNamespace(
        ticker="QQQ",
        direction="long",
        qty=3,
        price_type="ask",
        limit_price=None,
        stop_mode="percent",
    )
    monkeypatch.setattr(
        order_exec,
        "evaluate_with_context",
        lambda *a, **k: (
            ValidationResult(allowed=True, reason="", rule="", qty=3),
            ctx,
        ),
    )

    class SlowBrokerSubmit:
        count = 0
        count_lock = threading.Lock()

        def position_qty(self, _symbol):
            return 0

        def submit(self, _req):
            with self.count_lock:
                type(self).count += 1
            time.sleep(0.05)
            return {"id": "only-order", "status": "accepted"}

    monkeypatch.setattr(order_exec, "AlpacaTrading", SlowBrokerSubmit)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: order_exec.execute(source, req), range(2)))

    assert SlowBrokerSubmit.count == 1
    assert sum(result["executed"] is True for result in results) == 1
    assert sum(result.get("duplicate_contract") is True for result in results) == 1
    assert order_exec.REGISTRY.get(code).order_id == "only-order"


# ── 与 SOS 的持仓归属边界 ────────────────────────────────────────────────────

def _ctx_for(code):
    sel = SelectedContract(code, Decimal("1.00"), Decimal("1.02"),
                           delta=Decimal("0.47"), delta_source="uw")
    return sel, MarketContext(signal=None, account=None,
                              contract=contract_for_validation(sel), selected=sel)


def _exec_req(code, monkeypatch):
    _sel, ctx = _ctx_for(code)
    source = SimpleNamespace(name="watcher", market_context=lambda *_: ctx,
)
    monkeypatch.setattr(
        order_exec, "evaluate_with_context",
        lambda *a, **k: (ValidationResult(allowed=True, reason="", rule="", qty=3), ctx))
    req = SimpleNamespace(ticker="QQQ", direction="long", qty=3,
                          price_type="ask", limit_price=None, stop_mode="percent")
    return source, req


def test_execute_rejects_contract_held_by_other_system(monkeypatch):
    """券商已持有该合约、REGISTRY 却没登记 —— 那是 SOS（swing）或人工的仓。

    持仓在券商侧按 symbol 合并成**一条**，没有 client_order_id 可分（只有订单才带
    `ttt-` / `sos-` 前缀）。0DTE 买进去之后，两套系统各按各的账本算平仓数量，先动手
    的那套会把对方的张数一并卖掉，而两边风控谁都不会报错。
    触发场景不玄乎：SOS 的 swing 合约熬到到期日当天，就成了 0DTE 的可选标的。
    """
    code = "QQQ260716C00470000"
    source, req = _exec_req(code, monkeypatch)

    class ForeignHeld:
        def position_qty(self, _symbol):
            return 4

        def submit(self, _req):
            raise AssertionError("撞上别人的仓必须在下单前拒掉")

    monkeypatch.setattr(order_exec, "AlpacaTrading", ForeignHeld)
    result = order_exec.execute(source, req)

    assert result["executed"] is False
    assert result["foreign_position"] is True
    assert "非本系统" in result["error"]
    assert order_exec.REGISTRY.get(code) is None


def test_execute_rejects_when_broker_position_check_fails(monkeypatch):
    """核不清归属就不开仓 —— 与 SOS 侧、止盈防超卖同口径。"""
    code = "QQQ260716C00480000"
    source, req = _exec_req(code, monkeypatch)

    class BrokerDown:
        def position_qty(self, _symbol):
            raise RuntimeError("券商查询失败")

        def submit(self, _req):
            raise AssertionError("核不清归属不许下单")

    monkeypatch.setattr(order_exec, "AlpacaTrading", BrokerDown)
    result = order_exec.execute(source, req)

    assert result["executed"] is False
    assert "核对" in result["error"]
