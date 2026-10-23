# -*- coding: utf-8 -*-
"""通用撤单：券商订单是真源，不依赖重启即丢的 REGISTRY。

覆盖两类历史故障：
1. 入场买单仍在券商/JOURNAL，但进程重启后 REGISTRY 已清空，旧实现直接 404；
2. 平仓卖单用 close_order_id，旧通用撤单只查 entry order_id，前端点撤单必失败。
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal as D
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import app.exec_close as order_exec
from app.order_state import JOURNAL, REGISTRY, ManagedPosition


_CODE = "QQQ260726C00700000"


def _source():
    return SimpleNamespace(name="watcher")


class _Broker:
    def __init__(self, status="new", *, cancel_error=None, after_cancel="canceled"):
        self.status = status
        self.cancel_error = cancel_error
        self.after_cancel = after_cancel
        self.cancelled = []

    def get_order(self, order_id):
        return {
            "id": order_id,
            "status": self.status,
            "filled_qty": "1" if self.status == "filled" else "0",
            "filled_avg_price": "1.23" if self.status == "filled" else None,
        }

    def cancel(self, order_id):
        if self.cancel_error:
            raise self.cancel_error
        self.cancelled.append(order_id)
        self.status = self.after_cancel


def _pending_buy(order_id="buy-1"):
    JOURNAL.submitted(order_id, "QQQ", _CODE, "buy", 1, D("1.20"))


def _closing_sell(order_id="sell-1"):
    pos = ManagedPosition(
        option_code=_CODE, ticker="QQQ", direction="long", qty=2,
        entry_mid=D("1.00"), entry_time=datetime.now(timezone.utc),
        order_id="entry-1", status="closing",
    )
    pos.close_order_id = order_id
    pos.close_qty = 1
    pos.close_reason = "手动减仓"
    REGISTRY.record(pos)
    JOURNAL.submitted(order_id, "QQQ", _CODE, "sell", 1, reason="手动减仓")
    return pos


def test_cancel_journal_only_pending_after_registry_was_lost(monkeypatch):
    """重启后 REGISTRY 为空，只要券商仍在途，前端撤单仍必须成功。"""
    _pending_buy("lost-registry")
    broker = _Broker()
    monkeypatch.setattr(order_exec, "AlpacaTrading", lambda: broker)
    recorded = []
    monkeypatch.setattr(order_exec.trade_recorder, "record_cancelled", recorded.append)

    result = order_exec.cancel(_source(), "lost-registry")

    assert result["cancelled"] is True
    assert broker.cancelled == ["lost-registry"]
    assert JOURNAL._events["lost-registry"].status == "cancelled"
    assert JOURNAL._events["lost-registry"].cancel_cause == "manual"
    assert recorded == ["lost-registry"]


def test_cancel_generic_endpoint_routes_resting_close_order(monkeypatch):
    """订单面板的平仓卖单也走 /api/orders/{id}/cancel，不能因只查 entry id 而 404。"""
    pos = _closing_sell()
    broker = _Broker()
    monkeypatch.setattr(order_exec, "AlpacaTrading", lambda: broker)
    monkeypatch.setattr(order_exec.trade_recorder, "record_cancelled", lambda oid: None)

    result = order_exec.cancel(_source(), "sell-1")

    assert result["cancelled"] is True
    assert broker.cancelled == ["sell-1"]
    assert JOURNAL._events["sell-1"].status == "cancelled"
    assert pos.status == "open"
    assert pos.close_order_id is None


def test_cancel_failure_keeps_local_order_pending(monkeypatch):
    """券商撤单失败时不得把本地改成 cancelled、不得对前端谎报成功。"""
    _pending_buy()
    broker = _Broker(cancel_error=RuntimeError("broker timeout"))
    monkeypatch.setattr(order_exec, "AlpacaTrading", lambda: broker)
    recorded = []
    monkeypatch.setattr(order_exec.trade_recorder, "record_cancelled", recorded.append)

    with pytest.raises(HTTPException) as exc:
        order_exec.cancel(_source(), "buy-1")

    assert exc.value.status_code == 502
    assert JOURNAL._events["buy-1"].status == "pending"
    assert recorded == []


def test_cancel_that_raced_with_fill_reports_filled(monkeypatch):
    """点撤单前券商已成交：回填成交而不是显示撤单失败/已撤。"""
    _pending_buy()
    broker = _Broker(status="filled")
    monkeypatch.setattr(order_exec, "AlpacaTrading", lambda: broker)
    filled = []
    monkeypatch.setattr(order_exec.trade_recorder, "record_filled",
                        lambda oid, **kw: filled.append((oid, kw)))

    result = order_exec.cancel(_source(), "buy-1")

    assert result == {"cancelled": False, "order_id": "buy-1", "status": "filled"}
    assert broker.cancelled == []
    assert JOURNAL._events["buy-1"].status == "filled"
    assert JOURNAL._events["buy-1"].price == D("1.23")
    assert filled[0][0] == "buy-1"


def test_filled_close_cancel_race_applies_only_unsettled_delta(monkeypatch):
    """close_run 已应用的累计成交不能在用户撤单撞上 filled 时再次扣减。"""
    pos = ManagedPosition(
        option_code=_CODE, ticker="QQQ", direction="long", qty=10,
        entry_mid=D("1.00"), entry_time=datetime.now(timezone.utc),
        order_id="entry-1", status="closing",
    )
    pos.close_order_id = "sell-1"
    pos.close_qty = 5
    pos.close_applied_qty = 2
    pos.close_reason = "手动减仓"
    REGISTRY.record(pos)
    JOURNAL.submitted("sell-1", "QQQ", _CODE, "sell", 5, reason="手动减仓")
    recorded = []
    monkeypatch.setattr(
        order_exec.trade_recorder,
        "record_closed",
        lambda oid, mid, qty, reason: recorded.append(qty),
    )

    result = order_exec._settle_filled_order(
        "sell-1",
        {"status": "filled", "filled_qty": "5", "filled_avg_price": "1.25"},
        None,
        pos,
    )

    assert result["status"] == "filled"
    assert pos.qty == 7
    assert recorded == [3]


def test_entry_cancel_race_does_not_erase_later_adds(monkeypatch):
    """原始入场单终态晚到时，不能把已经包含加仓的总仓位重置为原始成交量。"""
    pos = ManagedPosition(
        option_code=_CODE, ticker="QQQ", direction="long", qty=4,
        entry_mid=D("1.10"), entry_time=datetime.now(timezone.utc),
        order_id="buy-1", status="open",
        entry_filled_qty=1, entry_fill_mid=D("1.00"),
    )
    REGISTRY.record(pos)
    JOURNAL.submitted("buy-1", "QQQ", _CODE, "buy", 1, D("1.00"))
    monkeypatch.setattr(
        order_exec.trade_recorder, "record_position_sync", lambda *a, **k: None)

    order_exec._settle_filled_order(
        "buy-1",
        {"status": "filled", "filled_qty": "1", "filled_avg_price": "1.00"},
        pos,
        None,
    )

    assert pos.qty == 4
    assert pos.entry_filled_qty == 1


def test_cancel_already_cancelled_at_broker_settles_local(monkeypatch):
    _pending_buy()
    broker = _Broker(status="canceled")
    monkeypatch.setattr(order_exec, "AlpacaTrading", lambda: broker)
    monkeypatch.setattr(order_exec.trade_recorder, "record_cancelled", lambda oid: None)

    result = order_exec.cancel(_source(), "buy-1")

    assert result["cancelled"] is True
    assert result["already_terminal"] is True
    assert broker.cancelled == []
    assert JOURNAL._events["buy-1"].status == "cancelled"


def test_cancel_broker_not_found_settles_local_without_name_error(monkeypatch):
    """券商明确 404 时应清理本地在途单，不能因拆分遗漏 helper 而返回 500。"""
    _pending_buy("missing-order")

    class _MissingBroker(_Broker):
        def get_order(self, order_id):
            raise RuntimeError(f"404 Order {order_id} not found")

    broker = _MissingBroker()
    monkeypatch.setattr(order_exec, "AlpacaTrading", lambda: broker)
    monkeypatch.setattr(order_exec.trade_recorder, "record_cancelled", lambda _oid: None)

    result = order_exec.cancel(_source(), "missing-order")

    assert result["cancelled"] is True
    assert result["already_terminal"] is True
    assert broker.cancelled == []
    assert JOURNAL.get("missing-order").status == "cancelled"


def test_cancel_pending_is_audited_with_order_context(monkeypatch):
    _pending_buy()
    broker = _Broker(after_cancel="new")
    monkeypatch.setattr(order_exec, "AlpacaTrading", lambda: broker)
    monkeypatch.setattr(order_exec.time, "sleep", lambda *_: None)
    audits = []
    monkeypatch.setattr(
        order_exec.trade_recorder, "audit",
        lambda event, **fields: audits.append((event, fields)),
    )

    result = order_exec.cancel(_source(), "buy-1")

    assert result["status"] == "cancel_pending"
    assert JOURNAL.get("buy-1").cancel_cause == "manual"
    assert [event for event, _ in audits] == [
        "trade_cancel_requested",
        "trade_cancel_pending",
    ]
    assert audits[0][1]["order_id"] == "buy-1"
    assert audits[0][1]["ticker"] == "QQQ"
    assert audits[0][1]["contract"] == _CODE
    assert audits[0][1]["side"] == "buy"
    assert audits[1][1]["broker_status"] == "new"


def test_resting_close_cancel_pending_stays_marked_until_terminal(monkeypatch):
    pos = _closing_sell()
    broker = _Broker(after_cancel="new")
    monkeypatch.setattr(order_exec, "AlpacaTrading", lambda: broker)
    monkeypatch.setattr(order_exec.time, "sleep", lambda *_: None)

    result = order_exec.cancel(_source(), "sell-1")

    assert result["status"] == "cancel_pending"
    assert pos.cancel_requested is True
    assert pos.close_order_id == "sell-1"
    assert JOURNAL.get("sell-1").cancel_cause == "manual"


def test_cancel_waits_for_trading_stream_confirmation_when_connected(monkeypatch):
    """撤单动作仍走 REST，但确认阶段优先等 trade_updates，不再连续查六次 REST。"""
    from app.trade_update_stream import TRADE_UPDATE_STREAM

    _pending_buy()
    broker = _Broker(after_cancel="new")
    get_calls = []
    original_get = broker.get_order

    def counted_get(order_id):
        get_calls.append(order_id)
        return original_get(order_id)

    broker.get_order = counted_get
    monkeypatch.setattr(order_exec, "AlpacaTrading", lambda: broker)
    monkeypatch.setattr(order_exec.trade_recorder, "record_cancelled", lambda _oid: None)
    TRADE_UPDATE_STREAM.connected = True
    monkeypatch.setattr(TRADE_UPDATE_STREAM, "order_revision", lambda _oid: 3)
    monkeypatch.setattr(
        TRADE_UPDATE_STREAM,
        "wait_for_order",
        lambda *_args, **_kwargs: {
            "id": "buy-1",
            "event": "canceled",
            "status": "canceled",
            "filled_qty": "0",
            "filled_avg_price": None,
            "revision": 4,
        },
    )

    result = order_exec.cancel(_source(), "buy-1")

    assert result["cancelled"] is True
    assert get_calls == ["buy-1"]  # 撤单前的竞态保护查询；确认阶段没有 REST 轮询


def test_entry_cancel_after_partial_fill_keeps_position_open(monkeypatch):
    """撤掉剩余数量不等于撤掉已经成交的仓位。"""
    from app.trade_update_stream import TRADE_UPDATE_STREAM

    _pending_buy()
    pos = ManagedPosition(
        option_code=_CODE, ticker="QQQ", direction="long", qty=2,
        entry_mid=D("1.20"), entry_time=datetime.now(timezone.utc),
        order_id="buy-1", status="pending",
    )
    REGISTRY.record(pos)
    broker = _Broker(after_cancel="new")
    monkeypatch.setattr(order_exec, "AlpacaTrading", lambda: broker)
    monkeypatch.setattr(order_exec.trade_recorder, "record_filled", lambda *_a, **_k: None)
    TRADE_UPDATE_STREAM.connected = True
    monkeypatch.setattr(TRADE_UPDATE_STREAM, "order_revision", lambda _oid: 1)
    monkeypatch.setattr(
        TRADE_UPDATE_STREAM,
        "wait_for_order",
        lambda *_args, **_kwargs: {
            "id": "buy-1",
            "event": "canceled",
            "status": "canceled",
            "filled_qty": "1",
            "filled_avg_price": "1.22",
            "revision": 2,
        },
    )

    result = order_exec.cancel(_source(), "buy-1")

    assert result["status"] == "partially_filled"
    assert pos.status == "open"
    assert pos.qty == 1
    assert pos.entry_mid == D("1.22")
    assert JOURNAL.get("buy-1").status == "filled"


def test_close_cancel_already_settled_by_watchdog_is_not_settled_twice(monkeypatch):
    """撤单终态只能结算一次。

    2026-07-31 实盘：同一笔平仓撤单被 watchdog 巡检和前台 cancel() 各审计一次
    （相隔 60~70ms）。除审计重复外，后到的 cancel() 还会把 watchdog 已判定
    closed 的持仓用 set_status 改回 open，等于凭空复活仓位。
    """
    from app.trade_update_stream import TRADE_UPDATE_STREAM

    pos = _closing_sell("sell-dup")
    audits: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        order_exec.trade_recorder, "audit",
        lambda event, **kw: audits.append((event, kw)))
    monkeypatch.setattr(order_exec.trade_recorder, "record_cancelled", lambda *_a, **_k: None)

    broker = _Broker()
    broker_cancel = broker.cancel

    def cancel_then_watchdog_settles(order_id):
        """券商受理撤单的同一拍，watchdog 抢先看到 canceled 并完成结算。

        与 watchdog 真实路径同构：先拿终态令牌，再认领审计权后才写审计。
        """
        broker_cancel(order_id)
        JOURNAL.mark_cancelled(order_id, broker_status="canceled")
        if JOURNAL.claim_terminal_audit(order_id):
            order_exec.trade_recorder.audit(
                "trade_cancel_confirmed", order_id=order_id,
                trigger="manual_cancel", status="cancelled")
        pos.close_order_id = None
        pos.close_qty = 0
        pos.close_applied_qty = 0
        pos.cancel_requested = False
        pos.status = "closed"

    broker.cancel = cancel_then_watchdog_settles
    monkeypatch.setattr(order_exec, "AlpacaTrading", lambda: broker)
    TRADE_UPDATE_STREAM.connected = False

    result = order_exec.cancel(_source(), "sell-dup")

    confirmed = [kw for event, kw in audits if event == "trade_cancel_confirmed"]
    assert len(confirmed) == 1, f"撤单终态被结算了 {len(confirmed)} 次"
    assert pos.status == "closed", "已终结的持仓不得被后到的撤单结算复活"
    # 输掉结算令牌必须可观测，否则盘中无法分辨"竞态被挡住"与"竞态没发生"
    assert result["already_settled"] is True


def test_cancel_still_audited_when_terminal_was_claimed_by_silent_writer(monkeypatch):
    """终态被「只同步状态、不写审计」的路径抢先置位时，审计不能整条丢失。

    2026-08-01 MOCK 实测：8 笔撤单有 6 笔没有任何 trade_cancel_confirmed。
    原因是审计被绑在结算令牌上，而抢到令牌的那方根本不写审计。审计的幂等锚点
    必须独立于状态迁移——谁先到谁写，且只写一次。
    """
    from app.trade_update_stream import TRADE_UPDATE_STREAM

    pos = _closing_sell("sell-silent")
    # 第三方（纯状态同步路径）已把该单推进终态，且没有写过任何审计
    JOURNAL.mark_cancelled("sell-silent", broker_status="canceled")

    audits: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        order_exec.trade_recorder, "audit",
        lambda event, **kw: audits.append((event, kw)))
    monkeypatch.setattr(order_exec.trade_recorder, "record_cancelled", lambda *_a, **_k: None)
    broker = _Broker(status="canceled")
    monkeypatch.setattr(order_exec, "AlpacaTrading", lambda: broker)
    TRADE_UPDATE_STREAM.connected = False

    order_exec.cancel(_source(), "sell-silent")

    confirmed = [kw for event, kw in audits if event == "trade_cancel_confirmed"]
    assert len(confirmed) == 1, "落败方必须补写审计，撤单不能没有任何审计记录"
    assert pos.close_order_id is None, "同时仍要清掉本地平仓跟踪"


def test_terminal_audit_can_only_be_claimed_once():
    """审计认领本身必须是一次性的，否则补写会变成重复写。"""
    JOURNAL.submitted("claim-once", "QQQ", _CODE, "sell", 1)

    first = JOURNAL.claim_terminal_audit("claim-once")
    second = JOURNAL.claim_terminal_audit("claim-once")
    third = JOURNAL.claim_terminal_audit("claim-once")

    assert (first, second, third) == (True, False, False)
    # 不在 JOURNAL 里的订单无从去重，必须放行（宁可多写也不能丢）
    assert JOURNAL.claim_terminal_audit("never-seen") is True


def test_不得从0DTE面板撤掉兄弟系统的sos单(monkeypatch):
    """归属闸必须装在 cancel 本身，而不只是靠「订单流里看不见」。

    SOS(nDTE) 与 0DTE 共用一个 Alpaca 账户，靠 client_order_id 前缀分家。SOS 侧的
    DELETE /api/sos/orders/{id} 早就写死「仅限 sos- 前缀」，0DTE 侧却没有对称的闸：
    只要订单在 JOURNAL 里是 pending 就撤。撤掉的若是 SOS 的止盈单，那边的止盈保护
    就凭空消失了，而 SOS 自己完全不知道是谁干的。
    """
    JOURNAL.submitted(
        "sos-tp-1", "SLV", _CODE, "sell", 1,
        client_order_id="sos-3f2a1b4c-0000-4000-8000-000000000001",
    )
    broker = _Broker()
    monkeypatch.setattr(order_exec, "AlpacaTrading", lambda: broker)

    with pytest.raises(HTTPException) as exc:
        order_exec.cancel(_source(), "sos-tp-1")

    assert exc.value.status_code == 403
    assert "SOS" in str(exc.value.detail)
    assert broker.cancelled == [], "券商侧一次撤单都不许发出"
