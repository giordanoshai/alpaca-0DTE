# -*- coding: utf-8 -*-
import asyncio
from datetime import datetime, timedelta

import pytest

import app.services.market_tide_service as mts

from app.services.market_tide_service import (
    compute_tide_current,
    build_tide_series,
    detect_cross,
    tide_view,
    TideRestState,
    load_tide_day,
    session_label,
    tide_snapshot,
    broadcast_tide,
    seed_previous_session,
    market_tide_refresh_loop,
)

H = 5_000_000


@pytest.fixture(autouse=True)
def _reset_uw_client():
    """`_uw_client()` 是模块级单例，会把某条用例 patch 进去的假 client 留给下一条。

    实测：`test_load_tide_day_网络异常返回error` patch 的 `_Err` 被缓存后，
    后面两条 load_tide_day 用例全都拿到它、恒定失败。模块级状态必须按用例重置。
    """
    mts._UW_CLIENT = None
    yield
    mts._UW_CLIENT = None


def test_每次拉取复用同一个UwClient而不是新建(monkeypatch):
    """2026-08-15 起 `UwClient` 内部持有一条常驻 `requests.Session`（连接池），
    而且**没有 close()**。每 30s 新建一个 client 就是每 30s 漏一个连接池
    —— 本机有过 WinError 10055 本地端口耗尽的前科，这个方向不能碰。

    顺带：复用之后连接也复用了，省掉每次约 0.6s 的 SOCKS5 + TLS 握手。
    """
    built = []

    class _FakeUw:
        def __init__(self, **kw):
            built.append(kw)

        @staticmethod
        def zero_dte_tide(date_str, tide_type="all", moneyness="all"):
            return [{"net_call_premium": "10", "net_put_premium": "1"}]

    monkeypatch.setattr("app.integrations.uw.UwClient", _FakeUw)  # 单例由 autouse fixture 清空

    st = TideRestState()
    asyncio.run(load_tide_day(st, "2026-08-14"))
    asyncio.run(load_tide_day(st, "2026-08-14"))
    asyncio.run(load_tide_day(st, "2026-08-13"))

    assert len(built) == 1, f"建了 {len(built)} 个 UwClient，应当全程复用同一个"


def test_compute_tide_current_bullish():
    ticks = [{"net_call_premium": "30000000", "net_put_premium": "5000000"}]
    r = compute_tide_current(ticks)
    assert r["diff"] == 25_000_000
    assert r["direction"] == "bullish"
    assert r["label"] == "看多 +$25.0M"


def test_compute_tide_current_bearish_uses_last_tick():
    ticks = [
        {"net_call_premium": "10", "net_put_premium": "0"},
        {"net_call_premium": "1000000", "net_put_premium": "13000000"},
    ]
    r = compute_tide_current(ticks)
    assert r["diff"] == -12_000_000
    assert r["direction"] == "bearish"
    assert r["label"] == "看空 -$12.0M"


def test_compute_tide_current_empty():
    assert compute_tide_current([]) == {
        "diff": None,
        "direction": "flat",
        "label": "--",
    }


def test_compute_tide_current_with_trailing_none_points():
    """尾部出现 None 点时，跳到最后一个有效点，不抛 TypeError。

    ⚠️ **这是防守，不是已观测行为**：2026-08-20 实测 UW 只返回已成型的根
    （历史日 390/390 全有值；盘前拉当天直接返回空列表 `data: []`），
    **一个 None 槽位都没见到**。此前文档里写的「返回全天 390 根、未到的槽位是 None」
    未经证实。盘中实时拉取还没测过，过滤留着兜底、但别把它当成已知契约去依赖。
    """
    ticks = [
        {"net_call_premium": "1000000", "net_put_premium": "200000"},
        {"net_call_premium": "5000000", "net_put_premium": "1000000"},
        {"net_call_premium": None, "net_put_premium": None},
        {"net_call_premium": None, "net_put_premium": None},
    ]
    r = compute_tide_current(ticks)
    assert r["diff"] == 4_000_000
    assert r["direction"] == "bullish"
    assert r["label"] == "看多 +$4.0M"


