"""订单、持仓、风控经交易终端 /ws 单通道推送。"""
from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.trading_state as trading_state
from app.order_state import JOURNAL
from app.risk import ClosedTrade
from app.routers import trading_ws
from app.templates import get_store
from app.trading_state import TradingStateHub, risk_snapshot
from frontend_src import app_js


def _source_with_account_pnl(
    *,
    daily: str = "602.00",
    weekly: str = "602.00",
    as_of: datetime | None = None,
):
    stamp = as_of or datetime(2026, 7, 27, 14, 0, tzinfo=timezone.utc)
    day = SimpleNamespace(
        program_gross_pnl=Decimal(daily),
        external_gross_pnl=Decimal("929.00"),
        account_net_pnl=Decimal("1527.16"),
        account_adjustment=Decimal("-3.84"),
    )
    week = SimpleNamespace(
        program_gross_pnl=Decimal(weekly),
        external_gross_pnl=Decimal("929.00"),
        account_net_pnl=Decimal("1527.16"),
        account_adjustment=Decimal("-3.84"),
    )
    return SimpleNamespace(
        program_pnl=lambda _now=None: SimpleNamespace(
            daily=day,
            weekly=week,
            as_of=stamp,
        ),
        trade_count_today=lambda _now=None: 13,
    )


def test_trading_state_hub_builds_one_shared_snapshot(monkeypatch):
    source = _source_with_account_pnl()
    calls = []
    positions = {"positions": [{"option_code": "QQQ260727C00500000"}], "unmanaged": []}
    monkeypatch.setattr(trading_state, "current_source", lambda: source)
    monkeypatch.setattr(trading_state, "closed_trades", lambda: [])
    monkeypatch.setattr(trading_state, "trade_count_today", lambda _now=None: 13)
    monkeypatch.setattr(
        trading_state,
        "list_positions",
        lambda src, **kwargs: calls.append((src, kwargs)) or positions,
    )

    JOURNAL.submitted(
        "O1", "QQQ", "QQQ260727C00500000", "buy", 1,
        price=Decimal("1.25"),
    )
    hub = TradingStateHub()
    assert hub.positions_interval_s == 0.5
    hub.refresh_fast()
    asyncio.run(hub.refresh_positions())

    snap = hub.snapshot()
    assert calls == [(source, {"include_unmanaged": False})]
    assert snap["orders"]["pending"][0]["order_id"] == "O1"
    assert snap["positions"] == positions
    assert snap["risk"]["max_trades_per_day"] > 0
    assert snap["broker_sync"]["channel"] == "trade_updates"


def test_order_revision_advances_on_et_session_rollover(monkeypatch):
    pending = {"pending": [{"order_id": "O1"}], "filled": [], "cancelled": []}
    hub = TradingStateHub()
    hub._orders = pending
    hub._orders_source_revision = JOURNAL.revision
    hub._orders_session_date = "2026-07-30"
    before = hub.snapshot()["revisions"]["orders"]
    monkeypatch.setattr(
        "app.trading_state.order_session_window",
        lambda _now=None: {"session_date": "2026-07-31"},
    )
    monkeypatch.setattr(JOURNAL, "current_session", lambda *_args, **_kwargs: pending)

    assert hub._refresh_orders()
    assert hub.snapshot()["revisions"]["orders"] == before + 1


def test_risk_snapshot_uses_alpaca_pnl_and_local_trade_streak(monkeypatch):
    now = datetime(2026, 7, 27, 14, 0, tzinfo=timezone.utc)
    trades = [
        ClosedTrade(exit_time=now - timedelta(minutes=3), pnl=Decimal("20.40")),
        ClosedTrade(exit_time=now - timedelta(minutes=2), pnl=Decimal("-5.00")),
        ClosedTrade(exit_time=now - timedelta(minutes=1), pnl=Decimal("-4.00")),
        ClosedTrade(exit_time=now - timedelta(days=8), pnl=Decimal("999.00")),
    ]
    monkeypatch.setattr(trading_state, "closed_trades", lambda: trades)
    monkeypatch.setattr(
        trading_state,
        "current_source",
        lambda: _source_with_account_pnl(as_of=now),
    )
    monkeypatch.setattr(trading_state, "trade_count_today", lambda _now=None: 13)

    snap = risk_snapshot(now)

    assert snap["daily_pnl"] == "602.00"
    assert snap["weekly_pnl"] == "602.00"
    assert snap["trades_today"] == 13
    assert snap["consecutive_losses"] == 2
    assert snap["cooldown_remaining_s"] == 540
    assert snap["pnl_basis"] == "alpaca_program_fills"
    assert snap["week_starts_on"] == "monday"


