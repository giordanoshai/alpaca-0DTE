"""今日交易次数以本程序仓位生命周期计数，不以买入订单数计数。"""
from datetime import datetime, timezone

from app.services import account_state


def test_trade_count_uses_new_york_day_start_and_db_lifecycle_count(monkeypatch):
    starts = []
    monkeypatch.setattr(
        "app.services.account_state.count_trades_since",
        lambda start: starts.append(start) or 13,
    )
    count = account_state.trade_count_today(
        datetime(2026, 7, 27, 15, 30, tzinfo=timezone.utc),
    )

    assert count == 13
    assert starts[0].isoformat() == "2026-07-27T04:00:00+00:00"