def test_tide_rest_state_update_ignores_trailing_none_points():
    st = TideRestState()
    ticks = [
        {"timestamp": "2026-08-18T09:30:00Z", "net_call_premium": "1000", "net_put_premium": "200"},
        {"timestamp": "2026-08-18T09:31:00Z", "net_call_premium": "3000", "net_put_premium": "1000"},
        {"timestamp": "2026-08-18T09:32:00Z", "net_call_premium": None, "net_put_premium": None},
    ]
    st.update(ticks)
    assert st.seeded is True
    assert st.cum_call == 3000.0
    assert st.cum_put == 1000.0
    assert len(st.series) == 2


def test_build_tide_series():
    ticks = [{
        "timestamp": "2026-07-22T09:30:00-04:00",
        "net_call_premium": "5",
        "net_put_premium": "2",
    }]
    s = build_tide_series(ticks)
    assert len(s) == 1
    assert s[0]["call"] == 5.0
    assert s[0]["put"] == 2.0
    assert s[0]["t"] == int(
        datetime.fromisoformat("2026-07-22T09:30:00-04:00").timestamp()
    )


def test_detect_cross_first_entry_from_neutral_no_alert():
    state = {"zone": "neutral"}
    assert detect_cross(30_000_000, state, H) is None
    assert state["zone"] == "above"


def test_detect_cross_below_to_above_is_bullish():
    state = {"zone": "below"}
    r = detect_cross(6_000_000, state, H)
    assert r == {"direction": "bullish", "diff": 6_000_000}
    assert state["zone"] == "above"


def test_detect_cross_above_to_below_is_bearish():
    state = {"zone": "above"}
    r = detect_cross(-6_000_000, state, H)
    assert r == {"direction": "bearish", "diff": -6_000_000}
    assert state["zone"] == "below"


def test_detect_cross_death_zone_no_alert_and_keeps_zone():
    state = {"zone": "above"}
    assert detect_cross(1_000_000, state, H) is None
    assert state["zone"] == "above"


def test_detect_cross_same_direction_not_repeated():
    state = {"zone": "above"}
    assert detect_cross(9_000_000, state, H) is None
    assert state["zone"] == "above"


def test_detect_cross_deadzone_bounce_back_same_side_no_alert():
    state = {"zone": "above"}
    assert detect_cross(1_000_000, state, H) is None
    assert detect_cross(8_000_000, state, H) is None
    assert state["zone"] == "above"


def test_detect_cross_reverses_can_fire_again():
    state = {"zone": "above"}
    assert detect_cross(-6_000_000, state, H)["direction"] == "bearish"
    assert detect_cross(6_000_000, state, H)["direction"] == "bullish"


class _FakeManager:
    def __init__(self):
        self.msgs = []

    async def broadcast(self, message):
        self.msgs.append(message)


_NOW = datetime.fromisoformat("2026-07-22T10:00:00-04:00")


# ── TideRestState 纯状态测试 ───────────────────────────────────────────────

def test_update_整体替换累计值与series():
    st = TideRestState()
    ticks = [
        {"timestamp": "2026-08-15T09:30:00-04:00", "net_call_premium": "1000", "net_put_premium": "200"},
        {"timestamp": "2026-08-15T09:31:00-04:00", "net_call_premium": "3000", "net_put_premium": "500"},
    ]
    st.update(ticks)
    assert st.cum_call == 3000.0
    assert st.cum_put == 500.0
    assert len(st.series) == 2
    assert st.seeded is True


def test_update_空数据不动现值():
    st = TideRestState()
    st.cum_call = 1000.0
    st.cum_put = 200.0
    st.seeded = True
    st.update([])
    assert st.cum_call == 1000.0
    assert st.cum_put == 200.0
    assert st.seeded is True


