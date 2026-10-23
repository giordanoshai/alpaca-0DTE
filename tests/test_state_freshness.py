# -*- coding: utf-8 -*-
"""盯盘状态的新鲜度闸：几十分钟前的 K 线不许当成现在用。

2026-08-28 现场：进程 21:44 崩溃、21:45:53 重启，磁盘缓存停在 09:27（美东，盘前），
`warmup()` 立刻返回、`start()` 拿这份旧数据算出第一份状态就对外发布了。当时表现是
**错误地拦住**（盘前成交量恒 0 → 流动性硬闸判不可交易，13 只票全灰）。

真正危险的是同一机制的反面：缓存停在盘中时流动性硬闸会说「可以交易」——那就变成
拿几十分钟前的 VWAP / 效率比 / 均线方向去放行一笔真单。实测当天重启后的缓存
（最新一根 09:45）跑 `data_quality_ok` 返回 True，条件当场成立。

根因是发布出去的状态里**没有「这份数据是什么时候的」**，于是没有任何一层拦得住。
本文件锁死：状态自带落后秒数，落后就 fail-closed，且卡片文案说的是「数据落后」
而不是「暂不可交易」——后者会让人以为是行情不合格。
"""
from __future__ import annotations

from decimal import Decimal as D
from types import SimpleNamespace

import pandas as pd
import pytest

from app.datasource.watcher import WatcherDataSource
import app.services.feed_snapshot as feed_snapshot
from app.services.feed_snapshot import build_feed_snapshot
from app.services.market_context import signal_from_payload
from app.validator import AccountState
from app.watcher.watcher_manager import STATE_STALE_AFTER_S, TickerWatcher

ET = "America/New_York"


# ───────────────────────── 生产者：watcher.get_state ─────────────────────────

def _frame(end: str, periods: int = 60) -> pd.DataFrame:
    idx = pd.date_range(end=pd.Timestamp(end, tz=ET), periods=periods, freq="1min",
                        name="datetime")
    return pd.DataFrame({
        "open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0, "volume": 1000,
    }, index=idx)


class _StubDataManager:
    def __init__(self, df: pd.DataFrame):
        self._df = df
        self.bar_count = len(df)

    def get_df_1m(self, tail=None, current_bar=None) -> pd.DataFrame:
        df = self._df.copy()
        return df if tail is None or len(df) <= tail else df.tail(tail)

    def get_df_5m(self, tail=None, current_bar=None) -> pd.DataFrame:
        return pd.DataFrame()


class _StubClock:
    def __init__(self, now: str):
        self._now = pd.Timestamp(now, tz=ET)

    def get_ntp_time(self):
        return self._now


class _StubBarBuilder:
    def get_current_bar(self):
        return {"last_price": 100.0}


def _watcher(*, now: str, last_bar: str) -> TickerWatcher:
    w = object.__new__(TickerWatcher)
    w.ticker = "SPY"
    w.data_manager = _StubDataManager(_frame(last_bar))
    w.time_sync = _StubClock(now)
    w.bar_builder = _StubBarBuilder()
    w._minute_state = {}
    w._running = True
    w._signal_engines = []
    w._last_result = None
    w._recalculate_minute_state()
    return w


def test_数据落后二十分钟判为陈旧():
    """崩溃重启现场：缓存最新一根 09:27，现在 09:46。"""
    w = _watcher(now="2026-08-28 09:46", last_bar="2026-08-28 09:27")
    state = w.get_state()

    assert state["data_stale"] is True
    assert state["data_age_s"] == pytest.approx(19 * 60, abs=1)


def test_刚收盘的bar不算陈旧():
    """K 线时间戳是**起始时间**，正常年龄本来就在 60~120 秒之间，不许误伤。"""
    w = _watcher(now="2026-08-28 09:47:30", last_bar="2026-08-28 09:46")
    assert w.get_state()["data_stale"] is False


def test_阈值留足一根bar的余量():
    """阈值必须大于两根 bar 的间隔，否则每分钟末尾都会闪一次「数据落后」。"""
    assert STATE_STALE_AFTER_S > 120


