from decimal import Decimal

import pytest

from app.datasource import WatcherDataSource
from app.services.feed_snapshot import build_feed_snapshot
from app.services.market_context import build_market_context, market_signal


class _StubClient:
    def __init__(self, online=True, states=None, last=None):
        self._running = online
        self._watchers = {"mock": True} if online else {}
        self.states = states or {}
        self.last_signal_ts = last or {}
    def get_all_states(self):
        return self.states
    def get_ticker_state(self, ticker):
        return self.states.get(ticker)
    def get_watchlist(self):
        return list(self.states.keys())


def _ds(client):
    ds = WatcherDataSource.__new__(WatcherDataSource)  # 跳过 __init__（不建 AlpacaData）
    ds._client = client
    return ds


# 2026-08-26 趋势分下线：原先这里有两条用例断言 `regime` 由 watcher 下发的
# `trend_direction` 映射而来（up→trend_up 等）。那条映射连同 watcher 侧的
# `trend_direction` 键一起删了，被测对象已不存在，故整体移除而不是改断言。
# 「趋势键没了闸门照常放行」由 tests/test_gate_survives_trend_keys_gone.py 承接。


def test_watcher_regime_raises_when_not_tradable():
    ds = _ds(_StubClient(states={"SPY": {"trend_score": {"tradable": False, "direction": "up"}}}))
    with pytest.raises(RuntimeError, match="不可交易"):
        market_signal(ds, "SPY")


def test_market_context_blocks_when_signal_offline():
    ds = _ds(_StubClient(online=False))
    with pytest.raises(RuntimeError, match="盯盘引擎未启动"):
        build_market_context(ds, "SPY", "long")


def test_feed_snapshot_reports_online_and_ordered_pool():
    """#4：pool 顺序须按信号新鲜度排（order_pool 接线），不是固定插入顺序。

    B 有信号（last_signal_ts）→ 排最前；其余（A/QQQ/IWM/SPY）保持原插入顺序在后。
    """
    client = _StubClient(
        online=True,
        states={"A": {"trend_score": {"tradable": True, "direction": "up"}},
                "B": {"trend_score": {"tradable": False, "direction": "chop"}}},
        last={"B": 99.0})
    ds = _ds(client)
    snap = build_feed_snapshot(ds)
    assert snap["signal_online"] is True
    assert [p["sym"] for p in snap["pool"]] == ["B", "A", "QQQ", "IWM", "SPY"]


def test_feed_snapshot_exposes_last_signal():
    """#5：feed_snapshot 须带出当前最新信号的 ticker/时间戳，供前端自动定位聚焦。"""
    client = _StubClient(
        online=True,
        states={"A": {"trend_score": {"tradable": True, "direction": "up"}},
                "B": {"trend_score": {"tradable": False, "direction": "chop"}}},
        last={"B": 99.0, "A": 50.0})
    ds = _ds(client)
    snap = build_feed_snapshot(ds)
    assert snap["last_signal"] == {"sym": "B", "ts": 99.0}


def test_feed_snapshot_last_signal_none_without_any_signal():
    ds = _ds(_StubClient(online=True, states={"A": {}}))
    assert build_feed_snapshot(ds)["last_signal"] is None


def test_feed_snapshot_reports_offline_when_watcher_down():
    # 盯盘引擎未运行/无监听标的 → signal_online=False（前端离线横幅锁单）。
    # 回归护栏：合并后曾被硬编码为 True，致离线横幅永不触发，且旧测试只覆盖 online=True 而漏网。
    ds = _ds(_StubClient(online=False))
    assert build_feed_snapshot(ds)["signal_online"] is False


def _feed_state(
    *,
    direction="up",
    tradable=True,
    score=0.7,
    price="100",
    vwap="99",
    atr="1",
    sigma="0.5",
    primary_direction=None,
    primary_score=None,
):
    """2026-08-02 主副双层后，watcher 下发的是两层结构，helper 必须跟着给两层。

    `primary_*` 缺省跟随 `direction`/`score`（大多数用例不关心两层分歧）；要构造
    「冲量与主趋势打架」的场景就显式传 primary_*。
    """
    if primary_direction is None:
        primary_direction = direction
    if primary_score is None:
        primary_score = score
    return {
        "trend_score": {
            "tradable": tradable,
            "data_quality_ok": tradable,
            "direction": direction,
            "score": score,
            "impulse_score": score,
            "primary_direction": primary_direction,
            "primary_score": primary_score,
        },
        "current_bar": {"last_price": float(price)},
        "vwap": float(vwap),
        "atr": float(atr),
        "er": 0.4,
        "vwap_sigma": float(sigma),
    }


def test_feed_snapshot_dist_uses_sigma_not_atr():
    # dist = (price−vwap)/σ = (100−99)/0.5 = +2.0
    client = _StubClient(states={"QQQ": _feed_state(
        price="100",
        vwap="99",
        sigma="0.5",
    )})
    ds = _ds(client)
    tick = next(t for t in build_feed_snapshot(ds)["ticks"] if t["sym"] == "QQQ")
    assert tick["vwap"] == "+2.0"


def test_feed_snapshot_caps_huge_opening_vwap_distance_for_display_only():
    """开盘初期 σ 极小时，卡片封顶显示，避免超长数字撑坏布局。"""
    client = _StubClient(states={
        "QQQ": _feed_state(price="100", vwap="99", sigma="0.000001"),
        "SPY": _feed_state(price="99", vwap="100", sigma="0.000001"),
    })
    ds = _ds(client)

    ticks = {t["sym"]: t for t in build_feed_snapshot(ds)["ticks"]}

    assert ticks["QQQ"]["vwap"] == "≥99.9"
    assert ticks["SPY"]["vwap"] == "≤−99.9"


def test_signal_carries_vwap_sigma_from_state():
    st = {"trend_score": {"tradable": True, "direction": "up"},
          "current_bar": {"last_price": 100.0},
          "vwap": 99.0, "atr": 1.0, "er": 0.4, "vwap_sigma": 0.5}
    ds = _ds(_StubClient(states={"QQQ": st}))
    sig = market_signal(ds, "QQQ")
    assert sig.vwap_sigma == Decimal("0.5")


# 2026-09-01：盯盘页趋势徽章（_regime_display）随震荡闸、方向闸一并删除，
# 这里原有的三条徽章用例同时移除。


def test_signal_vwap_sigma_defaults_zero_when_missing():
    # 状态缺 vwap_sigma（早盘未算完）→ 0（哨兵，规则7 将 fail-open 跳过）
    st = {"trend_score": {"tradable": True, "direction": "up"},
          "current_bar": {"last_price": 100.0},
          "vwap": 99.0, "atr": 1.0, "er": 0.4}
    ds = _ds(_StubClient(states={"QQQ": st}))
    assert market_signal(ds, "QQQ").vwap_sigma == Decimal("0")