def test_reset_清零并更新session_date():
    st = TideRestState()
    st.cum_call = 1000.0
    st.cum_put = 200.0
    st.seeded = True
    st.reset("2026-08-15")
    assert st.cum_call == 0.0
    assert st.cum_put == 0.0
    assert st.seeded is False
    assert st.series == []
    assert st.session_date == "2026-08-15"


# ── load_tide_day ─────────────────────────────────────────────────────────────

def test_load_tide_day_网络异常返回error且不动现值(monkeypatch):
    import app.integrations.uw as uw_mod

    class _Err:
        def __init__(self, **kw): pass
        def zero_dte_tide(self, date_str, **kw): raise RuntimeError("中转站超时")

    monkeypatch.setattr(uw_mod, "UwClient", _Err)
    st = TideRestState()
    st.cum_call = 100.0
    assert asyncio.run(load_tide_day(st, "2026-08-15")) == mts._SEED_ERROR
    assert st.cum_call == 100.0


def test_load_tide_day_当日无数据返回empty且不动现值(monkeypatch):
    import app.integrations.uw as uw_mod

    class _Empty:
        def __init__(self, **kw): pass
        def zero_dte_tide(self, date_str, **kw): return []

    monkeypatch.setattr(uw_mod, "UwClient", _Empty)
    st = TideRestState()
    st.cum_call = 100.0
    assert asyncio.run(load_tide_day(st, "2026-08-15")) == mts._SEED_EMPTY
    assert st.cum_call == 100.0


def test_load_tide_day_成功更新state并返回ok(monkeypatch):
    import app.integrations.uw as uw_mod

    class _Ok:
        def __init__(self, **kw): pass
        def zero_dte_tide(self, date_str, **kw):
            return [{"timestamp": "2026-08-15T09:31:00-04:00",
                     "net_call_premium": "2500", "net_put_premium": "900"}]

    monkeypatch.setattr(uw_mod, "UwClient", _Ok)
    st = TideRestState()
    assert asyncio.run(load_tide_day(st, "2026-08-15")) == mts._SEED_OK
    assert st.cum_call == 2500.0
    assert st.cum_put == 900.0
    assert st.seeded is True


# ── 广播 ──────────────────────────────────────────────────────────────────────

_REST_TICKS = [
    {"timestamp": "2026-08-05T09:30:00-04:00", "net_call_premium": "30000000", "net_put_premium": "1000000"},
]


def test_broadcast用累计值出徽章并按需带series():
    st = TideRestState()
    st.update(_REST_TICKS)
    st.session_date = _NOW.strftime("%Y-%m-%d")
    mgr = _FakeManager()

    asyncio.run(broadcast_tide(st, mgr, _NOW, {"zone": "below"}, with_series=True))

    types = [m["type"] for m in mgr.msgs]
    assert "market_tide" in types and "market_tide_cross" in types
    tide = next(m for m in mgr.msgs if m["type"] == "market_tide")
    assert tide["data"]["diff"] == 29_000_000.0
    assert tide["data"]["direction"] == "bullish"
    assert tide["data"]["seeded"] is True
    assert tide["data"]["session"] == "live"
    assert tide["data"]["ts"] == _NOW.isoformat()
    assert len(tide["data"]["series"]) == 1


def test_broadcast不带series时省掉该键_省带宽():
    st = TideRestState()
    st.update(_REST_TICKS)
    mgr = _FakeManager()

    asyncio.run(broadcast_tide(st, mgr, _NOW, {"zone": "neutral"}, with_series=False))

    tide = next(m for m in mgr.msgs if m["type"] == "market_tide")
    assert "series" not in tide["data"]
    assert tide["data"]["diff"] == 29_000_000.0


# ── 场次归属与开盘清零 ────────────────────────────────────────────────────────

def _et(day: str, hh: int, mm: int) -> datetime:
    return datetime.strptime(f"{day} {hh:02d}:{mm:02d}", "%Y-%m-%d %H:%M")


