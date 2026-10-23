"""盯盘器 VWAP 锚定窗口测试。

背景：`_recalculate_minute_state` 原先用 `get_df_1m(tail=300)` 的同一份切片同时喂
VWAP 与 ATR/ER/趋势分。当日 bar 自 04:00 起算（Yahoo prepost=True 带盘前），
14:30 ET 之后 300 根窗口的左边界就越过 09:30，开盘那头的成交量被逐分钟丢弃，
session VWAP 退化成「5 小时滚动 VWAP」。实测 SPY 2026-07-31 收盘时偏 0.45σ，
而追高闸阈值只有 2σ。

修法：VWAP/σ 改吃「当日 RTH 全量」锚定窗口，ATR/ER/趋势分仍吃 300 根滚动窗口。
"""
from __future__ import annotations

import pandas as pd
import pytest

from app.watcher.watcher_manager import TickerWatcher, session_rth_bars

ET = "America/New_York"


def _day_frame(day: str = "2026-07-31", *, pre_close=100.0, rth_close=110.0) -> pd.DataFrame:
    """构造一天的 1m 线：04:00–09:29 盘前（volume=0，Yahoo 口径），09:30–15:59 RTH。

    RTH 前半段价格 rth_close-10、后半段 rth_close+10，两段等量 —— 真 session VWAP
    落在中间；若窗口被截断只剩后半段，VWAP 会被拉高 10，差异一眼可见。
    """
    pre_idx = pd.date_range(f"{day} 04:00", f"{day} 09:29", freq="1min", tz=ET)
    rth_idx = pd.date_range(f"{day} 09:30", f"{day} 15:59", freq="1min", tz=ET)

    pre = pd.DataFrame(
        {"open": pre_close, "high": pre_close, "low": pre_close,
         "close": pre_close, "volume": 0},
        index=pre_idx,
    )
    half = len(rth_idx) // 2
    prices = [rth_close - 10.0] * half + [rth_close + 10.0] * (len(rth_idx) - half)
    rth = pd.DataFrame(
        {"open": prices, "high": prices, "low": prices,
         "close": prices, "volume": 1000},
        index=rth_idx,
    )
    return pd.concat([pre, rth])


class _StubDataManager:
    """只实现 watcher 用到的两个取数方法，避免碰真实缓存/网络。"""

    def __init__(self, df_1m: pd.DataFrame):
        self._df = df_1m
        self.bar_count = len(df_1m)

    def get_df_1m(self, tail=None, current_bar=None) -> pd.DataFrame:
        df = self._df.copy()
        if tail is None or len(df) <= tail:
            return df
        return df.tail(tail)

    def get_df_5m(self, tail=None, current_bar=None) -> pd.DataFrame:
        return pd.DataFrame()


class _StubTimeSync:
    """固定时钟。

    真实 NTPTimeSync 取网络时间，测试必须把"今天"钉死：session VWAP 现在要求
    显式传入当前交易日，若"今天"不是造数据的那天，函数会（正确地）拒绝算出 VWAP。
    """

    def __init__(self, now_et: str):
        self._now = pd.Timestamp(now_et, tz=ET)

    def get_ntp_time(self):
        return self._now


class _StubBarBuilder:
    def __init__(self, last_price=None):
        self._bar = {"last_price": last_price} if last_price is not None else {}

    def get_current_bar(self):
        return self._bar


def _watcher(df: pd.DataFrame, now_et: str, ticker: str = "IWM") -> TickerWatcher:
    """裸装一个 watcher：跳过引擎构建，只留重算与取状态需要的依赖。"""
    w = object.__new__(TickerWatcher)
    w.ticker = ticker
    w.data_manager = _StubDataManager(df)
    w.time_sync = _StubTimeSync(now_et)
    w.bar_builder = _StubBarBuilder()
    w._minute_state = {}
    w._running = True
    w._signal_engines = []
    w._last_result = None
    return w


# ---- 锚定窗口纯函数 ----

def test_session_rth_bars_剔除盘前且保留全部_rth():
    df = _day_frame()
    win = session_rth_bars(df)
    assert len(win) == 390                       # 09:30–15:59 全量，一根不少
    assert win.index[0].strftime("%H:%M") == "09:30"
    assert win.index[-1].strftime("%H:%M") == "15:59"


