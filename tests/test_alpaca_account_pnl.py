"""Alpaca 账户级今日/本周盈亏口径。"""
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from alpaca.common.exceptions import APIError

from app.integrations.alpaca.data import AlpacaData


class _Account:
    equity = "989784.18"
    last_equity = "988257.02"
    buying_power = "3959136.72"


class _History:
    base_value = "988257.02"


class _EmptyHistory:
    """本周尚无交易数据时 Alpaca 返回的形态（典型：周一盘前）。"""

    base_value = None


class _TradingClient:
    def __init__(self, history=None, history_error=None):
        self.history_request = None
        self.account_calls = 0
        self.history_calls = 0
        self._history = history or _History()
        self._history_error = history_error

    def get_account(self):
        self.account_calls += 1
        return _Account()

    def get_portfolio_history(self, request):
        self.history_calls += 1
        self.history_request = request
        if self._history_error is not None:
            raise self._history_error
        return self._history


def test_account_pnl_matches_alpaca_equity_and_monday_base(monkeypatch):
    client = _TradingClient()
    data = AlpacaData(cache_ttl=20)
    monkeypatch.setattr(data, "_tc", lambda: client)

    snap = data.account_pnl(datetime(2026, 7, 27, 14, 0, tzinfo=timezone.utc))

    assert snap.daily_pnl == Decimal("1527.16")
    assert snap.weekly_pnl == Decimal("1527.16")
    assert snap.buying_power == Decimal("3959136.72")
    assert snap.equity == Decimal("989784.18")
    assert snap.last_equity == Decimal("988257.02")
    assert snap.week_base_equity == Decimal("988257.02")
    assert snap.as_of.tzinfo is not None
    assert snap.as_of.isoformat() == "2026-07-27T14:00:00+00:00"
    assert client.history_request.start.isoformat() == "2026-07-27T00:00:00-04:00"
    assert client.history_request.timeframe == "1D"
    assert client.history_request.pnl_reset == "no_reset"


def test_portfolio_history_显式传入当前时间作为end():
    client = _TradingClient()
    data = AlpacaData(cache_ttl=20)
    monkeypatch_tc(data, client)

    as_of = datetime(2026, 8, 3, 10, 18, tzinfo=timezone.utc)   # 周一 ET 06:18 盘前
    data.account_pnl(as_of)

    assert client.history_request.end is not None
    assert client.history_request.end == as_of


def test_周一盘前日线区间尚不存在时以上一交易日权益为周基准():
    error = APIError(
        '{"code":400,"message":"start cannot be after end. '
        'start: 2026-08-03T00:00:00-04:00, '
        'end: 2026-07-31T09:30:00-04:00: bad request"}'
    )
    client = _TradingClient(history_error=error)
    data = AlpacaData(cache_ttl=20)
    monkeypatch_tc(data, client)

    as_of = datetime(2026, 8, 3, 10, 18, tzinfo=timezone.utc)
    snap = data.account_pnl(as_of)

    assert snap.week_base_equity == Decimal("988257.02")
    assert snap.weekly_pnl == Decimal("1527.16")
    assert snap.daily_pnl == Decimal("1527.16")
    assert data.account_pnl(as_of) == snap
    assert client.history_calls == 1


def test_portfolio_history_其他错误不能被周初兜底吞掉():
    error = APIError('{"code":500,"message":"service unavailable"}')
    client = _TradingClient(history_error=error)
    data = AlpacaData(cache_ttl=20)
    monkeypatch_tc(data, client)

    with pytest.raises(APIError, match="service unavailable"):
        data.account_pnl(datetime(2026, 8, 3, 10, 18, tzinfo=timezone.utc))


def test_本周无交易数据时以上一交易日收盘权益为基准():
    """周一盘前 base_value 为 None 是正常周初状态，不是故障：
    本周盈亏应从 0 起算，而不是抛异常打断整条 trading_state 刷新链路。
    """
    client = _TradingClient(history=_EmptyHistory())
    data = AlpacaData(cache_ttl=20)
    monkeypatch_tc(data, client)

    snap = data.account_pnl(datetime(2026, 8, 3, 10, 18, tzinfo=timezone.utc))

    assert snap.week_base_equity == Decimal("988257.02")   # = last_equity
    assert snap.weekly_pnl == Decimal("1527.16")           # equity - last_equity
    assert snap.daily_pnl == Decimal("1527.16")


def monkeypatch_tc(data, client):
    """直接替换实例方法，避免每个用例都要 monkeypatch fixture。"""
    data._tc = lambda: client


def test_account_pnl_is_cached(monkeypatch):
    client = _TradingClient()
    data = AlpacaData(cache_ttl=20)
    monkeypatch.setattr(data, "_tc", lambda: client)
    now = datetime(2026, 7, 27, 14, 0, tzinfo=timezone.utc)

    assert data.account_pnl(now) == data.account_pnl(now)
    assert client.account_calls == 1
    assert client.history_calls == 1
