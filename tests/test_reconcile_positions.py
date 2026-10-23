# -*- coding: utf-8 -*-
from datetime import datetime, timezone
from decimal import Decimal as D
from types import SimpleNamespace

import app.exec_reconcile as order_exec
from app.order_state import JOURNAL, ManagedPosition, REGISTRY


def test_reconcile_uses_owned_db_qty_and_does_not_absorb_same_contract_external_qty(monkeypatch):
    """券商同合约聚合 4 张、DB 本程序 2 张 → 只找回 2 张，额外 2 张不接管。"""
    code = "IWM260727P00294000"
    original_time = datetime(2026, 7, 27, 14, 4, tzinfo=timezone.utc)
    latest_add_time = datetime(2026, 7, 27, 14, 5, tzinfo=timezone.utc)
    broker_position = SimpleNamespace(
        symbol=code,
        qty="4",
        avg_entry_price="0.826",
    )
    latest_add = SimpleNamespace(
        id="add-order",
        symbol=code,
        submitted_at=latest_add_time,
        filled_at=latest_add_time,
        filled_avg_price="0.85",
    )

    class FakeTrading:
        def broker_positions(self):
            return [broker_position]

    monkeypatch.setattr(order_exec, "AlpacaTrading", FakeTrading)
    monkeypatch.setattr(
        order_exec.trade_recorder,
        "load_today",
        lambda: [{
            "order_id": "original-order",
            "option_code": code,
            "status": "open",
            "entry_time": original_time,
            "entry_mid": D("0.80"),
            "qty": 2,
            "realized_pnl": D("37.50"),
            "template_snapshot": {"stop_mode": "amount"},
        }],
    )
    monkeypatch.setattr(
        order_exec.trade_recorder,
        "load_order_events",
        lambda *_args: [],
    )
    persisted = {}
    monkeypatch.setattr(
        order_exec.trade_recorder,
        "record_position_sync",
        lambda oid, qty, entry_mid=None: persisted.update(
            order_id=oid, qty=qty, entry_mid=entry_mid,
        ),
    )

    result = order_exec.reconcile(SimpleNamespace(name="watcher"))

    pos = REGISTRY.get(code)
    assert pos is not None
    assert pos.order_id == "original-order"
    assert pos.entry_time == original_time
    assert pos.qty == 2
    assert pos.entry_mid == D("0.80")
    assert pos.realized_pnl == D("37.50")
    assert pos.entry_filled_qty == 2
    assert pos.entry_fill_mid == D("0.80")
    assert pos.stop_mode == "amount"
    assert persisted == {}
    assert result["recovered"] == [{
        "option_code": code,
        "qty": 2,
        "entry_mid": "0.80",
        "matched_order": True,
        "matched_trade": True,
        "broker_qty": 4,
        "ignored_external_qty": 2,
    }]
    assert result["ignored"] == []


def test_reconcile_ignores_position_without_program_open_trade(monkeypatch):
    code = "GOOGL260727C00330000"
    broker_position = SimpleNamespace(symbol=code, qty="8", avg_entry_price="1.30")

    class FakeTrading:
        def broker_positions(self):
            return [broker_position]

    monkeypatch.setattr(order_exec, "AlpacaTrading", FakeTrading)
    monkeypatch.setattr(order_exec.trade_recorder, "load_today", lambda: [])
    monkeypatch.setattr(
        order_exec.trade_recorder,
        "load_order_events",
        lambda *_args: [],
    )

    result = order_exec.reconcile(SimpleNamespace(name="watcher"))

    assert REGISTRY.get(code) is None
    assert result["recovered"] == []
    assert result["ignored"] == [{
        "option_code": code,
        "broker_qty": 8,
        "reason": "本程序数据库无 open 生命周期，按外部仓忽略",
    }]


