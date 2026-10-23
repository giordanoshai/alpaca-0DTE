# -*- coding: utf-8 -*-
"""feed_snapshot 每票带**真实拦截原因**（审查 2.5/2.6）。

用户 2026-07-24：「禁止下单的 Ticker，现在只会显示盯盘方向不明确…如果是其他原因导致的，
这个地方也要展示。比如说跟 VWAP 的倍数太高了，期权跟墙太近了。」
2026-07-25 又提：「只有当盯盘是震荡盘的时候买入按钮才是灰的，其他条件导致不能入场的话，
点了才会被 toast 拦截。」

根因有两层，这里修后端这层：`pool` 里只有 `{sym, tradable, direction}`，
追高 σ / 距墙 / 午后禁入 / 次数用尽 / 熔断这些**根本没进这一帧**，前端无从展示。
"""
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from app.datasource.watcher import WatcherDataSource
import app.services.feed_snapshot as feed_snapshot
from app.services.feed_snapshot import build_feed_snapshot
from app.validator import AccountState


def _state(direction="up", tradable=True, price=500.0, vwap=500.0, sigma=1.0, atr=1.0,
           primary_direction=None, primary_score=80.0, impulse_score=80.0,
           primary_stage=None, trend_direction=None, trendiness=0.8, er=0.5):
    tdir = direction if trend_direction is None else trend_direction
    return {
        # 规则 6 方向闸判据。跟着 tdir 走，让本文件继续只测「拦截理由」本身。
        "ema_dir": {"m1": tdir, "m5": tdir},
        "trend_score": {
            "score": impulse_score,
            "direction": direction,
            "tradable": tradable,
            "primary_direction": primary_direction or direction,
            "primary_score": primary_score,
            "primary_stage": primary_stage or direction,
            "impulse_score": impulse_score,
            "data_quality_ok": tradable,
            "phase": "advancing",
            "trend_direction": tdir,
            "trend_value": primary_score / 100.0,
            "trend_display": primary_score,
            "trendiness": trendiness,
        },
        "current_bar": {"last_price": price, "close": price},
        # er：2026-08-21 起是规则 5 的判据。默认 0.5 > 阈值 0.30 = 不判震荡。
        "vwap": vwap, "atr": atr, "er": er, "vwap_sigma": sigma,
    }


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


def _src(states, account=None, mins=D("60")):
    s = WatcherDataSource(_FakeWatcher(states))
    s.alpaca = SimpleNamespace(clock=lambda: SimpleNamespace(minutes_since_open=mins))
    s._account_state = account or AccountState(
        daily_pnl=D("0"), weekly_pnl=D("0"), consecutive_losses=0, cooldown_remaining_s=0,
        minutes_since_open=mins, buying_power=D("100000"))
    s._account_error = None
    return s


@pytest.fixture(autouse=True)
def _stub_account_state(monkeypatch):
    def build(source, _minutes):
        if source._account_error is not None:
            raise source._account_error
        return source._account_state

    monkeypatch.setattr(feed_snapshot, "build_account_state", build)


def _tick(snap, sym):
    return next(t for t in snap["ticks"] if t["sym"] == sym)


def test_tradable_ticker_reports_not_blocked():
    snap = build_feed_snapshot(_src({"QQQ": _state()}))
    g = _tick(snap, "QQQ")["gates"]["long"]
    assert g["blocked"] is False


def test_震荡不再拦单():
    """规则 5 于 2026-09-01 删除：效率比再低也不拦。"""
    snap = build_feed_snapshot(
        _src({"QQQ": _state(er=0.05)}))
    g = _tick(snap, "QQQ")["gates"]["long"]
    assert g["blocked"] is False


def test_chop_near_confirmation_no_longer_blocked():
    """已确立方向 Up 状态放行。"""
    snap = build_feed_snapshot(
        _src({"QQQ": _state(trend_direction="up", primary_score=69.7)}))
    g = _tick(snap, "QQQ")["gates"]["long"]
    assert g["blocked"] is False