def test_risk_snapshot_uses_active_template_cooldown(monkeypatch):
    now = datetime(2026, 7, 27, 14, 0, tzinfo=timezone.utc)
    store = get_store()
    active = store.active()
    store._templates[store.active_name()] = replace(  # noqa: SLF001
        active,
        cooldown_seconds=120,
    )
    monkeypatch.setattr(
        trading_state,
        "closed_trades",
        lambda: [
            ClosedTrade(exit_time=now - timedelta(seconds=90), pnl=Decimal("-5")),
            ClosedTrade(exit_time=now - timedelta(seconds=30), pnl=Decimal("-4")),
        ],
    )
    monkeypatch.setattr(
        trading_state,
        "current_source",
        lambda: _source_with_account_pnl(as_of=now),
    )
    monkeypatch.setattr(trading_state, "trade_count_today", lambda _now=None: 13)

    snap = risk_snapshot(now)

    assert snap["consecutive_losses"] == 2
    assert snap["cooldown_remaining_s"] == 90


def test_broker_order_baseline_keeps_old_open_order(monkeypatch):
    monkeypatch.setattr(
        trading_state,
        "current_source",
        lambda: _source_with_account_pnl(),
    )
    monkeypatch.setattr(trading_state, "closed_trades", lambda: [])
    monkeypatch.setattr(trading_state, "trade_count_today", lambda _now=None: 13)
    monkeypatch.setattr(
        trading_state,
        "fetch_broker_order_session",
        lambda: [{
            "id": "296554a6-2fd5-4dd4-b18e-7d51a24338ad",
            "symbol": "SLV260727C00052500",
            "side": "buy",
            "qty": "10",
            "status": "accepted",
            "submitted_at": "2026-07-25T03:33:49.068Z",
            "updated_at": "2026-07-25T03:33:49.069095Z",
            "type": "limit",
            "limit_price": "0.61",
        }],
    )
    hub = TradingStateHub()

    result = asyncio.run(hub.refresh_broker_orders())
    hub.refresh_fast()

    assert result["orders_received"] == 1
    assert hub.snapshot()["orders"]["pending"][0]["order_id"] == (
        "296554a6-2fd5-4dd4-b18e-7d51a24338ad"
    )


def test_ws_frame_contains_shared_trading_state(monkeypatch):
    trading = {
        "orders": {"pending": [], "filled": [], "cancelled": []},
        "positions": {"positions": [], "unmanaged": []},
        "risk": {"trades_today": 0},
        "broker_sync": {"connected": True},
        "revisions": {
            "orders": 1,
            "positions": 1,
            "risk": 1,
            "broker_sync": 1,
        },
    }
    monkeypatch.setattr(
        trading_ws.MARKET_FEED,
        "snapshot",
        lambda: {"hbMs": 12, "ticks": [], "pool": [], "stale": False},
    )
    monkeypatch.setattr(trading_ws.TRADING_STATE, "snapshot", lambda: trading)
    options = {
        "previews": {"QQQ": {"selected": "QQQ260727C00500000"}},
        "subscription": {"desired": ["QQQ260727C00500000"]},
    }
    monkeypatch.setattr(trading_ws.OPTION_CANDIDATES, "snapshot", lambda: options)
    hub = trading_ws.TradingWsHub()
    monkeypatch.setattr(trading_ws, "TRADING_WS_HUB", hub)

    app = FastAPI()
    app.include_router(trading_ws.router)
    with TestClient(app).websocket_connect("/ws") as ws:
        frame = ws.receive_json()

    assert frame["hbMs"] == 12
    assert frame["schema_version"] == 2
    assert frame["type"] == "snapshot"
    assert frame["trading"] == {
        key: trading[key]
        for key in ("orders", "positions", "risk", "broker_sync")
    }
    assert frame["options"] == options