def test_reconcile_recovers_owned_net_qty_from_events_when_trade_was_falsely_closed(
    monkeypatch,
):
    """买 2 + 加 1 - 系统卖 2 = 本系统仍有 1；即使汇总行误 closed 也应找回。"""
    code = "IWM260728P00291000"
    root_id = "open-order"
    entry_time = datetime(2026, 7, 28, 14, 21, 15, tzinfo=timezone.utc)
    broker_position = SimpleNamespace(
        symbol=code,
        qty="1",
        avg_entry_price="1.033333",
    )

    class FakeTrading:
        def broker_positions(self):
            return [broker_position]

    monkeypatch.setattr(order_exec, "AlpacaTrading", FakeTrading)
    monkeypatch.setattr(
        order_exec.trade_recorder,
        "load_today",
        lambda: [{
            "order_id": root_id,
            "option_code": code,
            "status": "closed",
            "entry_time": entry_time,
            "entry_mid": D("1.033333333333333333333333333"),
            "qty": 0,
            "realized_pnl": D("-18.66666666666666666666666660"),
            "template_snapshot": {"stop_mode": "percent"},
        }],
    )
    monkeypatch.setattr(
        order_exec.trade_recorder,
        "load_order_events",
        lambda *_args: [
            {
                "order_id": root_id,
                "parent_order_id": None,
                "client_order_id": "ttt-open",
                "ticker": "IWM",
                "option_code": code,
                "side": "buy",
                "qty": 2,
                "status": "filled",
                "origin": "user",
                "action": "open",
                "price": D("1.10"),
                "submitted_at": entry_time,
            },
            {
                "order_id": "add-order",
                "parent_order_id": root_id,
                "client_order_id": "ttt-add",
                "ticker": "IWM",
                "option_code": code,
                "side": "buy",
                "qty": 1,
                "status": "filled",
                "origin": "user",
                "action": "add",
                "price": D("0.90"),
                "submitted_at": entry_time,
            },
            {
                "order_id": "close-order",
                "parent_order_id": root_id,
                "client_order_id": "ttt-close",
                "ticker": "IWM",
                "option_code": code,
                "side": "sell",
                "qty": 2,
                "status": "filled",
                "origin": "system",
                "action": "auto_close",
                "price": D("0.94"),
                "submitted_at": entry_time,
            },
        ],
    )
    persisted = {}
    monkeypatch.setattr(
        order_exec.trade_recorder,
        "record_reconciled_position",
        lambda oid, qty, entry_mid: persisted.update(
            order_id=oid,
            qty=qty,
            entry_mid=entry_mid,
        ),
    )

    result = order_exec.reconcile(SimpleNamespace(name="watcher"))

    pos = REGISTRY.get(code)
    assert pos is not None
    assert pos.order_id == root_id
    assert pos.qty == 1
    assert pos.entry_mid == D("1.033333333333333333333333333")
    assert pos.realized_pnl == D("-18.66666666666666666666666660")
    assert pos.status == "open"
    assert persisted == {
        "order_id": root_id,
        "qty": 1,
        "entry_mid": D("1.033333333333333333333333333"),
    }
    assert result["recovered"] == [{
        "option_code": code,
        "qty": 1,
        "entry_mid": "1.033333333333333333333333333",
        "matched_order": True,
        "matched_trade": True,
        "broker_qty": 1,
        "ignored_external_qty": 0,
    }]
    assert result["ignored"] == []


def test_reconcile_does_not_trust_external_or_unprefixed_order_events(monkeypatch):
    """事件表里只有外部或非 ttt 开仓，仍不得据此接管账户持仓。"""
    code = "GOOGL260728C00330000"
    broker_position = SimpleNamespace(symbol=code, qty="1", avg_entry_price="1.30")

    class FakeTrading:
        def broker_positions(self):
            return [broker_position]

    monkeypatch.setattr(order_exec, "AlpacaTrading", FakeTrading)
    monkeypatch.setattr(order_exec.trade_recorder, "load_today", lambda: [])
    monkeypatch.setattr(
        order_exec.trade_recorder,
        "load_order_events",
        lambda *_args: [{
            "order_id": "external-order",
            "parent_order_id": None,
            "client_order_id": "another-system",
            "ticker": "GOOGL",
            "option_code": code,
            "side": "buy",
            "qty": 1,
            "status": "filled",
            "origin": "external",
            "action": "open",
            "price": D("1.30"),
            "submitted_at": datetime.now(timezone.utc),
        }],
    )

    result = order_exec.reconcile(SimpleNamespace(name="watcher"))

    assert REGISTRY.get(code) is None
    assert result["recovered"] == []
    assert result["ignored"][0]["option_code"] == code


