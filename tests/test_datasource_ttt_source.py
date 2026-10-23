"""⑦ 切源：风控读 ttt_trades（status='closed'）+ 失安拒单。"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal as D

import pytest

from app.risk import ClosedTrade
from app.services import account_state


class FakeCursor:
    def __init__(self, rows):
        self.rows = rows
        self.sql = None

    def execute(self, sql, params=None):
        self.sql = sql

    def fetchall(self):
        return self.rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_fetch_closed_ttt_reads_ttt_trades(monkeypatch):
    rows = [(datetime(2026, 7, 19, 20, 0, tzinfo=timezone.utc), D("50")),
            (datetime(2026, 7, 19, 21, 0, tzinfo=timezone.utc), D("-30"))]
    cur = FakeCursor(rows)
    monkeypatch.setattr("app.db.connect", lambda: FakeConn(cur))
    out = account_state.closed_trades()
    assert "ttt_trades" in cur.sql
    assert "status='closed'" in cur.sql.replace(" ", "").lower() or "status = 'closed'" in cur.sql.lower()
    assert out == [ClosedTrade(exit_time=rows[0][0], pnl=D("50")),
                   ClosedTrade(exit_time=rows[1][0], pnl=D("-30"))]
    assert "coalesce(source, '') <> 'mock'" in " ".join(cur.sql.lower().split())


def test_trade_count_counts_only_successful_position_lifecycles(monkeypatch):
    class CountCursor(FakeCursor):
        def fetchone(self):
            return (13,)

    cur = CountCursor([])
    monkeypatch.setattr("app.db.connect", lambda: FakeConn(cur))

    count = account_state.count_trades_since(
        datetime(2026, 7, 27, 4, 0, tzinfo=timezone.utc),
    )

    sql = " ".join(cur.sql.lower().split())
    assert count == 13
    assert "count(*)" in sql
    assert "status in ('open', 'closed', 'external_closed')" in sql
    assert "coalesce(source, '') <> 'mock'" in sql


def test_account_read_failure_propagates(monkeypatch):
    def boom():
        raise RuntimeError("db down")
    monkeypatch.setattr("app.db.connect", boom)
    with pytest.raises(RuntimeError):
        account_state.closed_trades()


def test_gate_order_failsafe_rejects_on_db_error(monkeypatch):
    """失安：market_context 读 ttt_trades 抛 → gate_order 返回 rule='datasource'。"""
    from app.order_gate import gate_order

    class RaisingSource:
        name = "live"

    monkeypatch.setattr("app.trade_recorder.record_intercept", lambda **k: None)
    monkeypatch.setattr(
        "app.order_gate.build_market_context",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("ttt_trades 读失败")
        ),
    )
    out = gate_order(RaisingSource(), "QQQ", "long", 1)
    assert out["allowed"] is False
    assert out["rule"] == "datasource"