def test_ws_replies_to_browser_application_ping(monkeypatch):
    monkeypatch.setattr(
        trading_ws.MARKET_FEED,
        "snapshot",
        lambda: {"hbMs": 0, "ticks": [], "pool": []},
    )
    monkeypatch.setattr(
        trading_ws.TRADING_STATE,
        "snapshot",
        lambda: {
            "orders": {},
            "positions": {},
            "risk": {},
            "broker_sync": {},
            "revisions": {
                "orders": 1,
                "positions": 1,
                "risk": 1,
                "broker_sync": 1,
            },
        },
    )
    monkeypatch.setattr(
        trading_ws.OPTION_CANDIDATES,
        "snapshot",
        lambda: {"previews": {}, "subscription": {}},
    )
    monkeypatch.setattr(trading_ws, "TRADING_WS_HUB", trading_ws.TradingWsHub())
    app = FastAPI()
    app.include_router(trading_ws.router)

    with TestClient(app).websocket_connect("/ws") as ws:
        ws.send_json({"type": "ping", "id": 17})
        pong = None
        for _ in range(3):
            message = ws.receive_json()
            if message.get("type") == "pong":
                pong = message
                break

    assert pong == {"type": "pong", "id": 17}


def test_ws_hub_reuses_snapshot_encoding_and_omits_unchanged_orders():
    sections = {
        "feed": {"ticks": [], "pool": [], "signal_online": True},
        "orders": {"pending": [], "filled": [], "cancelled": []},
        "positions": {"positions": [], "unmanaged": []},
        "risk": {"trades_today": 0},
        "broker_sync": {"connected": True},
        "options": {"previews": {}, "subscription": {}},
        "walls": {},
    }
    tokens = {name: 1 for name in sections}
    hub = trading_ws.TradingWsHub()
    hub._capture = lambda: (sections, tokens)

    first = hub.subscribe()
    second = hub.subscribe()
    first_payload = first.get_nowait()
    second_payload = second.get_nowait()

    assert first_payload is second_payload

    tokens = {**tokens, "feed": 2}
    sections = {**sections, "feed": {"ticks": [{"sym": "QQQ"}], "pool": []}}
    patch = hub._advance()
    frame = __import__("json").loads(patch)

    assert frame["type"] == "patch"
    assert "orders" not in frame.get("trading", {})
    assert frame["ticks"] == [{"sym": "QQQ"}]


def test_ws_hub_queue_overflow_replaces_backlog_with_snapshot():
    sections = {
        "feed": {"ticks": [], "pool": []},
        "orders": {},
        "positions": {},
        "risk": {},
        "broker_sync": {},
        "options": {},
        "walls": {},
    }
    tokens = {name: 1 for name in sections}
    hub = trading_ws.TradingWsHub(queue_limit=1)
    hub._capture = lambda: (sections, tokens)
    queue = hub.subscribe()

    tokens = {**tokens, "feed": 2}
    sections = {**sections, "feed": {"ticks": [{"sym": "SPY"}], "pool": []}}
    patch = hub._advance()
    hub._broadcast(patch)

    frame = __import__("json").loads(queue.get_nowait())
    assert frame["type"] == "snapshot"
    assert frame["ticks"] == [{"sym": "SPY"}]


def test_frontend_no_longer_polls_contract_preview():
    from pathlib import Path

    js = app_js()
    assert "fetch('/api/contract-preview" not in js
    assert "ivContract" not in js


def test_frontend_uses_revisioned_region_factories():
    from pathlib import Path

    js = app_js()
    for factory in (
        "renderTickersBindings",
        "renderOrderEntryBindings",
        "renderPositionsBindings",
        "renderOrdersBindings",
        "renderRiskBindings",
        "renderSettingsBindings",
    ):
        assert factory in js
    assert "build(source, bindings)" in js
    assert "_riskSig" not in js
    assert "_ordersSig" not in js
    assert "_positionsSig" not in js
    assert "_wsFeedSig" not in js


def test_frontend_risk_cards_do_not_use_fake_pnl_values():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    js = app_js()
    html = (root / "static" / "index.html").read_text(encoding="utf-8")

    assert "const dayPnl = -64" not in js
    assert "weekPnl = -120" not in js
    assert "r2.daily_pnl" in js
    assert "r2.weekly_pnl" in js
    assert "今日本程序盈亏" in html


def test_frontend_risk_cards_use_real_position_capital_and_no_fake_intercepts():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    js = app_js()
    html = (root / "static" / "index.html").read_text(encoding="utf-8")

    assert "当前持仓资金" in html
    assert "真实 · 入场成本" in html
    assert "positionCapitalText" in js
    assert "Number(p.entryVal)" in js
    assert "regions: ['positions', 'risk']" in js
    assert "今日纪律率" not in html
    assert "今日拦截次数" not in html
    assert "blockLog" not in js
    assert "本周本程序盈亏" in html