def test_chop_below_enter_no_longer_blocked():
    """Up 状态放行。"""
    snap = build_feed_snapshot(
        _src({"QQQ": _state(trend_direction="up", primary_score=46.7)}))
    assert _tick(snap, "QQQ")["gates"]["long"]["blocked"] is False


# 2026-08-26 趋势分下线：原 `test_tick_trend_is_trend_display` 删除。
# 它断言 watcher 档 tick 里的 `trend` 是 trend_display 口径，而那个键连同趋势分
# 整体删除，tick 现在根本不发 `trend`（卡片那一格改显效率比，唯一来源是 pool 的
# `er`）。意图承接方：tests/test_pool_meta_gate_source.py::test_下发效率比给卡片那一格。


def test_距VWAP再远也不再拦():
    """规则 7 追高于 2026-09-01 删除：+3σ 两侧都放行。"""
    snap = build_feed_snapshot(
        _src({"QQQ": _state(price=503.0, vwap=500.0, sigma=1.0)})
    )
    gates = _tick(snap, "QQQ")["gates"]
    assert gates["long"]["blocked"] is False
    assert gates["short"]["blocked"] is False


def test_afternoon_cutoff_reported():
    """午后禁入（默认 150 分钟=12:00 ET）也要在卡片上看得见。"""
    snap = build_feed_snapshot(_src({"QQQ": _state()}, mins=D("200")))
    g = _tick(snap, "QQQ")["gates"]["long"]
    assert g["blocked"] is True and g["rule"] == "afternoon"


def test_breaker_reported():
    acc = AccountState(daily_pnl=D("-999"), weekly_pnl=D("0"), consecutive_losses=0,
                       cooldown_remaining_s=0, minutes_since_open=D("60"),
                       buying_power=D("100000"))
    g = _tick(
        build_feed_snapshot(_src({"QQQ": _state()}, account=acc)),
        "QQQ",
    )["gates"]["long"]
    assert g["blocked"] is True and g["rule"] == "breaker"


def test_untradable_watcher_state_reported():
    """trend_score 说不可交易 → 如实报盯盘层面的原因（保留既有语义）。"""
    g = _tick(
        build_feed_snapshot(_src({"QQQ": _state(tradable=False)})),
        "QQQ",
    )["gates"]["long"]
    assert g["blocked"] is True and g["rule"] == "watcher"


def test_account_failure_degrades_without_breaking_frame():
    """账户取数失败绝不能打断 1s 心跳帧 —— 退化为不给 gate，其余字段照常。"""
    src = _src({"QQQ": _state()})

    src._account_error = RuntimeError("券商查余额超时")
    snap = build_feed_snapshot(src)
    t = _tick(snap, "QQQ")
    assert t["sym"] == "QQQ"
    # 「其余字段照常」的证据。2026-08-26 `trend` 随趋势分下线、2026-09-01 `er`
    # 随震荡闸下线，换成 `vwap` —— 它是卡片那一格现在唯一的数据源。
    assert "vwap" in t
    assert t.get("gates") is None


def test_pool_meta只剩数据质量():
    """2026-09-01：方向标签与效率比随规则 5、6 一并从卡片撤掉。

    这条同时守住「别把方向判断偷偷加回卡片」—— 卡片一旦又说得出方向，
    用户就会把它当系统建议，而系统现在对方向不做任何判断。
    """
    from app.services.feed_snapshot import _pool_meta

    meta = _pool_meta({"trend_score": {"data_quality_ok": True}})

    assert set(meta) == {"tradable", "data_quality_ok"}
    assert meta["data_quality_ok"] is True


def test_pool_meta_defaults_when_fields_missing():
    from app.services.feed_snapshot import _pool_meta

    meta = _pool_meta({"trend_score": {}})

    assert meta["data_quality_ok"] is False


def test_pool_entries_still_present():
    """既有 pool 字段不能动（前端红绿灯仍靠它）。"""
    snap = build_feed_snapshot(_src({"QQQ": _state()}))
    assert snap["pool"][0]["sym"] == "QQQ"
    assert snap["pool"][0]["tradable"] is True
