"""行情数据窗口的判断（app/core/market_gate.py）。

窗口 = 开盘前 5 分钟 ~ 收盘后 5 分钟。主判据是券商时钟（认节假日与半日市），
拿不到时降级回纯时钟，且降级方向是**放行**——宁可多拉一会儿数据，也不能因为
券商接口抽风把整个盯盘停掉。
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from app.core import market_gate
from app.integrations.alpaca.data import ClockInfo

ET = ZoneInfo("America/New_York")


def et(y, m, d, hh, mm) -> dt.datetime:
    return dt.datetime(y, m, d, hh, mm, tzinfo=ET)


# 2026-09-02 是周三，09-05 周六，09-07 周一
WED = (2026, 9, 2)
SAT = (2026, 9, 5)


@pytest.fixture(autouse=True)
def _clean_gate():
    market_gate.reset_cache()
    yield
    market_gate.reset_cache()


def stub_clock(monkeypatch, *, is_open: bool, session_date: dt.date):
    def fake():
        return ClockInfo(is_open, Decimal("0"), session_date)
    monkeypatch.setattr(market_gate, "_fetch_clock", fake)


def stub_clock_broken(monkeypatch):
    def fake():
        raise RuntimeError("Alpaca 挂了")
    monkeypatch.setattr(market_gate, "_fetch_clock", fake)


# ── 券商说开着 ──────────────────────────────────────────────────────────────

def test_broker_says_open(monkeypatch):
    stub_clock(monkeypatch, is_open=True, session_date=dt.date(*WED))
    assert market_gate.data_window_open(et(*WED, 12, 0)) is True


# ── 盘前预热：券商还没开，但下一场就是今天 ──────────────────────────────────

def test_pre_open_window_opens_five_minutes_early(monkeypatch):
    stub_clock(monkeypatch, is_open=False, session_date=dt.date(*WED))
    assert market_gate.data_window_open(et(*WED, 9, 25)) is True


def test_before_pre_open_window_stays_closed(monkeypatch):
    stub_clock(monkeypatch, is_open=False, session_date=dt.date(*WED))
    assert market_gate.data_window_open(et(*WED, 9, 24)) is False


def test_early_morning_stays_closed(monkeypatch):
    stub_clock(monkeypatch, is_open=False, session_date=dt.date(*WED))
    assert market_gate.data_window_open(et(*WED, 4, 0)) is False


# ── 收盘缓冲：靠进程内记住的「最后一次开着」 ────────────────────────────────

def test_post_close_buffer_keeps_window_open(monkeypatch):
    stub_clock(monkeypatch, is_open=True, session_date=dt.date(*WED))
    assert market_gate.data_window_open(et(*WED, 15, 59)) is True  # 记下 last_open

    stub_clock(monkeypatch, is_open=False, session_date=dt.date(2026, 9, 3))
    market_gate.reset_cache(keep_last_open=True)
    assert market_gate.data_window_open(et(*WED, 16, 2)) is True


def test_post_close_buffer_expires(monkeypatch):
    stub_clock(monkeypatch, is_open=True, session_date=dt.date(*WED))
    market_gate.data_window_open(et(*WED, 15, 59))

    stub_clock(monkeypatch, is_open=False, session_date=dt.date(2026, 9, 3))
    market_gate.reset_cache(keep_last_open=True)
    assert market_gate.data_window_open(et(*WED, 16, 10)) is False


def test_post_close_without_remembered_open_is_closed(monkeypatch):
    """进程刚重启、没见过开盘 → 没有缓冲。少拉几分钟，无害。"""
    stub_clock(monkeypatch, is_open=False, session_date=dt.date(2026, 9, 3))
    assert market_gate.data_window_open(et(*WED, 16, 2)) is False


# ── 节假日 / 周末：下一场不是今天 ───────────────────────────────────────────

def test_weekend_closed(monkeypatch):
    stub_clock(monkeypatch, is_open=False, session_date=dt.date(2026, 9, 7))
    assert market_gate.data_window_open(et(*SAT, 12, 0)) is False


def test_holiday_closed_even_during_regular_hours(monkeypatch):
    """节假日：纯时钟会判「周三中午=开」，券商时钟能纠正过来。"""
    stub_clock(monkeypatch, is_open=False, session_date=dt.date(2026, 9, 3))
    assert market_gate.data_window_open(et(*WED, 12, 0)) is False


# ── 券商不可用时降级回纯时钟 ────────────────────────────────────────────────

def test_fallback_open_during_weekday_hours(monkeypatch):
    stub_clock_broken(monkeypatch)
    assert market_gate.data_window_open(et(*WED, 12, 0)) is True


def test_fallback_respects_window_edges(monkeypatch):
    stub_clock_broken(monkeypatch)
    assert market_gate.data_window_open(et(*WED, 9, 24)) is False
    market_gate.reset_cache()
    assert market_gate.data_window_open(et(*WED, 9, 25)) is True
    market_gate.reset_cache()
    assert market_gate.data_window_open(et(*WED, 16, 4)) is True
    market_gate.reset_cache()
    assert market_gate.data_window_open(et(*WED, 16, 5)) is False


def test_fallback_closed_on_weekend(monkeypatch):
    stub_clock_broken(monkeypatch)
    assert market_gate.data_window_open(et(*SAT, 12, 0)) is False


# ── 缓存：多个循环一起调，不该把券商接口打爆 ────────────────────────────────

def test_clock_is_cached_across_calls(monkeypatch):
    calls = []

    def fake():
        calls.append(1)
        return ClockInfo(True, Decimal("0"), dt.date(*WED))

    monkeypatch.setattr(market_gate, "_fetch_clock", fake)
    for _ in range(10):
        market_gate.data_window_open(et(*WED, 12, 0))
    assert len(calls) == 1


def test_cache_expires_after_ttl(monkeypatch):
    calls = []

    def fake():
        calls.append(1)
        return ClockInfo(True, Decimal("0"), dt.date(*WED))

    monkeypatch.setattr(market_gate, "_fetch_clock", fake)
    market_gate.data_window_open(et(*WED, 12, 0))
    # 缓存按真实时间过期，直接把时间戳往前拨
    market_gate._STATE.cached_at -= market_gate.CLOCK_CACHE_TTL_S + 1
    market_gate.data_window_open(et(*WED, 12, 0))
    assert len(calls) == 2


# ── 给前端的状态字符串 ──────────────────────────────────────────────────────

def test_session_state_strings(monkeypatch):
    stub_clock(monkeypatch, is_open=True, session_date=dt.date(*WED))
    assert market_gate.session_state(et(*WED, 12, 0)) == "open"

    market_gate.reset_cache()
    stub_clock(monkeypatch, is_open=False, session_date=dt.date(2026, 9, 7))
    assert market_gate.session_state(et(*SAT, 12, 0)) == "closed"