def test_reconcile_ignores_closed_registry_entry_for_same_contract(monkeypatch):
    code = "SOFI260731C00016500"
    entry_time = datetime(2026, 7, 28, 15, 14, tzinfo=timezone.utc)
    REGISTRY.record(ManagedPosition(
        option_code=code,
        ticker="SOFI",
        direction="long",
        qty=0,
        entry_mid=D("0.78"),
        entry_time=entry_time,
        order_id="closed-lifecycle",
        status="closed",
    ))
    broker_position = SimpleNamespace(
        symbol=code,
        qty="3",
        avg_entry_price="0.77",
    )

    class FakeTrading:
        def broker_positions(self):
            return [broker_position]

    monkeypatch.setattr(order_exec, "AlpacaTrading", FakeTrading)
    monkeypatch.setattr(
        order_exec.trade_recorder,
        "load_order_events",
        lambda *_args: [],
    )
    monkeypatch.setattr(
        order_exec.trade_recorder,
        "load_today",
        lambda: [{
            "order_id": "owned-open-lifecycle",
            "option_code": code,
            "status": "open",
            "entry_time": entry_time,
            "entry_mid": D("0.76"),
            "qty": 3,
            "realized_pnl": D("0"),
            "template_snapshot": {"stop_mode": "percent"},
        }],
    )

    result = order_exec.reconcile(SimpleNamespace(name="watcher"))

    recovered = REGISTRY.get(code)
    assert recovered is not None
    assert recovered.status == "open"
    assert recovered.order_id == "owned-open-lifecycle"
    assert recovered.qty == 3
    assert result["recovered"][0]["option_code"] == code


def test_reconcile_reattaches_unique_owned_pending_sell(monkeypatch):
    """重启恢复持仓时，把同一生命周期唯一的 Alpaca 在途 ttt-* 卖单重新绑定。"""
    code = "INTC260731C00094000"
    root_id = "entry-intc"
    sell_id = "sell-intc"
    entry_time = datetime(2026, 7, 30, 14, 29, tzinfo=timezone.utc)
    broker_position = SimpleNamespace(
        symbol=code,
        qty="2",
        avg_entry_price="2.24",
    )

    class FakeTrading:
        def broker_positions(self):
            return [broker_position]

        def get_order(self, order_id):
            assert order_id == sell_id
            return {
                "id": sell_id,
                "status": "new",
                "symbol": code,
                "side": "sell",
                "qty": "1",
                "filled_qty": "0",
                "client_order_id": "ttt-sell-intc",
            }

    JOURNAL.submitted(
        sell_id,
        "INTC",
        code,
        "sell",
        1,
        reason="目标限价 +30%",
        price_type="limit",
        limit_price=D("2.91"),
        origin="user",
        action="reduce",
        client_order_id="ttt-sell-intc",
        parent_order_id=root_id,
    )
    monkeypatch.setattr(order_exec, "AlpacaTrading", FakeTrading)
    monkeypatch.setattr(
        order_exec.trade_recorder,
        "load_order_events",
        lambda *_args: [],
    )
    monkeypatch.setattr(
        order_exec.trade_recorder,
        "load_today",
        lambda: [{
            "order_id": root_id,
            "option_code": code,
            "status": "open",
            "entry_time": entry_time,
            "entry_mid": D("2.24"),
            "qty": 2,
            "realized_pnl": D("0"),
            "template_snapshot": {"stop_mode": "percent"},
        }],
    )
    monkeypatch.setattr(order_exec.trade_recorder, "audit", lambda *a, **k: None)

    order_exec.reconcile(SimpleNamespace(name="watcher"))

    recovered = REGISTRY.get(code)
    assert recovered is not None
    assert recovered.status == "closing"
    assert recovered.close_order_id == sell_id
    assert recovered.close_qty == 1
    assert recovered.close_applied_qty == 0
    assert recovered.close_reason == "目标限价 +30%"
    assert recovered.close_timeout_policy == "persistent"