def test_session_label_四态():
    st = TideRestState()
    assert session_label(st, _et("2026-08-07", 10, 0)) == "empty"
    assert session_label(None, _et("2026-08-07", 10, 0)) == "empty"

    st.reset("2026-08-07")
    assert session_label(st, _et("2026-08-07", 10, 0)) == "live"
    assert session_label(st, _et("2026-08-07", 9, 29)) == "closed"
    assert session_label(st, _et("2026-08-07", 16, 1)) == "closed"
    assert session_label(st, _et("2026-08-08", 8, 0)) == "prev"


def test_reset保留上一交易日终值直到开盘():
    st = TideRestState()
    st.reset("2026-08-06")
    st.cum_call, st.cum_put = 300.0, 100.0
    closing = st.cum_call - st.cum_put
    assert closing == 200.0

    # 次日盘前：值仍在，且被标成上一场
    assert st.cum_call - st.cum_put == closing
    assert session_label(st, _et("2026-08-07", 8, 0)) == "prev"

    # 开盘那一刻才清零
    st.reset("2026-08-07")
    assert st.cum_call == 0.0 and st.cum_put == 0.0
    assert session_label(st, _et("2026-08-07", 10, 0)) == "live"


def test_清零判定在RTH闸之后():
    import inspect
    src = inspect.getsource(market_tide_refresh_loop)
    rth_gate = src.index("_RTH_START <= now_et.time()")
    reset_call = src.index("state.reset(date_str)")
    assert rth_gate < reset_call, "清零判定必须在非 RTH 之后，否则盘前会被清零"


def test_启动回填跳过周末并逐日回看(monkeypatch):
    tried: list = []

    async def _fake_load(state, date_str):
        tried.append(date_str)
        if date_str == "2026-08-05":
            state.cum_call, state.cum_put = 300.0, 100.0
            return mts._SEED_OK
        return mts._SEED_EMPTY

    monkeypatch.setattr(mts, "load_tide_day", _fake_load)
    st = TideRestState()
    ok = asyncio.run(seed_previous_session(st, _et("2026-08-10", 8, 0)))

    assert ok is True
    assert tried == ["2026-08-07", "2026-08-06", "2026-08-05"]
    assert st.session_date == "2026-08-05"
    assert st.cum_call - st.cum_put == 200.0
    assert session_label(st, _et("2026-08-10", 8, 0)) == "prev"


def test_收盘后启动回填的是今日(monkeypatch):
    async def _fake_load(state, date_str):
        state.cum_call = 500.0
        return mts._SEED_OK

    monkeypatch.setattr(mts, "load_tide_day", _fake_load)
    st = TideRestState()
    asyncio.run(seed_previous_session(st, _et("2026-08-07", 16, 30)))

    assert st.session_date == "2026-08-07"
    assert session_label(st, _et("2026-08-07", 16, 30)) == "closed"


def test_回填全失败不抛错(monkeypatch):
    async def _fake_load(state, date_str):
        return mts._SEED_EMPTY

    monkeypatch.setattr(mts, "load_tide_day", _fake_load)
    st = TideRestState()
    assert asyncio.run(seed_previous_session(st, _et("2026-08-07", 8, 0))) is False
    assert st.session_date == ""
    assert session_label(st, _et("2026-08-07", 8, 0)) == "empty"


def test_周末跳过不消耗回看预算(monkeypatch):
    tried: list = []

    async def _fake_load(state, date_str):
        tried.append(date_str)
        return mts._SEED_EMPTY

    monkeypatch.setattr(mts, "load_tide_day", _fake_load)
    st = TideRestState()
    asyncio.run(seed_previous_session(st, _et("2026-08-10", 8, 0), max_lookback=3))

    assert tried == ["2026-08-07", "2026-08-06", "2026-08-05"]


def test_snapshot无累加器时为empty():
    data = tide_snapshot(None, _NOW)
    assert data["session"] == "empty"
    assert data["diff"] is None and data["label"] == "--"
    assert data["seeded"] is False and data["series"] == []


