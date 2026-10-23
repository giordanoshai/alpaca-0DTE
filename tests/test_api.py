"""/api/order 下单闸门端点测试。

README 铁律：后端只接受 (ticker, direction)，不接受数量/价格类型/strike。
"""
from __future__ import annotations

import pytest
from datetime import datetime, timezone
from decimal import Decimal as D
from types import SimpleNamespace
from fastapi.testclient import TestClient

from app.main import app
from app.order_gate import MarketContext
import app.trading_state as trading_state
from app.validator import AccountState, Contract, TickerSignal

client = TestClient(app)


@pytest.fixture(autouse=True)
def _mock_watcher_market_context(monkeypatch):
    # API 行为测试默认处于盘前；盘中 423 由专门用例覆盖，避免测试依赖实际运行时间。
    monkeypatch.setattr("app.templates.is_market_hours", lambda now=None: False)

    def _fake_ctx(_source, ticker: str, direction: str) -> MarketContext:
        return MarketContext(
            signal=TickerSignal(price=D("100"), vwap=D("99.9"), atr=D("1.0")),
            account=AccountState(daily_pnl=D("0"), weekly_pnl=D("0"), consecutive_losses=0,
                                 cooldown_remaining_s=0, minutes_since_open=D("60"), buying_power=D("1000")),
            contract=Contract(delta=D("0.45"), spread_pct=D("0.02"), mid=D("2.00")),
        )
    monkeypatch.setattr("app.order_gate.build_market_context", _fake_ctx)
    monkeypatch.setattr("app.exec_entry.build_market_context", _fake_ctx)


def test_order_valid_long_allowed():
    r = client.post("/api/order", json={"ticker": "QQQ", "direction": "long"})
    assert r.status_code == 200
    body = r.json()
    assert body["allowed"] is True
    assert body["qty"] >= 1
    assert set(body) >= {"allowed", "reason", "rule", "qty"}


def test_order_off_whitelist_rejected(monkeypatch):
    # 白名单已动态化（user_watchlist 直读，TSLA 实际在库内）→ 此处固定白名单测规则本身
    from app import order_gate
    monkeypatch.setattr(order_gate, "_get_dynamic_whitelist", lambda: ("QQQ", "IWM", "SPY"))
    r = client.post("/api/order", json={"ticker": "TSLA", "direction": "long"})
    assert r.status_code == 200
    body = r.json()
    assert body["allowed"] is False
    assert body["rule"] == "whitelist"
    assert body["reason"]


def test_order_accepts_qty_field():
    # ⑥（2026-07-16 起）：铁律松动，接受用户输入的 qty（由 validator 守上限）
    r = client.post("/api/order", json={"ticker": "QQQ", "direction": "long", "qty": 2})
    assert r.status_code == 200
    body = r.json()
    assert set(body) >= {"allowed", "reason", "rule", "qty"}


def test_order_accepts_stop_mode_and_defaults_to_percent():
    from app.routers.orders import OrderRequest

    assert OrderRequest(ticker="QQQ", direction="long").stop_mode == "percent"
    assert OrderRequest(
        ticker="QQQ", direction="long", stop_mode="amount").stop_mode == "amount"


def test_order_rejects_invalid_stop_mode():
    r = client.post(
        "/api/order",
        json={"ticker": "QQQ", "direction": "long", "stop_mode": "both"},
    )
    assert r.status_code == 422


def test_order_rejects_non_positive_qty():
    # qty ≤ 0 非法 → 422（Field ge=1）
    r = client.post("/api/order", json={"ticker": "QQQ", "direction": "long", "qty": 0})
    assert r.status_code == 422


def test_order_rejects_unknown_extra_field():
    # 仍拒绝未知越权字段（如价格/strike 走 execute，不进下单闸门）
    r = client.post("/api/order", json={"ticker": "QQQ", "direction": "long", "strike": 470})
    assert r.status_code == 422


def test_order_bad_direction_is_422():
    r = client.post("/api/order", json={"ticker": "QQQ", "direction": "sideways"})
    assert r.status_code == 422


def test_risk_endpoint_shape(monkeypatch):
    now = datetime(2026, 7, 27, 14, 0, tzinfo=timezone.utc)
    day = SimpleNamespace(
        program_gross_pnl=D("602.00"),
        external_gross_pnl=D("929.00"),
        account_net_pnl=D("1527.16"),
        account_adjustment=D("-3.84"),
    )
    broker = SimpleNamespace(
        daily=day,
        weekly=day,
        as_of=now,
    )
    monkeypatch.setattr(trading_state, "closed_trades", lambda: [])
    monkeypatch.setattr(trading_state, "trade_count_today", lambda _now=None: 13)
    monkeypatch.setattr(
        trading_state,
        "current_source",
        lambda: SimpleNamespace(
            program_pnl=lambda _now=None: broker,
        ),
    )
    r = client.get("/api/risk")
    assert r.status_code == 200
    b = r.json()
    assert set(b) >= {
        "trades_today", "max_trades_per_day",
        "daily_pnl", "weekly_pnl", "consecutive_losses", "cooldown_remaining_s",
        "daily_profit_target", "max_daily_loss", "max_weekly_loss",
        "pnl_basis",
    }
    assert isinstance(b["trades_today"], int)
    assert b["trades_today"] == 13
    assert isinstance(b["max_trades_per_day"], int)
    assert b["daily_pnl"] == "602.00"
    assert b["weekly_pnl"] == "602.00"
    assert b["account_daily_pnl"] == "1527.16"
    assert b["external_daily_gross_pnl"] == "929.00"
    assert b["account_daily_adjustment"] == "-3.84"
    assert b["pnl_basis"] == "alpaca_program_fills"
    # 2026-09-01：`chase_sigma_k` 随追高闸删除，风控快照不再下发它。
    assert "chase_sigma_k" not in b