# ── 未登记持仓的归属边界 ──────────────────────────────────────────────────────
# 口径变更（2026-08-15）：SOS 移出本项目后，这里不再向 SOS 账本查 swing 持仓并扣减。
# 原先的两条「排除 SOS 持仓」「只扣减 SOS 实际持有张数」用例已随之删除（备份在
# D:\SOS_BAK\tests\test_reconcile_positions.py）。现在的口径就一条：**REGISTRY 没
# 登记的，一律报出来**——包含券商侧可能残留的 SOS 时期 swing 仓。

def test_未登记扫描仍报出真正的外部仓(monkeypatch):
    """这个扫描存在的理由：人工/外部下的仓必须被看见。"""
    code = "GOOGL260727C00330000"

    class FakeTrading:
        def broker_positions(self):
            return [SimpleNamespace(symbol=code, qty="8", avg_entry_price="1.30")]

    monkeypatch.setattr(order_exec, "AlpacaTrading", FakeTrading)
    out = order_exec.list_unmanaged_positions(SimpleNamespace(name="watcher"))

    assert [r["option_code"] for r in out] == [code]
    assert out[0]["qty"] == 8


def test_未登记扫描只扣减REGISTRY已登记的张数(monkeypatch):
    """同合约券商 5 张、REGISTRY 认 3 张 → 剩下 2 张是来路不明的仓，必须报出来。"""
    code = "WMT260810C00126000"

    class FakeTrading:
        def broker_positions(self):
            return [SimpleNamespace(symbol=code, qty="5", avg_entry_price="1.00")]

    monkeypatch.setattr(order_exec, "AlpacaTrading", FakeTrading)
    REGISTRY.record(ManagedPosition(
        option_code=code, ticker="WMT", direction="long", qty=3,
        entry_mid=D("1.00"),
        entry_time=datetime(2026, 8, 10, 14, 0, tzinfo=timezone.utc),
        order_id="ttt-managed-3", status="open",
    ))
    try:
        out = order_exec.list_unmanaged_positions(SimpleNamespace(name="watcher"))
    finally:
        with REGISTRY._lock:
            REGISTRY._pos.pop(code, None)

    assert len(out) == 1
    assert out[0]["qty"] == 2
    assert out[0]["same_contract_managed_qty"] == 3


def test_SOS残留仓现在会被报为未登记(monkeypatch):
    """SOS 移出后的**已知代价**，用测试把它钉住而不是留给现场发现。

    券商账户里若还有 SOS 时期的 swing 仓，它不在 REGISTRY 里、也不再有 SOS 账本
    自报归属，于是必然出现在「未登记持仓」表上。这是只读展示、本身不平仓，但绝
    不能顺手点「找回」——拉进 REGISTRY 就等于交给 0DTE 风控按 0DTE 止损纪律处置。
    """
    code = "NVDA260821C00180000"

    class FakeTrading:
        def broker_positions(self):
            return [SimpleNamespace(symbol=code, qty="3", avg_entry_price="1.00")]

    monkeypatch.setattr(order_exec, "AlpacaTrading", FakeTrading)
    out = order_exec.list_unmanaged_positions(SimpleNamespace(name="watcher"))

    assert [r["option_code"] for r in out] == [code]
    assert out[0]["qty"] == 3
    assert "same_contract_sos_qty" not in out[0], "SOS 扣减字段应已随模块移除"