def test_非live场次不报交叉提醒():
    st = TideRestState()
    st.cum_call, st.cum_put = 30_000_000.0, 1_000_000.0
    st.session_date = "1970-01-01"
    mgr = _FakeManager()

    asyncio.run(broadcast_tide(st, mgr, _NOW, {"zone": "below"}, with_series=False))

    types = [m["type"] for m in mgr.msgs]
    assert "market_tide" in types
    assert "market_tide_cross" not in types
    assert mgr.msgs[0]["data"]["session"] == "prev"


class _FakeSync:
    def __init__(self, times):
        self._it = iter(times)
        self._last = times[-1]

    def sync(self):
        pass

    def get_ntp_time(self):
        self._last = next(self._it, self._last)
        return self._last


# ── 轮询循环测试 ─────────────────────────────────────────────────────────────

def test_盘前启动把昨收推给盯盘页(monkeypatch):
    pre = datetime.fromisoformat("2026-08-07T08:00:00-04:00")
    monkeypatch.setattr(mts, "_ACTIVE_ACC", None)
    monkeypatch.setitem(mts.MARKET_TIDE_CONFIG, "enabled", True)
    monkeypatch.setattr(mts, "NTPTimeSync", lambda: _FakeSync([pre]))

    async def _fake_seed(state, date_str):
        state.cum_call, state.cum_put = 30_000_000.0, 1_000_000.0
        state.session_date = "2026-08-06"
        state.seeded = True
        return mts._SEED_OK

    monkeypatch.setattr(mts, "load_tide_day", _fake_seed)

    async def _cancel_sleep(s):
        raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "sleep", _cancel_sleep)

    mgr = _FakeManager()
    asyncio.run(market_tide_refresh_loop(mgr))

    tides = [m for m in mgr.msgs if m["type"] == "market_tide"]
    assert len(tides) == 1
    assert tides[0]["data"]["session"] == "prev"
    assert tides[0]["data"]["diff"] == 29_000_000.0


def test_盘中轮询成功持续广播(monkeypatch):
    rth = datetime.fromisoformat("2026-08-07T10:00:00-04:00")
    monkeypatch.setattr(mts, "_ACTIVE_ACC", None)
    monkeypatch.setitem(mts.MARKET_TIDE_CONFIG, "enabled", True)
    monkeypatch.setattr(mts, "NTPTimeSync", lambda: _FakeSync([rth]))

    async def _fake_load(state, date_str):
        state.cum_call, state.cum_put = 50_000_000.0, 10_000_000.0
        state.session_date = "2026-08-07"
        state.seeded = True
        return mts._SEED_OK

    monkeypatch.setattr(mts, "load_tide_day", _fake_load)

    async def _cancel_sleep(s):
        raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "sleep", _cancel_sleep)

    mgr = _FakeManager()
    asyncio.run(market_tide_refresh_loop(mgr))

    tides = [m for m in mgr.msgs if m["type"] == "market_tide"]
    assert len(tides) == 1
    assert tides[0]["data"]["session"] == "live"
    assert tides[0]["data"]["diff"] == 40_000_000.0


# ── 三态返回：_SEED_ERROR ≠ _SEED_EMPTY ───────────────────────────────────────
#
# 下面这一组钉的是 2026-08-14 11:23 线上实测的真实 bug，**迁 REST 时机制保住了、
# 测试却被一并删掉**（2026-08-15 复查补回）。当时的日志：
#   11:23:52 WARNING REST 播种失败: SOCKSHTTPSConnectionPool(...) Read timeout   ← 试 08-13
#   11:23:56 INFO    已回填 2026-08-12 终值：净额 -106,089,408
# 而 08-13 真实收盘是 +314,246,584。徽章显示「看空 $106M」，真相是「看多 $314M」——
# 量级和方向全是前一天的。
#
# ⚠️ 这几条用例的要害是 fake **必须返回 `_SEED_ERROR`**：新写的回填用例清一色只返
# `_SEED_OK` / `_SEED_EMPTY`，于是把 `seed_previous_session` 里的
# `if result != _SEED_ERROR: break` 改成裸 `break` 也能全绿——正好放过这个 bug。

