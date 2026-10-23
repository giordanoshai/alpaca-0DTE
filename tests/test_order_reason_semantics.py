"""订单面板必须明确“谁发起、做什么、谁撤单、Alpaca 什么终态”。

这些信息是订单读模型的结构化事实，不允许前端仅凭 buy/sell 或 canceled 猜测。
"""
from datetime import datetime, timezone

from app.order_state import OrderJournal
from frontend_src import app_js


NOW = datetime(2026, 7, 27, 15, 30, tzinfo=timezone.utc)
CODE = "NVDA260727P00200000"


def _broker_order(*, oid: str, client_id: str, status: str, side: str = "sell") -> dict:
    return {
        "id": oid,
        "client_order_id": client_id,
        "symbol": CODE,
        "side": side,
        "qty": "3",
        "filled_qty": "3" if status == "filled" else "0",
        "status": status,
        "submitted_at": NOW.isoformat(),
        "updated_at": NOW.isoformat(),
        "filled_avg_price": "2.10" if status == "filled" else None,
        "type": "limit",
        "limit_price": "2.08",
    }


def test_client_id_only_recovers_program_ownership_not_business_reason():
    journal = OrderJournal()
    journal.upsert_broker_orders([_broker_order(
        oid="add-1",
        client_id="ttt-01234567-89ab-cdef-0123-456789abcdef",
        status="filled",
        side="buy",
    )])

    order = journal.current_session(NOW)["filled"][0]
    assert order["origin"] == "program"
    assert order["action"] == "unknown"
    assert order["broker_status"] == "filled"


def test_manual_cancel_keeps_user_origin_when_broker_confirms():
    journal = OrderJournal()
    journal.submitted(
        "open-1", "NVDA", CODE, "buy", 3,
        origin="user", action="open",
    )
    journal.mark_cancel_requested(
        "open-1", cancel_origin="user", reason="用户手动撤单")
    journal.mark_cancelled("open-1", broker_status="canceled")

    order = journal.today()["cancelled"][0]
    assert order["cancel_origin"] == "user"
    assert order["fail_reason"] == "用户手动撤单 · Alpaca 已确认取消"


def test_alpaca_rejection_does_not_invent_a_detail():
    journal = OrderJournal()
    journal.upsert_broker_orders([_broker_order(
        oid="reject-1",
        client_id="ttt-01234567-89ab-cdef-0123-456789abcdef",
        status="rejected",
        side="buy",
    )])

    order = journal.current_session(NOW)["cancelled"][0]
    assert order["broker_status"] == "rejected"
    assert order["fail_reason"] == "Alpaca 拒单 · 券商未提供更详细原因"


def test_legacy_cancel_does_not_guess_who_cancelled_it():
    journal = OrderJournal()
    journal.upsert_broker_orders([_broker_order(
        oid="legacy-1",
        client_id="legacy-order-without-ttt-prefix",
        status="canceled",
    )])

    order = journal.current_session(NOW)["cancelled"][0]
    assert order["origin"] == "unknown"
    assert order.get("cancel_origin") is None
    assert order["fail_reason"] == "Alpaca 已取消 · 无法从券商状态判断撤单发起者"


def test_frontend_renders_action_badge_for_pending_filled_and_cancelled():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    js = app_js()
    html = (root / "static" / "index.html").read_text(encoding="utf-8")

    for label in ("手动开仓", "手动加仓", "手动减仓", "手动全平", "来源不明"):
        assert label in js
    # 待成交/已取消保留来源徽章；已成交改用固定列，原因放在最右侧文本列。
    assert html.count("{{ o.reasonTag.label }}") >= 2
    assert 'title="{{ o.reasonText }}"' in html


def test_persistent_journal_writes_each_state_with_full_reason(monkeypatch):
    writes = []
    monkeypatch.setattr(
        "app.trade_recorder.record_order_event",
        lambda event: writes.append(dict(event)),
    )
    journal = OrderJournal(persist=True)

    journal.submitted(
        "reduce-1", "NVDA", CODE, "sell", 2,
        reason="减半仓", origin="user", action="reduce",
        parent_order_id="entry-1",
    )
    journal.mark_cancel_requested(
        "reduce-1", cancel_origin="system",
        reason="平仓挂单超时 5 秒未成交，系统自动撤单",
    )
    journal.mark_cancelled("reduce-1", broker_status="canceled")

    assert [row["status"] for row in writes] == ["pending", "pending", "cancelled"]
    final = writes[-1]
    assert final["parent_order_id"] == "entry-1"
    assert final["action"] == "reduce"
    assert final["reason"] == "减半仓"
    assert final["cancel_origin"] == "system"
    assert final["broker_status"] == "canceled"
    assert "系统自动撤单" in final["fail_reason"]


def test_rebuild_from_order_events_preserves_reason_and_parent():
    journal = OrderJournal()
    journal.rebuild_from_order_events([{
        "order_id": "reduce-1",
        "parent_order_id": "entry-1",
        "client_order_id": "ttt-01234567-89ab-cdef-0123-456789abcdef",
        "ticker": "NVDA",
        "option_code": CODE,
        "side": "sell",
        "qty": 2,
        "status": "filled",
        "origin": "user",
        "action": "reduce",
        "reason": "减半仓",
        "triggered_at": NOW,
        "trigger_bid": "0.76",
        "trigger_ask": "0.82",
        "trigger_value": "380",
        "trigger_loss": "130",
        "stop_mode": "amount",
        "stop_threshold": "100",
        "entry_value": "510",
        "submitted_at": NOW,
        "updated_at": NOW,
    }])

    order = journal.current_session(NOW)["filled"][0]
    assert order["reason"] == "减半仓"
    assert order["parent_order_id"] == "entry-1"
    assert order["action"] == "reduce"
    assert order["triggered_at"] == NOW.isoformat()
    assert order["trigger_bid"] == "0.76"
    assert order["trigger_loss"] == "130"
    assert order["stop_mode"] == "amount"
    assert order["stop_threshold"] == "100"