def test_session_rth_bars_只取最后一个交易日():
    df = pd.concat([_day_frame("2026-07-30"), _day_frame("2026-07-31")])
    win = session_rth_bars(df)
    assert len(win) == 390
    assert set(win.index.date) == {pd.Timestamp("2026-07-31").date()}


def test_session_rth_bars_盘前时段返回空():
    """盘前重算时 RTH 尚未开始 —— 返回空，让调用方走 fallback 而不是算出假 VWAP。"""
    df = _day_frame().between_time("04:00", "09:29")
    assert session_rth_bars(df).empty


def test_session_rth_bars_空表不炸():
    assert session_rth_bars(pd.DataFrame()).empty


# ---- 回归：收盘时 VWAP 不被 300 根窗口截断 ----

def test_收盘时_vwap_覆盖整个_rth_而非最后300根():
    watcher = _watcher(_day_frame(), "2026-07-31 16:00")

    watcher._recalculate_minute_state()
    vwap = watcher._minute_state["vwap"]

    # 真 session VWAP：前后两段等量 → 恰好落在 rth_close
    assert vwap == pytest.approx(110.0, abs=1e-6)
    # 截断版（最后 300 根）会几乎全落在后半段，明显高于 115
    assert vwap < 115.0


def test_sigma_同样按_rth_全量算():
    watcher = _watcher(_day_frame(), "2026-07-31 16:00")

    watcher._recalculate_minute_state()
    sigma = watcher._minute_state["vwap_sigma"]

    # 两段各偏离 VWAP 10，等量 → σ = 10
    assert sigma == pytest.approx(10.0, abs=1e-6)


def test_盘前重算不产出_vwap_数值():
    """RTH 未开始时不该拿盘前零量 bar 硬算，退回现价。"""
    watcher = _watcher(_day_frame().between_time("04:00", "09:29"),
                       "2026-07-31 09:00")

    watcher._recalculate_minute_state()
    assert watcher._minute_state["vwap"] == pytest.approx(100.0)   # 退回最后收盘价
    assert watcher._minute_state["vwap_sigma"] is None


# ---- 回归：跨日陈旧的 VWAP/σ 不得与今日现价拼在一起 ----

def test_重算时只认当前交易日的_rth():
    """数据里最后一根是昨日收盘、今日尚无 bar 时，不得把昨日 RTH 当成今日 session。

    旧实现用 `df.index[-1].date()` 推断"当日"，这种情况下会返回昨日 RTH，
    算出一份昨日的 VWAP/σ。
    """
    watcher = _watcher(_day_frame("2026-07-30"), "2026-07-31 04:37")

    watcher._recalculate_minute_state()

    assert watcher._minute_state["vwap_sigma"] is None
    assert watcher._minute_state["session_date"] == pd.Timestamp("2026-07-31").date()


def test_跨日后_get_state_让_vwap_失效():
    """_minute_state 只在 1m bar 收盘时刷新；盘前数据源不推 bar 时它会一直留着
    昨日收盘的 VWAP/σ，而 current_bar 是实时价。两者拼进同一份快照，validator
    就会拿今日价比昨日 VWAP —— 实测 2026-07-31 盘前 SPY 被判「追高 10.8σ」，
    当时 price(750.07) 尚在 vwap(750.94) 下方、做多本属有利位置。
    """
    watcher = _watcher(_day_frame("2026-07-30"), "2026-07-30 16:00", ticker="SPY")
    watcher._recalculate_minute_state()
    assert watcher._minute_state["vwap_sigma"] == pytest.approx(10.0)   # 昨日确有值

    # 时钟走到次日盘前，数据源没有新 bar → _minute_state 原封不动
    watcher.time_sync = _StubTimeSync("2026-07-31 04:37")
    state = watcher.get_state()

    assert state["vwap"] is None
    assert state["vwap_sigma"] is None      # → market_context 兜底为 0 → 规则7 fail-open


def test_同日内_get_state_不误伤_vwap():
    """对照组：同一交易日内，VWAP/σ 必须原样透出，不能被新鲜度检查误杀。"""
    watcher = _watcher(_day_frame(), "2026-07-31 16:00")
    watcher._recalculate_minute_state()

    state = watcher.get_state()

    assert state["vwap"] == pytest.approx(110.0, abs=1e-6)
    assert state["vwap_sigma"] == pytest.approx(10.0, abs=1e-6)