def test_回填遇到网络异常先在同一天重试而不是跳过整个交易日(monkeypatch):
    tried: list = []

    async def _fake_load(state, date_str):
        tried.append(date_str)
        if len(tried) == 1:
            return mts._SEED_ERROR             # 第一次中转站超时
        state.cum_call, state.cum_put = 300.0, 100.0
        return mts._SEED_OK

    monkeypatch.setattr(mts, "load_tide_day", _fake_load)
    monkeypatch.setattr(mts, "_SEED_ERROR_BACKOFF_S", 0)
    st = TideRestState()

    # 周五 08:00 盘前启动 → 应当回填周四(08-13)
    ok = asyncio.run(seed_previous_session(st, _et("2026-08-14", 8, 0)))

    assert ok is True
    assert tried == ["2026-08-13", "2026-08-13"], "网络异常必须在同一天重试，不能往前跳"
    assert st.session_date == "2026-08-13"


def test_返回空说明那天休市_不浪费重试直接往前找(monkeypatch):
    """空数据是「这天没开市」的确定信号，同一天重试多少次都还是空。"""
    tried: list = []

    async def _fake_load(state, date_str):
        tried.append(date_str)
        if date_str == "2026-08-12":
            state.cum_call = 300.0
            return mts._SEED_OK
        return mts._SEED_EMPTY

    monkeypatch.setattr(mts, "load_tide_day", _fake_load)
    monkeypatch.setattr(mts, "_SEED_ERROR_BACKOFF_S", 0)
    st = TideRestState()
    asyncio.run(seed_previous_session(st, _et("2026-08-14", 8, 0)))

    assert tried == ["2026-08-13", "2026-08-12"], "空数据不该在同一天重试"


def test_同一天重试用尽后才继续往前找(monkeypatch):
    """中转站长时间挂掉时不能死磕一天，否则整个回填功能失效、徽章整天空着。"""
    tried: list = []

    async def _fake_load(state, date_str):
        tried.append(date_str)
        return mts._SEED_ERROR

    monkeypatch.setattr(mts, "load_tide_day", _fake_load)
    monkeypatch.setattr(mts, "_SEED_ERROR_BACKOFF_S", 0)
    st = TideRestState()
    ok = asyncio.run(seed_previous_session(st, _et("2026-08-14", 8, 0), max_lookback=2))

    assert ok is False
    assert tried.count("2026-08-13") == mts._SEED_ERROR_ATTEMPTS
    assert tried.count("2026-08-12") == mts._SEED_ERROR_ATTEMPTS


def test_同一天的重试不消耗回看预算(monkeypatch):
    """max_lookback 计的是**交易日**，不是 HTTP 请求次数。"""
    tried: list = []

    async def _fake_load(state, date_str):
        tried.append(date_str)
        return mts._SEED_ERROR

    monkeypatch.setattr(mts, "load_tide_day", _fake_load)
    monkeypatch.setattr(mts, "_SEED_ERROR_BACKOFF_S", 0)
    st = TideRestState()
    asyncio.run(seed_previous_session(st, _et("2026-08-14", 8, 0), max_lookback=3))

    assert sorted(set(tried)) == ["2026-08-11", "2026-08-12", "2026-08-13"], \
        "重试同一天不该吃掉回看预算"


def test_拉取结果是三态而不是布尔():
    """锁死契约：`if await load_tide_day(...)` 这种写法下 'error'/'empty' 都是 truthy，
    会把失败当成功。三个结果必须都不是 bool，逼调用方显式判等。"""
    for value in (mts._SEED_OK, mts._SEED_EMPTY, mts._SEED_ERROR):
        assert not isinstance(value, bool)
    assert len({mts._SEED_OK, mts._SEED_EMPTY, mts._SEED_ERROR}) == 3


# ── 场次切换补帧与快照解耦（同为迁 REST 时误删，2026-08-15 补回）──────────────

