"""⑧ 交易记录器 best-effort 落库 单测（mock db.connect，不触真库）。"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal as D

import pytest

from app import trade_recorder as tr
from app.order_state import ManagedPosition, order_session_window


class FakeCursor:
    def __init__(self, fetch=None):
        self.calls: list[tuple] = []   # [(sql, params), ...]
        self._fetch = fetch
        self.description = []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))

    def fetchone(self):
        return self._fetch

    def fetchall(self):
        return []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor
        self.committed = False
        self.closed = False

    def cursor(self):
        return self._cursor

    def commit(self):
        self.committed = True

    def close(self):
        self.closed = True


@pytest.fixture
def cur():
    return FakeCursor()


@pytest.fixture
def wire(monkeypatch, cur):
    conn = FakeConn(cur)
    monkeypatch.setattr("app.db.connect", lambda: conn)
    return conn


def _pos():
    return ManagedPosition(
        option_code="QQQ260719C00500000", ticker="QQQ", direction="long",
        qty=2, entry_mid=D("2.00"), entry_time=datetime(2026, 7, 19, 14, 0, tzinfo=timezone.utc),
        order_id="entry-1", status="pending")


def test_load_order_events_uses_et_calendar_day_and_all_pending(wire, cur):
    rows = tr.load_order_events(
        order_session_window(
            datetime(2026, 7, 31, 18, 0, tzinfo=timezone.utc)
        )
    )
    sql, params = cur.calls[0]

    assert rows == []
    assert "status='pending'" in sql
    assert "submitted_at >= %s and submitted_at < %s" in sql
    assert params == (
        datetime(2026, 7, 31, 4, 0, tzinfo=timezone.utc),
        datetime(2026, 8, 1, 4, 0, tzinfo=timezone.utc),
        datetime(2026, 7, 31, 4, 0, tzinfo=timezone.utc),
        datetime(2026, 8, 1, 4, 0, tzinfo=timezone.utc),
    )


def test_load_order_events_weekend_still_uses_et_calendar_day(wire, cur):
    tr.load_order_events(
        order_session_window(
            datetime(2026, 8, 1, 16, 0, tzinfo=timezone.utc)
        )
    )
    sql, params = cur.calls[0]

    assert "status='pending'" in sql
    assert "submitted_at >= %s and submitted_at < %s" in sql
    assert params == (
        datetime(2026, 8, 1, 4, 0, tzinfo=timezone.utc),
        datetime(2026, 8, 2, 4, 0, tzinfo=timezone.utc),
        datetime(2026, 8, 1, 4, 0, tzinfo=timezone.utc),
        datetime(2026, 8, 2, 4, 0, tzinfo=timezone.utc),
    )


def test_record_submitted_upserts_pending(wire, cur):
    tr.record_submitted(_pos(), "watcher", {"name": "默认", "params": {}})
    sql, params = cur.calls[0]
    assert "insert into ttt_trades" in sql.lower()
    assert "on conflict (order_id) do nothing" in sql.lower()
    assert params[0] == "entry-1"          # order_id
    assert "pending" in sql.lower()        # status 字面量
    assert '"stop_mode": "percent"' in params[-1]
    assert wire.committed and wire.closed


def test_record_submitted_marks_mock_execution_source(monkeypatch, wire, cur):
    monkeypatch.setattr(
        tr,
        "env",
        lambda key, default: "mock" if key == "DATA_SOURCE" else default,
    )

    tr.record_submitted(_pos(), "watcher", {"name": "默认", "params": {}})

    _sql, params = cur.calls[0]
    assert params[-2] == "mock"


def test_record_filled_sets_open(wire, cur):
    tr.record_filled("entry-1")
    sql, params = cur.calls[0]
    assert "update ttt_trades set status='open'" in sql.lower().replace("  ", " ")
    assert "status='pending'" in sql.lower()   # 幂等：仅 pending→open
    assert params == ("entry-1",)


def test_record_cancelled_sets_cancelled(wire, cur):
    tr.record_cancelled("entry-1")
    sql, params = cur.calls[0]
    assert "status='cancelled'" in sql.lower()
    assert params == ("entry-1",)


def test_sell_fill_audit_keeps_sell_and_does_not_update_entry_table(monkeypatch):
    events = []
    monkeypatch.setattr(tr, "audit", lambda event, **fields: events.append((event, fields)))
    monkeypatch.setattr(tr, "_write", lambda *_: (_ for _ in ()).throw(
        AssertionError("sell order id must not update ttt_trades")))

    tr.record_filled("close-1", entry_mid=D("2.50"), qty=2, side="sell")
    tr.record_cancelled("close-2", side="sell")

    assert events[0][1]["side"] == "sell"
    assert events[0][1]["status"] == "filled"
    assert events[1][1]["side"] == "sell"


def test_record_add_updates_qty_and_entry_mid(wire, cur):
    tr.record_add("entry-1", 5, D("2.20"))
    sql, params = cur.calls[0]
    assert "update ttt_trades set qty=%s" in sql.lower()
    assert params == (5, D("2.20"), "entry-1")


def test_record_position_sync_updates_open_qty_without_guessing_price(wire, cur):
    tr.record_position_sync("entry-1", 5)
    sql, params = cur.calls[0]
    assert "set qty=%s" in sql.lower()
    assert "status='open'" in sql.lower()
    assert "entry_mid=%s" not in sql.lower()
    assert params == (5, "entry-1")


def test_best_effort_swallows_db_error(monkeypatch):
    def boom():
        raise RuntimeError("db down")
    monkeypatch.setattr("app.db.connect", boom)
    # 全部静默返回 None，不抛
    assert tr.record_submitted(_pos(), "watcher", None) is None
    assert tr.record_filled("x") is None
    assert tr.record_cancelled("x") is None
    assert tr.record_add("x", 1, D("1")) is None
    assert tr.record_position_sync("x", 1) is None


def test_record_closed_partial_accumulates_realized(monkeypatch):
    # 现有行：entry_mid 2.00, qty 3, realized 0；部分平 1 张 @ exit 2.50
    cur = FakeCursor(fetch=(D("2.00"), 3, D("0")))
    monkeypatch.setattr("app.db.connect", lambda: FakeConn(cur))
    tr.record_closed("entry-1", D("2.50"), 1, "手动平仓")
    # 首条 SELECT，次条 UPDATE
    upd_sql, upd_params = cur.calls[1]
    assert "update ttt_trades set qty=%s" in upd_sql.lower()
    assert "status='open'" in upd_sql.lower()
    # realized += (2.50-2.00)*100*1 = 50；剩余 qty = 2
    assert upd_params[0] == 2                 # new_qty
    assert upd_params[1] == D("50")           # new_realized


def test_record_closed_full_settles_total_pnl(monkeypatch):
    cur = FakeCursor(fetch=(D("2.00"), 3, D("0")))
    monkeypatch.setattr("app.db.connect", lambda: FakeConn(cur))
    tr.record_closed("entry-1", D("2.50"), 3, "watchdog:stop")
    upd_sql, upd_params = cur.calls[1]
    assert "status=%s" in upd_sql.lower()
    assert "total_pnl=%s" in upd_sql.lower()
    assert "exit_time=now()" in upd_sql.lower()
    # (2.50-2.00)*100*3 = 150
    assert D("150") in upd_params
    assert "closed" in upd_params             # 非 external → closed


def test_record_closed_external_no_exit_mid(monkeypatch):
    cur = FakeCursor(fetch=(D("2.00"), 2, D("0")))
    monkeypatch.setattr("app.db.connect", lambda: FakeConn(cur))
    tr.record_closed("entry-1", None, 2, "external_closed", external=True)
    upd_sql, upd_params = cur.calls[1]
    assert "external_closed" in upd_params    # status
    assert D("0") in upd_params               # total_pnl = realized（无 exit 加 0）


def test_record_closed_missing_row_silent(monkeypatch):
    cur = FakeCursor(fetch=None)              # 入场行没写成
    monkeypatch.setattr("app.db.connect", lambda: FakeConn(cur))
    assert tr.record_closed("nope", D("2.5"), 1, "x") is None
    assert len(cur.calls) == 1               # 只 SELECT，无 UPDATE


def test_market_snapshot_exports_signal_and_account():
    from app.order_gate import MarketContext
    from app.validator import AccountState, Contract, TickerSignal
    ctx = MarketContext(
        signal=TickerSignal(price=D("100"),
                            vwap=D("99.9"), atr=D("1.0")),
        account=AccountState(daily_pnl=D("-10"), weekly_pnl=D("-20"),
                             consecutive_losses=1, cooldown_remaining_s=0,
                             minutes_since_open=D("60"), buying_power=D("1000")),
        contract=Contract(delta=D("0.45"), spread_pct=D("0.02"), mid=D("2.00")))
    snap = tr.market_snapshot(ctx)
    # 2026-09-01: er / ema_dir_1m / ema_dir_5m 随规则 5、规则 6 一并删除，
    # 快照只剩「被拦当时行情什么样」这三个纯记录字段。
    assert set(snap["signal"]) == {"price", "vwap", "atr"}
    assert snap["signal"]["price"] == "100"          # Decimal→str
    assert snap["account"]["daily_pnl"] == "-10"
    assert snap["account"]["consecutive_losses"] == 1


def test_market_snapshot_broken_ctx_returns_none():
    # best-effort：坏 ctx（缺 signal/account）→ None，不抛（保证 gate_order 参数永不抛）
    class Broken:
        pass
    assert tr.market_snapshot(Broken()) is None


def test_template_snapshot_shape(monkeypatch):
    snap = tr.template_snapshot()                    # conftest 已隔离为播种默认
    assert set(snap) == {"name", "params"}
    assert snap["name"] == "测试基准"
    assert "stop_pct" in snap["params"]


def test_record_intercept_inserts_row(wire, cur):
    tr.record_intercept("whitelist", "不在白名单", "TSLA", "long", 1, "watcher",
                        market_snapshot={"signal": {}}, template_snapshot={"name": "默认"})
    sql, params = cur.calls[0]
    assert "insert into ttt_intercept_log" in sql.lower()
    assert "whitelist" in params
    assert "TSLA" in params
    assert wire.committed


def test_record_intercept_null_snapshots_ok(wire, cur):
    # 降级拒单：market_snapshot 可为 None
    tr.record_intercept("datasource", "数据源不可用", "QQQ", None, 0, "watcher",
                        market_snapshot=None, template_snapshot=None)
    assert cur.calls[0][0].lower().startswith("\n        insert into ttt_intercept_log") \
        or "insert into ttt_intercept_log" in cur.calls[0][0].lower()


def test_record_order_event_upserts_each_real_order_id(wire, cur):
    tr.record_order_event({
        "order_id": "sell-1",
        "parent_order_id": "entry-1",
        "client_order_id": "ttt-01234567-89ab-cdef-0123-456789abcdef",
        "ticker": "QQQ",
        "option_code": "QQQ260727C00500000",
        "side": "sell",
        "qty": 1,
        "status": "filled",
        "origin": "user",
        "action": "reduce",
        "reason": "减半仓",
        "cancel_origin": None,
        "fail_reason": None,
        "broker_status": "filled",
        "price": "2.50",
        "price_type": "bid",
        "limit_price": "2.48",
        "ts": "2026-07-27T14:00:00+00:00",
        "updated_ts": "2026-07-27T14:00:01+00:00",
        "triggered_at": "2026-07-27T13:59:59.900000+00:00",
        "trigger_bid": "2.49",
        "trigger_ask": "2.51",
        "trigger_value": "249",
        "trigger_loss": "101",
        "stop_mode": "amount",
        "stop_threshold": "100",
        "entry_value": "350",
    })

    sql, params = cur.calls[0]
    assert "insert into ttt_order_events" in sql.lower()
    assert "on conflict (order_id) do update" in sql.lower()
    assert params[0] == "sell-1"
    assert params[1] == "entry-1"
    assert params[9] == "reduce"
    assert params[10] == "减半仓"
    assert "triggered_at" in sql.lower()
    assert params[19:] == (
        "2026-07-27T13:59:59.900000+00:00",
        "2.49", "2.51", "249", "101", "amount", "100", "350",
    )


def test_order_event_write_is_best_effort(monkeypatch):
    monkeypatch.setattr("app.db.connect", lambda: (_ for _ in ()).throw(RuntimeError("db down")))
    assert tr.record_order_event({
        "order_id": "x", "ticker": "QQQ", "option_code": "C",
        "side": "buy", "qty": 1, "status": "pending",
    }) is None


def test_exact_order_fills_recompute_lifecycle_summary(monkeypatch):
    rows = [
        ("buy", 2, D("1.10"), "手动开仓", datetime(2026, 7, 27, 14, 0, tzinfo=timezone.utc)),
        ("buy", 1, D("1.40"), "手动加仓", datetime(2026, 7, 27, 14, 1, tzinfo=timezone.utc)),
        ("sell", 1, D("1.50"), "手动减仓", datetime(2026, 7, 27, 14, 2, tzinfo=timezone.utc)),
        ("sell", 2, D("1.60"), "watchdog:超时 5分", datetime(2026, 7, 27, 14, 3, tzinfo=timezone.utc)),
    ]

    class RecomputeCursor(FakeCursor):
        def fetchall(self):
            return rows

    cur = RecomputeCursor()
    conn = FakeConn(cur)
    monkeypatch.setattr("app.db.connect", lambda: conn)

    tr._recompute_trade_from_order_events("entry-1")

    update_sql, params = cur.calls[1]
    assert "status='closed'" in update_sql.lower()
    assert params[0] == D("1.20")       # (2×1.10 + 1×1.40) / 3
    assert params[1] == D("110.00")     # (1×1.50 + 2×1.60 - 3×1.20) × 100
    assert params[2].quantize(D("0.0001")) == D("1.5667")
    assert params[4] == D("110.00")
    assert params[5] == "watchdog:超时 5分"
    assert conn.committed


def test_recompute_waits_when_any_fill_price_is_missing(monkeypatch):
    class IncompleteCursor(FakeCursor):
        def fetchall(self):
            return [
                ("buy", 1, D("1.10"), "手动开仓", datetime.now(timezone.utc)),
                ("sell", 1, None, "手动平仓", datetime.now(timezone.utc)),
            ]

    cur = IncompleteCursor()
    monkeypatch.setattr("app.db.connect", lambda: FakeConn(cur))

    tr._recompute_trade_from_order_events("entry-1")

    assert len(cur.calls) == 1  # 只读逐笔事件；不拿缺失价格覆盖汇总账


def test_record_lifecycle_emits_audit_events(monkeypatch):
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(tr, "_write", lambda *_: None)
    monkeypatch.setattr(tr, "audit", lambda event, **fields: events.append((event, fields)))
    # 本用例验证显式传入 watcher 的审计字段，不应受开发机 .env 的 mock 模式影响。
    monkeypatch.setattr(tr, "env", lambda _key, default: default)

    tr.record_submitted(_pos(), "watcher", {"name": "保守"})
    tr.record_filled("entry-1", entry_mid=D("2.05"), qty=2)
    tr.record_add("entry-1", 3, D("2.10"))
    tr.record_cancelled("entry-2")
    tr.record_intercept("cooldown", "连续亏损冷却", "QQQ", "long", 1, "watcher", None, None)

    assert [event for event, _ in events] == [
        "trade_order_submitted",
        "trade_order_filled",
        "trade_position_added",
        "trade_order_cancelled",
        "trade_gate_rejected",
    ]
    submitted = events[0][1]
    assert submitted["order_id"] == "entry-1"
    assert submitted["ticker"] == "QQQ"
    assert submitted["contract"] == "QQQ260719C00500000"
    assert submitted["source"] == "watcher"
    assert submitted["side"] == "buy"
    assert events[1][1]["fill_price"] == D("2.05")
    assert events[2][1]["total_qty"] == 3
    assert events[4][1]["rule"] == "cooldown"


def test_record_closed_audit_has_realized_pnl_and_remaining(monkeypatch):
    cur = FakeCursor(fetch=(D("2.00"), 3, D("10")))
    monkeypatch.setattr("app.db.connect", lambda: FakeConn(cur))
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(tr, "audit", lambda event, **fields: events.append((event, fields)))

    tr.record_closed("entry-1", D("2.50"), 1, "手动减仓")

    assert events == [(
        "trade_position_reduced",
        {
            "order_id": "entry-1",
            "side": "sell",
            "sold_qty": 1,
            "entry_price": D("2.00"),
            "exit_price": D("2.50"),
            "trade_pnl": D("50.00"),
            "realized_pnl": D("60.00"),
            "remaining_qty": 2,
            "status": "open",
            "reason": "手动减仓",
            "external": False,
        },
    )]