def test_从未算过状态判为陈旧():
    """预热没跑完就被读取 —— 没有数据就不是「数据没问题」。"""
    w = _watcher(now="2026-08-28 09:46", last_bar="2026-08-28 09:45")
    w._minute_state = {}
    assert w.get_state()["data_stale"] is True


def test_mock回放不判新鲜度(monkeypatch):
    """回放的钟与真实钟不同源，拿真钟去判回放数据会把 mock 联调全部拦死。"""
    monkeypatch.setenv("DATA_SOURCE", "mock")
    w = _watcher(now="2026-08-28 09:46", last_bar="2026-08-28 09:27")
    assert w.get_state()["data_stale"] is False


# ───────────────────────── 消费者：下单硬闸 ─────────────────────────

def _state(*, stale=False, age=None, price=500.0):
    return {
        "data_stale": stale,
        "data_age_s": age,
        "trend_score": {"data_quality_ok": True},
        "ema_dir": {"m1": "up", "m5": "up"},
        "current_bar": {"last_price": price, "close": price},
        "vwap": 500.0, "vwap_sigma": 1.0, "atr": 1.0, "er": 0.5,
    }


def test_下单硬闸拦住陈旧数据():
    """落后多久要说人话：「19 分钟」用户当场就知道该等还是该重启。"""
    with pytest.raises(RuntimeError, match="19 分钟"):
        signal_from_payload(
            {"kind": "watcher", "state": _state(stale=True, age=1140)},
            require_tradable=True)


def test_只读快照不受新鲜度影响():
    """卡片仍要显示价格/效率比 —— 拦的是下单，不是显示。"""
    sig = signal_from_payload(
        {"kind": "watcher", "state": _state(stale=True, age=1140)},
        require_tradable=False)
    assert sig.price == D("500.0")


def test_缺键时不误锁():
    """老快照/手工构造的 payload 没有该键 —— 按不陈旧处理，与 tradable 的兜底同一条约定。"""
    payload = _state()
    payload.pop("data_stale")
    assert signal_from_payload({"kind": "watcher", "state": payload},
                               require_tradable=True) is not None


# ───────────────────────── 消费者：卡片 ─────────────────────────

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


def _src(states):
    s = WatcherDataSource(_FakeWatcher(states))
    s.alpaca = SimpleNamespace(clock=lambda: SimpleNamespace(minutes_since_open=D("60")))
    s._account_state = AccountState(
        daily_pnl=D("0"), weekly_pnl=D("0"), consecutive_losses=0, cooldown_remaining_s=0,
        minutes_since_open=D("60"), buying_power=D("100000"))
    s._account_error = None
    return s


@pytest.fixture
def _stub_account_state(monkeypatch):
    monkeypatch.setattr(feed_snapshot, "build_account_state",
                        lambda source, _m: source._account_state)


def test_卡片报数据落后而不是暂不可交易(_stub_account_state):
    snap = build_feed_snapshot(_src({"QQQ": _state(stale=True, age=1140)}))
    gate = next(t for t in snap["ticks"] if t["sym"] == "QQQ")["gates"]["long"]

    assert gate["blocked"] is True
    # 独立 rule 名：拦截日志按 rule 分类统计，混进 watcher/chop 会污染历史口径
    assert gate["rule"] == "stale"
    assert "落后 19 分钟" in gate["reason"]


def test_落后不到两分钟的文案按秒说(_stub_account_state):
    snap = build_feed_snapshot(_src({"QQQ": _state(stale=True, age=95)}))
    gate = next(t for t in snap["ticks"] if t["sym"] == "QQQ")["gates"]["long"]
    assert "落后 95 秒" in gate["reason"]


def test_卡片可交易标记与按钮一致(_stub_account_state):
    """铁律：按钮灰的时候卡片不能写「可买入」。"""
    snap = build_feed_snapshot(_src({"QQQ": _state(stale=True, age=1140)}))
    row = next(p for p in snap["pool"] if p["sym"] == "QQQ")

    assert row["data_quality_ok"] is False
    assert row["tradable"] is False