def test_收盘那一刻补推一帧标记今日收盘(monkeypatch):
    """不补这一帧，徽章会一直停在收盘前那一秒的样子，看不出已经收盘。

    同时钉住「非 RTH 不再发 REST 请求」：收盘后那两拍 load_tide_day 一次都不该调。
    """
    times = [
        datetime.fromisoformat("2026-08-07T15:59:00-04:00"),   # 启动即盘中：不回填
        datetime.fromisoformat("2026-08-07T15:59:30-04:00"),   # 第 1 拍：盘中
        datetime.fromisoformat("2026-08-07T16:01:00-04:00"),   # 第 2 拍：已收盘
        datetime.fromisoformat("2026-08-07T16:01:02-04:00"),   # 第 3 拍：仍收盘
    ]
    monkeypatch.setattr(mts, "_ACTIVE_ACC", None)
    monkeypatch.setitem(mts.MARKET_TIDE_CONFIG, "enabled", True)
    monkeypatch.setattr(mts, "NTPTimeSync", lambda: _FakeSync(times))

    loads: list = []

    async def _fake_load(state, date_str):
        loads.append(date_str)
        state.cum_call, state.cum_put = 100_000.0, 0.0
        state.seeded = True
        return mts._SEED_OK

    monkeypatch.setattr(mts, "load_tide_day", _fake_load)

    calls = {"n": 0}

    async def _sleep_until_3rd(s):
        calls["n"] += 1
        if calls["n"] >= 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "sleep", _sleep_until_3rd)

    mgr = _FakeManager()
    asyncio.run(market_tide_refresh_loop(mgr))

    tides = [m["data"] for m in mgr.msgs if m["type"] == "market_tide"]
    assert [t["session"] for t in tides] == ["live", "closed"]
    # 陈值不该被改动：两帧净额相同
    assert tides[0]["diff"] == tides[1]["diff"]
    # 收盘后不再发 REST 请求
    assert loads == ["2026-08-07"]


def test_broadcast发出的series与状态解耦():
    """广播出去的必须是快照而非 state.series 的引用，否则后续轮询会追改已发出的帧。"""
    st = TideRestState()
    st.update([{"timestamp": "2026-08-15T09:30:00-04:00",
                "net_call_premium": "100", "net_put_premium": "0"}])
    mgr = _FakeManager()
    asyncio.run(broadcast_tide(st, mgr, _NOW, {"zone": "neutral"}, with_series=True))

    st.series[-1]["call"] = 999.0                   # 就地改状态
    sent = next(m for m in mgr.msgs if m["type"] == "market_tide")["data"]["series"]
    assert sent[-1]["call"] == 100.0                # 已发出的快照不被追改


def test_uw_client_zero_dte_tide_group_unpacking(monkeypatch):
    """验证 zero_dte_tide 正确解包 UW Expiry Tide Response 的嵌套结构。"""
    from app.integrations.uw import UwClient

    client = UwClient(api_key="mock_key")

    mock_resp = {
        "data": [
            {
                "tide_type": "all",
                "moneyness": "all",
                "data": [
                    {
                        "timestamp": "2026-08-18T09:30:00Z",
                        "date": "2026-08-18",
                        "net_call_premium": 5000.0,
                        "net_put_premium": 2000.0,
                        "net_volume": 100.0,
                    }
                ]
            }
        ],
        "date": "2026-08-18",
        "expiration": ["zero_dte"],
    }

    monkeypatch.setattr(client, "_get_json", lambda path, params=None: mock_resp)

    res = client.zero_dte_tide("2026-08-18")
    assert res is not None
    assert len(res) == 1
    assert res[0]["net_call_premium"] == 5000.0
    assert res[0]["net_put_premium"] == 2000.0


def test_uw_client_zero_dte_tide_empty_response(monkeypatch):
    """验证 zero_dte_tide 在空响应时返回 None。"""
    from app.integrations.uw import UwClient

    client = UwClient(api_key="mock_key")
    monkeypatch.setattr(client, "_get_json", lambda path, params=None: {"data": []})
    assert client.zero_dte_tide("2026-08-18") is None

