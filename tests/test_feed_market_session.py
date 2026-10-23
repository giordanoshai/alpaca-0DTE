"""feed 快照要带市场时段，前端据此显示「休市」。

非盘中行情循环挂起，卡片会冻在收盘时的值 —— 不标出来的话，看的人分不清
自己盯的是实时价还是几小时前的残影（和 tide 徽章「已收盘」是同一个用意）。
"""
from __future__ import annotations

from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from app.core import market_gate
from app.datasource.watcher import WatcherDataSource
import app.services.feed_snapshot as feed_snapshot
from app.services.feed_snapshot import build_feed_snapshot
from app.validator import AccountState


class _FakeWatcher:
    def __init__(self, states):
        self._states = states
        self._running = True
        self._watchers = {k: object() for k in states}
        self.last_signal_ts = {}

    def get_all_states(self):
        return dict(self._states)

    def get_ticker_state(self, t):
        return self._states.get(t)


@pytest.fixture(autouse=True)
def _stub_account_state(monkeypatch):
    def build(source, _minutes):
        return source._account_state

    monkeypatch.setattr(feed_snapshot, "build_account_state", build)


def _src():
    s = WatcherDataSource(_FakeWatcher({"QQQ": {}}))
    s.alpaca = SimpleNamespace(
        clock=lambda: SimpleNamespace(minutes_since_open=D("60")))
    s._account_state = AccountState(
        daily_pnl=D("0"), weekly_pnl=D("0"), consecutive_losses=0,
        cooldown_remaining_s=0, minutes_since_open=D("60"),
        buying_power=D("100000"))
    s._account_error = None
    return s


def _stub_gate(monkeypatch, *, is_open: bool):
    class _Clock:
        pass

    _Clock.is_open = is_open
    # is_open=False 时 _decide 会看 session_date；给一个「下一场在明天」的日期，
    # 表示今天的盘已经结束（或今天压根不是交易日）。
    import datetime as dt
    _Clock.session_date = dt.date(2030, 1, 1)

    market_gate.reset_cache()
    monkeypatch.setattr(market_gate, "_fetch_clock", _Clock)


def test_snapshot_reports_open_session(monkeypatch):
    _stub_gate(monkeypatch, is_open=True)
    assert build_feed_snapshot(_src())["market_session"] == "open"


def test_snapshot_reports_closed_session(monkeypatch):
    _stub_gate(monkeypatch, is_open=False)
    assert build_feed_snapshot(_src())["market_session"] == "closed"