# ---- σ 的 ATR 地板（2026-08-08）----

def _frame_open_first_minute() -> pd.DataFrame:
    """昨日 300 根锯齿 RTH bar（喂 ATR）+ 今日第 1 根 RTH bar。

    昨日 H=L=C 在 99/101 间逐根交替 → 每根 TR 恒为 2.0 → ATR14 恰为 2.0，可精确断言。
    今日那根必须接着这个交替走（末根昨日 bar 是 99，故今日取 101）：
    若取别的价位，最后一根 TR 会变成跳空值，ATR14 就不再是 2.0
    （例如今日取 103 时 ATR = (13×2+4)/14 ≈ 2.143）。
    昨日给满 300 根是为了让 df_1m_ts 达到 trend_score 的正常输入规模——
    `_recalculate_minute_state` 只 catch ValueError，bar 太少时别的异常会漏出来。

    今日只有一根 bar 且 H=L=C → σ_session 精确为 0，正是开盘首分钟的真实形态。
    """
    prev_idx = pd.date_range("2026-07-30 09:30", periods=300, freq="1min", tz=ET)
    closes = [99.0 if i % 2 else 101.0 for i in range(300)]
    prev = pd.DataFrame(
        {"open": closes, "high": closes, "low": closes,
         "close": closes, "volume": 1000},
        index=prev_idx,
    )
    today_idx = pd.date_range("2026-07-31 09:30", periods=1, freq="1min", tz=ET)
    today = pd.DataFrame(
        {"open": 101.0, "high": 101.0, "low": 101.0,
         "close": 101.0, "volume": 1000},
        index=today_idx,
    )
    return pd.concat([prev, today])


def test_开盘首分钟_sigma被atr地板抬起():
    """单根 RTH bar 时 σ_session=0，不套地板会让规则7 的分母坍缩。

    实测 505 组样本：开盘第 1 分钟有 7.1% 因浮点残差算出 ~1e-13 的 σ，
    |price−vwap|/σ 达 1.4e12 直接拦死；第 2 分钟拦截率 47.5%。
    """
    watcher = _watcher(_frame_open_first_minute(), "2026-07-31 09:31")

    watcher._recalculate_minute_state()

    # 以下三个值均已在现有代码上实测：改动前 vwap_sigma 为 0.0
    assert watcher._minute_state["atr"] == pytest.approx(2.0)
    assert watcher._minute_state["vwap"] == pytest.approx(101.0)
    # σ_session=0 → 地板抬到 1.0×ATR = 2.0
    assert watcher._minute_state["vwap_sigma"] == pytest.approx(2.0)


def test_盘中_sigma长起来后地板失效():
    """守住「只修开盘、不改盘中」：σ 已远大于 ATR 时必须逐位不变。

    _day_frame 的 RTH 半日恒价 → 末 14 根 TR 全为 0 → ATR=0（已实测），
    地板 max(σ, 0) 退化为 σ 本身；σ 仍是两段各偏离 10 的 10.0。
    """
    watcher = _watcher(_day_frame(), "2026-07-31 16:00")

    watcher._recalculate_minute_state()

    assert watcher._minute_state["atr"] == pytest.approx(0.0)
    assert watcher._minute_state["vwap_sigma"] == pytest.approx(10.0, abs=1e-6)


def test_地板算崩不得带断整个分钟态重算(monkeypatch):
    """地板是这里唯一把 float 塞进 Decimal 比较的地方，NaN 会抛 InvalidOperation。

    它排在 ER / trend_score / indicators 之前，裸奔就会把后面全带断（前端 K 线
    不跳、闸门读到隔夜的旧趋势分）。失败必须只丢地板本身，σ 退回地板前的值。
    """
    def _boom(*_args, **_kwargs):
        raise ArithmeticError("模拟 NaN 触发的 decimal.InvalidOperation")

    monkeypatch.setattr(
        "app.watcher.watcher_manager.sigma_with_atr_floor", _boom)
    watcher = _watcher(_frame_open_first_minute(), "2026-07-31 09:31")

    watcher._recalculate_minute_state()

    # σ 退回地板前的原值（今日单根 bar → 0.0），其余字段照常算出
    assert watcher._minute_state["vwap_sigma"] == pytest.approx(0.0)
    assert watcher._minute_state["atr"] == pytest.approx(2.0)
    assert watcher._minute_state["trend_score"] is not None