def test_list_templates_exposes_independent_rows():
    r = client.get("/api/templates")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {
        "active", "active_id", "pending", "pending_id", "market_locked", "templates",
    }
    assert isinstance(body["market_locked"], bool)
    assert len(body["templates"]) == 1
    assert all({"id", "name", "status", "reason"} <= set(row) for row in body["templates"])
    assert all("params" not in row and "templates" not in row for row in body["templates"])
    assert all("whitelist" not in row for row in body["templates"])


def test_list_templates_exposes_real_market_lock(monkeypatch):
    monkeypatch.setattr("app.templates.is_market_hours", lambda now=None: True)
    r = client.get("/api/templates")
    assert r.status_code == 200
    assert r.json()["market_locked"] is True


def test_template_params_requires_reason():
    r = client.post(
        "/api/templates/params",
        json={"name": "测试配置", "changes": {"stop_pct": "0.25"}, "reason": "  "},
    )
    assert r.status_code == 422


def test_template_params_requires_name():
    r = client.post(
        "/api/templates/params",
        json={"name": "  ", "changes": {"stop_pct": "0.25"}, "reason": "回测收紧"},
    )
    assert r.status_code == 422


def test_template_params_rejects_iron_law_key():
    # T1 铁律：仓位 $500 锁死，不可经 API 修改
    r = client.post(
        "/api/templates/params",
        json={"name": "越权配置", "changes": {"max_position_size": "1000"}, "reason": "试改仓位"},
    )
    assert r.status_code == 422


def test_template_params_ok(monkeypatch):
    from app.routers import config as cfg_router
    monkeypatch.setattr(
        cfg_router,
        "save_template",
        lambda name, changes, reason=None: {
            "status": "saved", "active": "默认",
            "template": {"id": 4, "name": name, "params": changes},
        },
    )
    r = client.post(
        "/api/templates/params",
        json={"name": "回测收紧版", "changes": {"stop_pct": "0.25"}, "reason": "回测收紧"},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "saved"
    assert r.json()["template"]["name"] == "回测收紧版"


def test_load_template_ok(monkeypatch):
    from app.routers import config as cfg_router
    monkeypatch.setattr(
        cfg_router,
        "load_template",
        lambda template_id: {
            "status": "active", "active": "配置A", "pending": None,
            "template": {"id": template_id, "name": "配置A"},
        },
    )
    r = client.post("/api/templates/7/load")
    assert r.status_code == 200
    assert r.json()["template"]["id"] == 7


def test_load_template_rejects_unknown(monkeypatch):
    from app.routers import config as cfg_router

    def missing(_template_id):
        raise KeyError("missing")

    monkeypatch.setattr(cfg_router, "load_template", missing)
    r = client.post("/api/templates/999/load")
    assert r.status_code == 404


def test_load_template_intraday_returns_423(monkeypatch):
    from app.routers import config as cfg_router

    def locked(_template_id):
        raise cfg_router.TemplateLockedError("美股盘中不可加载配置模板")

    monkeypatch.setattr(cfg_router, "load_template", locked)
    r = client.post("/api/templates/7/load")
    assert r.status_code == 423
    assert "盘中不可加载" in r.json()["detail"]


def test_delete_template_ok(monkeypatch):
    from app.routers import config as cfg_router
    monkeypatch.setattr(
        cfg_router,
        "delete_template",
        lambda template_id: {"deleted": True, "id": template_id, "name": "旧配置"},
    )
    r = client.delete("/api/templates/7")
    assert r.status_code == 200
    assert r.json() == {"deleted": True, "id": 7, "name": "旧配置"}


def test_orders_endpoint_returns_three_groups():
    r = client.get("/api/orders")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"pending", "filled", "cancelled"}
    assert all(isinstance(body[k], list) for k in body)


def test_data_source_switch_api_is_removed():
    assert client.get("/api/config/data-source").status_code == 404
    assert client.post("/api/config/data-source", json={"mode": "real"}).status_code == 404


def test_execute_watcher_offline_rejects_gracefully_not_500(monkeypatch):
    """watcher 档信号离线时 /api/execute 应优雅拒单（带原因）而非穿透成 500（I1）。

    默认无 lifespan 启动 → watcher WS 未连 → signal_online=False → market_context 抛离线，
    应被 /api/execute 捕获降级为 {executed:False, gate:{allowed:False, reason 含"离线"}}。
    """
    def offline(_source, _ticker, _direction):
        raise RuntimeError("盯盘引擎未启动 — 信号离线，下单已锁")

    monkeypatch.setattr("app.exec_entry.build_market_context", offline)
    r = client.post("/api/execute", json={"ticker": "SPY", "direction": "long"})
    assert r.status_code == 200  # 关键：不是 500
    body = r.json()
    assert body["executed"] is False
    assert body["gate"]["allowed"] is False
    assert "离线" in body["gate"]["reason"]
