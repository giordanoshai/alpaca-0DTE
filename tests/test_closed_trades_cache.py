"""成交历史本地缓存：gate 不再每次打 DB；平仓失效后即时重读（连亏冷却即时生效）。"""
from datetime import datetime, timezone
from decimal import Decimal as D

import app.services.account_state as account_state
from app.risk import ClosedTrade

_ROWS = [(datetime(2026, 7, 19, 20, 0, tzinfo=timezone.utc), D("50"))]


class _CountingCursor:
    def __init__(self, counter):
        self.counter = counter

    def execute(self, sql, *a):
        self.counter["n"] += 1

    def fetchall(self):
        return list(_ROWS)

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _CountingConn:
    def __init__(self, counter):
        self._cur = _CountingCursor(counter)

    def cursor(self):
        return self._cur

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_second_call_served_from_cache(monkeypatch):
    counter = {"n": 0}
    monkeypatch.setattr("app.db.connect", lambda: _CountingConn(counter))
    account_state.invalidate_closed_trades_cache()
    a = account_state.closed_trades()
    b = account_state.closed_trades()
    assert counter["n"] == 1                 # 第二次读缓存，不再打 DB
    assert a == b == [ClosedTrade(exit_time=_ROWS[0][0], pnl=D("50"))]


def test_invalidate_forces_reread(monkeypatch):
    counter = {"n": 0}
    monkeypatch.setattr("app.db.connect", lambda: _CountingConn(counter))
    account_state.invalidate_closed_trades_cache()
    account_state.closed_trades()
    account_state.invalidate_closed_trades_cache()       # 平仓落库后失效
    account_state.closed_trades()
    assert counter["n"] == 2                   # 失效后重新打 DB
