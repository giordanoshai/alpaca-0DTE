"""⑦ 订单事件日志 OrderJournal 单测 —— 生命周期迁移 + 当日三态分组。

阶段0 内存态：pending(下单未成交) → filled(已成交) / cancelled(已撤)。
持仓状态机(order_state.ManagedPosition)不变，此为独立的「订单流」视图数据源。
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.order_state import OrderEvent, OrderJournal, order_session_window


def _j() -> OrderJournal:
    return OrderJournal()


def test_submitted_records_pending():
    j = _j()
    ev = j.submitted("oid1", "QQQ", "QQQ240712C00470000", "buy", 2, Decimal("1.20"))
    assert isinstance(ev, OrderEvent)
    assert ev.status == "pending"
    grp = j.today()
    assert [e["order_id"] for e in grp["pending"]] == ["oid1"]
    assert grp["filled"] == [] and grp["cancelled"] == []


def test_mark_filled_moves_pending_to_filled():
    j = _j()
    j.submitted("oid1", "QQQ", "C1", "buy", 2, Decimal("1.20"))
    j.mark_filled("oid1")
    grp = j.today()
    assert grp["pending"] == []
    assert [e["order_id"] for e in grp["filled"]] == ["oid1"]
    assert grp["filled"][0]["status"] == "filled"


def test_mark_cancelled_moves_pending_to_cancelled():
    j = _j()
    j.submitted("oid1", "IWM", "C1", "buy", 1, Decimal("0.80"))
    j.mark_cancelled("oid1")
    grp = j.today()
    assert grp["pending"] == []
    assert [e["order_id"] for e in grp["cancelled"]] == ["oid1"]


def test_mark_unknown_order_is_noop():
    j = _j()
    j.mark_filled("nope")
    j.mark_cancelled("nope")
    grp = j.today()
    assert grp == {"pending": [], "filled": [], "cancelled": []}


def test_today_filters_out_other_days():
    j = _j()
    now = datetime(2026, 7, 16, 14, 0, tzinfo=timezone.utc)
    # 昨天的单：不该出现在「今日」
    j.submitted("old", "SPY", "C1", "buy", 1, Decimal("1.0"), ts=now - timedelta(days=1))
    j.submitted("new", "SPY", "C2", "buy", 1, Decimal("1.0"), ts=now)
    grp = j.today(now=now)
    assert [e["order_id"] for e in grp["pending"]] == ["new"]


def test_current_session_keeps_cross_day_pending_but_drops_old_terminal():
    j = _j()
    now = datetime(2026, 7, 27, 14, 0, tzinfo=timezone.utc)
    old = now - timedelta(hours=54)
    j.submitted("pending-old", "SLV", "SLV260727C00052500", "buy", 10, ts=old)
    j.submitted("filled-old", "SPY", "SPY260727C00600000", "buy", 1, ts=old)
    j.mark_filled("filled-old")
    # mark_filled 使用真实当前时间；把测试终态时间固定回窗口外。
    j._events["filled-old"].updated_ts = old

    groups = j.current_session(now=now)

    assert [e["order_id"] for e in groups["pending"]] == ["pending-old"]
    assert groups["filled"] == []


def test_upsert_broker_order_maps_accepted_to_pending():
    j = _j()
    result = j.upsert_broker_orders([{
        "id": "alpaca-1",
        "symbol": "SLV260727C00052500",
        "side": "buy",
        "qty": "10",
        "status": "accepted",
        "submitted_at": "2026-07-25T03:33:49.068Z",
        "updated_at": "2026-07-25T03:33:49.069095Z",
        "type": "limit",
        "limit_price": "0.61",
    }])

    order = j.current_session(
        now=datetime(2026, 7, 27, 14, 0, tzinfo=timezone.utc)
    )["pending"][0]
    assert result["inserted"] == 1
    assert order["order_id"] == "alpaca-1"
    assert order["ticker"] == "SLV"
    assert order["limit_price"] == "0.61"
    assert order["price_type"] == "limit"


def test_pending_program_sells_require_ttt_owner_and_exact_parent():
    j = _j()
    j.submitted(
        "owned-close",
        "INTC",
        "INTC260731C00094000",
        "sell",
        1,
        client_order_id="ttt-owned-close",
        parent_order_id="entry-1",
    )
    j.submitted(
        "other-parent",
        "INTC",
        "INTC260731C00094000",
        "sell",
        1,
        client_order_id="ttt-other-parent",
        parent_order_id="entry-2",
    )
    j.submitted(
        "external-close",
        "INTC",
        "INTC260731C00094000",
        "sell",
        1,
        client_order_id="manual-order",
        parent_order_id="entry-1",
    )

    rows = j.pending_program_sells_for(
        "INTC260731C00094000",
        parent_order_id="entry-1",
    )

    assert [row["order_id"] for row in rows] == ["owned-close"]


def test_broker_only_order_does_not_change_system_trade_limit():
    now = datetime.now(timezone.utc)
    j = _j()
    j.upsert_broker_orders([{
        "id": "external-fill",
        "symbol": "SLV260727C00052500",
        "side": "buy",
        "qty": "1",
        "filled_qty": "1",
        "status": "filled",
        "submitted_at": now.isoformat(),
        "updated_at": now.isoformat(),
        "filled_avg_price": "0.62",
    }])

    assert j.trade_count_today(now) == 0


def test_today_newest_first():
    j = _j()
    now = datetime(2026, 7, 16, 14, 0, tzinfo=timezone.utc)
    j.submitted("a", "QQQ", "C1", "buy", 1, Decimal("1.0"), ts=now - timedelta(minutes=5))
    j.submitted("b", "QQQ", "C2", "buy", 1, Decimal("1.0"), ts=now)
    grp = j.today(now=now)
    assert [e["order_id"] for e in grp["pending"]] == ["b", "a"]


def test_sell_side_recorded():
    j = _j()
    j.submitted("s1", "QQQ", "C1", "sell", 2, Decimal("1.50"))
    j.mark_filled("s1")
    grp = j.today()
    assert grp["filled"][0]["side"] == "sell"


def test_event_serialization_shape():
    j = _j()
    j.submitted("oid1", "QQQ", "QQQC1", "buy", 3, Decimal("1.25"))
    e = j.today()["pending"][0]
    assert set(e) >= {"order_id", "ticker", "option_code", "side", "qty", "status", "price", "ts"}
    assert e["qty"] == 3
    assert e["price"] == "1.25"  # Decimal 序列化为字符串（金额铁律）


def test_current_session_uses_et_calendar_day_boundaries_and_sparse_wire_shape():
    j = _j()
    now = datetime(2026, 7, 15, 18, 0, tzinfo=timezone.utc)
    samples = {
        "before": datetime(2026, 7, 15, 3, 59, 59, tzinfo=timezone.utc),
        "start": datetime(2026, 7, 15, 4, 0, tzinfo=timezone.utc),
        "last": datetime(2026, 7, 16, 3, 59, 59, tzinfo=timezone.utc),
        "next": datetime(2026, 7, 16, 4, 0, tzinfo=timezone.utc),
    }
    for order_id, stamp in samples.items():
        j.submitted(order_id, "QQQ", "C1", "buy", 1, ts=stamp)
        j.mark_filled(order_id)
        j._events[order_id].updated_ts = stamp

    groups = j.current_session(now=now)

    assert {row["order_id"] for row in groups["filled"]} == {"start", "last"}
    assert "reason" not in groups["filled"][0]
    assert "trigger_value" not in groups["filled"][0]


def test_order_session_window_handles_dst_and_et_day_boundaries():
    summer = order_session_window(
        datetime(2026, 7, 15, 14, 0, tzinfo=timezone.utc)
    )
    winter = order_session_window(
        datetime(2026, 1, 15, 15, 0, tzinfo=timezone.utc)
    )
    spring_dst = order_session_window(
        datetime(2026, 3, 8, 16, 0, tzinfo=timezone.utc)
    )
    fall_dst = order_session_window(
        datetime(2026, 11, 1, 16, 0, tzinfo=timezone.utc)
    )

    assert summer["session_start"] == datetime(
        2026, 7, 15, 4, 0, tzinfo=timezone.utc
    )
    assert winter["session_start"] == datetime(
        2026, 1, 15, 5, 0, tzinfo=timezone.utc
    )
    assert spring_dst["session_end"] - spring_dst["session_start"] == timedelta(
        hours=23
    )
    assert fall_dst["session_end"] - fall_dst["session_start"] == timedelta(
        hours=25
    )


def test_cross_day_pending_becomes_current_session_terminal():
    j = _j()
    old = datetime(2026, 7, 30, 14, 0, tzinfo=timezone.utc)
    filled_today = datetime(2026, 7, 31, 14, 0, tzinfo=timezone.utc)
    j.submitted("cross-day", "QQQ", "C1", "buy", 1, ts=old)
    j.mark_filled("cross-day", Decimal("1.20"), 1)
    j._events["cross-day"].updated_ts = filled_today

    groups = j.current_session(
        now=datetime(2026, 7, 31, 18, 0, tzinfo=timezone.utc)
    )

    assert [row["order_id"] for row in groups["filled"]] == ["cross-day"]


def test_weekend_returns_same_et_day_terminal_and_cross_day_pending():
    j = _j()
    saturday = datetime(2026, 8, 1, 14, 0, tzinfo=timezone.utc)
    friday = datetime(2026, 7, 31, 14, 0, tzinfo=timezone.utc)
    j.submitted("filled", "QQQ", "C1", "buy", 1, ts=saturday)
    j.mark_filled("filled")
    j._events["filled"].updated_ts = saturday
    j.submitted("pending", "QQQ", "C2", "buy", 1, ts=friday)

    groups = j.current_session(
        now=datetime(2026, 8, 1, 16, 0, tzinfo=timezone.utc)
    )

    assert [row["order_id"] for row in groups["filled"]] == ["filled"]
    assert [row["order_id"] for row in groups["pending"]] == ["pending"]


def test_order_revision_only_changes_for_visible_mutation():
    j = _j()
    j.submitted("o1", "QQQ", "C1", "buy", 1)
    submitted_revision = j.revision

    j.mark_filled("o1", price=Decimal("1.20"), qty=1)
    filled_revision = j.revision
    j.mark_filled("o1", price=Decimal("1.20"), qty=1)

    assert submitted_revision == 1
    assert filled_revision == 2
    assert j.revision == filled_revision


def test_trade_count_today_counts_filled_buys_only():
    j = _j()
    j.submitted("b1", "QQQ", "C1", "buy", 2); j.mark_filled("b1")
    j.submitted("b2", "SPY", "C2", "buy", 1)                       # pending 不计
    j.submitted("s1", "QQQ", "C1", "sell", 2); j.mark_filled("s1")  # 卖单不计
    j.submitted("b3", "IWM", "C3", "buy", 1); j.mark_cancelled("b3")  # 已撤不计
    assert j.trade_count_today() == 1


def test_clear_resets():
    j = _j()
    j.submitted("oid1", "QQQ", "C1", "buy", 1, Decimal("1.0"))
    j.clear()
    assert j.today() == {"pending": [], "filled": [], "cancelled": []}


def test_snapshot_and_broker_upsert_are_thread_safe():
    """WS 合并订单时，前端快照遍历不能因 dict 并发变更抛 RuntimeError。"""
    j = _j()
    j.submitted("seed", "QQQ", "C1", "buy", 1)
    snapshot_started = threading.Event()

    def include(_event):
        snapshot_started.set()
        time.sleep(0.02)
        return True

    def writer():
        snapshot_started.wait(timeout=1)
        j.upsert_broker_orders([{
            "id": "broker-2",
            "symbol": "QQQ260730C00600000",
            "side": "buy",
            "qty": "1",
            "status": "accepted",
        }], persist=False)

    thread = threading.Thread(target=writer)
    thread.start()
    grouped = j._grouped(include)
    thread.join(timeout=1)

    assert grouped["pending"][0]["order_id"] == "seed"
    assert j.get("broker-2") is not None


def test_pending_cancel_frame_preserves_local_cancel_reason():
    j = _j()
    j.submitted("o1", "QQQ", "QQQ260730C00600000", "buy", 1)
    j.mark_cancel_requested(
        "o1",
        cancel_origin="system",
        cancel_cause="timeout",
        reason="挂单超时 30 秒未成交，自动撤单",
    )

    j.upsert_broker_orders([{
        "id": "o1",
        "symbol": "QQQ260730C00600000",
        "side": "buy",
        "qty": "1",
        "status": "pending_cancel",
    }], persist=False)

    assert j.get("o1").fail_reason == "挂单超时 30 秒未成交，自动撤单"


# ── 跨系统订单归属隔离（2026-08-07）─────────────────────────────────────────
# SOS(nDTE) 与 0DTE 共用同一个 Alpaca 账户，靠 client_order_id 前缀分家：
# 0DTE 是 `ttt-`，SOS 是 `sos-`。`app/services/sos/trading.py` 的模块头早就写死了
# 「0DTE 侧的 order_journal 一律只认 ttt-，sos- 订单对它们天然不可见」——但读模型
# 从来没实现过这条，TradingStream 推什么就收什么。

def _sos_take_profit_order(now: datetime) -> dict:
    """SOS 挂出的止盈单在券商快照里长这样（sos- 前缀 + 未成交卖单）。"""
    return {
        "id": "sos-tp-order",
        "symbol": "SLV260919C00052500",
        "side": "sell",
        "qty": "1",
        "status": "new",
        "client_order_id": "sos-3f2a1b4c-0000-4000-8000-000000000001",
        "submitted_at": now.isoformat(),
        "updated_at": now.isoformat(),
    }


def test_兄弟系统的sos单不进0DTE订单流():
    """真实故障：SOS 的止盈单出现在 0DTE 订单面板的「未成交」组里。

    不只是脏——那一组每行都带撤单按钮，等于把另一个系统的止盈保护摆在手边一键可撤。
    """
    now = datetime.now(timezone.utc)
    j = _j()

    j.upsert_broker_orders([_sos_take_profit_order(now)])

    assert j.get("sos-tp-order") is None
    assert j.current_session(now)["pending"] == []


def test_兄弟系统的sos在途单不进券商轮询跟踪集():
    """看不见还不够：进了 tracking_order_ids 就会被 watchdog_order_sync 拿去 REST 轮询。"""
    now = datetime.now(timezone.utc)
    j = _j()

    j.upsert_broker_orders([_sos_take_profit_order(now)])

    assert j.tracking_order_ids() == set()


def test_归属过滤只挡兄弟系统_不误伤手动单():
    """只认前缀挡 `sos-`，**不能**顺手把手动/外部单也滤掉。

    共用账户上看得见手动单是有意的（前端有「外部程序」标签、`external_closed` 理由），
    过滤过头会让人在券商那边下的单在本面板凭空消失。
    """
    now = datetime.now(timezone.utc)
    j = _j()

    j.upsert_broker_orders([{
        "id": "manual-order",
        "symbol": "SLV260919C00052500",
        "side": "buy",
        "qty": "1",
        "status": "new",
        "submitted_at": now.isoformat(),
        "updated_at": now.isoformat(),
    }])

    assert [e["order_id"] for e in j.current_session(now)["pending"]] == ["manual-order"]
