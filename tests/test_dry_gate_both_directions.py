# -*- coding: utf-8 -*-
"""干跑闸门必须 CALL/PUT 各跑一次，且**只报 validator 真会拦的那些**。

历史病根（趋势分降级第二期）：`feed_snapshot._dry_gates` 曾在调用 validator
**之前**自带一份方向拦截（`primary_direction not in ("up","down") → rule=chop`），
于是闸门明明放行、卡片照旧显示「横盘震荡」、买入按钮是灰的 —— 后端的放宽用户
完全感受不到。镜像版事故是 2026-08-03 BMNR：卡片写「当前不可入场」却能一键买入
成交。卡片与真实闸门必须同源，两个方向都要如实报。

2026-09-01：震荡、追高、方向闸、动能闸四条删除后，validate_market 里没有一条规则
读方向，所以**两侧结论恒等**。两侧各跑一遍的结构保留不动（方向相关性 2026-08-14
到 08-21 之间就来回过一次）。
"""
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from app.datasource.watcher import WatcherDataSource
import app.services.feed_snapshot as feed_snapshot
from app.services.feed_snapshot import build_feed_snapshot
from app.validator import AccountState


def _state(*, trend_direction="up", primary_score=80.0, tradable=True,
           price=500.0, vwap=500.0, sigma=1.0, atr=1.0, trendiness=0.8,
           ema_1m="up", ema_5m="up", er=0.5):
    return {
        # 规则 6 方向闸(2026-08-21 复位)判据。默认两周期都向上 = 只准买 CALL。
        "ema_dir": {"m1": ema_1m, "m5": ema_5m},
        "trend_score": {
            "score": primary_score,
            "direction": trend_direction,
            "tradable": tradable,
            "primary_direction": trend_direction,
            "primary_score": primary_score,
            "primary_stage": trend_direction,
            "impulse_score": primary_score,
            "data_quality_ok": tradable,
            "phase": "advancing",
            "trend_direction": trend_direction,
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


def _src(states, mins=D("60")):
    s = WatcherDataSource(_FakeWatcher(states))
    s.alpaca = SimpleNamespace(clock=lambda: SimpleNamespace(minutes_since_open=mins))
    s._account_state = AccountState(
        daily_pnl=D("0"), weekly_pnl=D("0"), consecutive_losses=0,
        cooldown_remaining_s=0, minutes_since_open=mins, buying_power=D("100000"))
    s._account_error = None
    return s


@pytest.fixture(autouse=True)
def _stub_account_state(monkeypatch):
    monkeypatch.setattr(
        feed_snapshot, "build_account_state",
        lambda source, _minutes: source._account_state,
    )


def _gates(states, **kwargs):
    snap = build_feed_snapshot(_src(states, **kwargs))
    return next(t for t in snap["ticks"] if t["sym"] == "QQQ")["gates"]


def test_方向不影响放行():
    """闸门不看方向：同一行情 CALL / PUT 必须同放行。

    这条一旦红了，说明有人往 validator 里加回了与方向相关的判据 —— 卡片会重新
    出现「一边亮一边灰」，而系统现在对方向不做任何判断。
    """
    for kw in ({"ema_1m": "up", "ema_5m": "up"},
               {"ema_1m": "down", "ema_5m": "down"},
               {"ema_1m": "down", "ema_5m": "up"}):
        gates = _gates({"QQQ": _state(**kw)})
        assert gates["long"]["blocked"] is False, (kw, gates)
        assert gates["short"]["blocked"] is False, (kw, gates)


def test_均线数据缺失也不再拦():
    """规则 6 删除：老快照没有 ema_dir 键照样能买。"""
    state = _state()
    del state["ema_dir"]
    gates = _gates({"QQQ": state})

    assert gates["long"]["blocked"] is False
    assert gates["short"]["blocked"] is False


def test_震荡与追高都不再拦():
    """规则 5、规则 7 删除：效率比 0.05、价距 VWAP 3σ 都放行。"""
    for state in (_state(er=0.05), _state(price=503.0, vwap=500.0, sigma=1.0)):
        gates = _gates({"QQQ": state})
        assert gates["long"]["blocked"] is False, gates
        assert gates["short"]["blocked"] is False, gates


def test_data_quality_blocks_both_sides():
    gates = _gates({"QQQ": _state(tradable=False)})

    assert gates["long"]["rule"] == "watcher"
    assert gates["short"]["rule"] == "watcher"


def test_account_wide_rules_block_both_sides():
    """午后禁入是账户/时段级规则，与方向无关，两边同拦。"""
    gates = _gates({"QQQ": _state()}, mins=D("200"))

    assert gates["long"]["rule"] == "afternoon"
    assert gates["short"]["rule"] == "afternoon"
