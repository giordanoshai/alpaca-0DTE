# -*- coding: utf-8 -*-
"""跨夜缺口：服务改 24 小时常驻之后，收盘到开盘之间的那一段没人补。

现场（2026-09-03 09:42 ET，线上 https://tt.ggwp.cn:8011 拉到的真实 300 根 1m 线）：

    09-02 15:17 → 15:59   43 根   昨天收盘前，成交量正常
    09-02 16:00 → 19:59  239 根   昨天盘后，成交量全是 0
    09-02 19:59 → 09-03 09:25     空洞 806 分钟，一根都没有
    09-03 09:25 → 09:42   18 根   今天，从行情窗口开门那一刻起算

今天盘前 04:00–09:24 一根都没有，今天的 bar 全是实时报价流现攒的 —— 历史补齐一次
都没参与。后果不只是卡片图上那道断崖：EMA9/21 与 MACD 里塞着 239 根昨天盘后几乎
不动的 bar，而入场判据看的就是多周期均线同向；ATR 与效率比走的同一个 300 根窗口，
开盘头几十分钟一并失真。

两个缺陷叠加，且都只在「服务常驻、不再每天手动启停」之后才同时生效：

1. `quote_loops` 非盘中只是 sleep 后 continue，一到窗口开门直接重连吃 tick；
   `_fill_gaps` 只挂在 WebSocket 报错那条分支上，而跨夜是正常流程、不报错。
2. 就算去补也补不到今天：`_download_history_yf` 把起止时间降级成日期字符串传给
   yfinance，而 yfinance 的 `end` 是**右开区间**，`end='2026-09-03'` 返回的最后
   一天是 09-02。所以缓存里出现「昨天盘后特别全、今天盘前完全没有」这个组合。
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pandas as pd

import app.core.data_manager as data_manager
import app.watcher.quote_loops as quote_loops
from app.core import market_gate
from app.core.data_manager import DataManager
from app.watcher.quote_loops import QuoteLoopsMixin

ET = "America/New_York"


class _StubClock:
    def __init__(self, now: str):
        self._now = pd.Timestamp(now, tz=ET)

    def get_ntp_time(self):
        return self._now


def _stub_yf(monkeypatch, seen: dict):
    """把 yfinance 换成只记参数的假货，返回空表即可（本组只验请求区间）。"""
    class _Ticker:
        def __init__(self, _symbol):
            pass

        def history(self, **kwargs):
            seen.update(kwargs)
            return pd.DataFrame()

    monkeypatch.setattr(data_manager, "yf", SimpleNamespace(Ticker=_Ticker))


def test_补齐区间必须覆盖到今天(monkeypatch):
    """昨晚 19:59 → 今早 09:25 的缺口，请求区间必须真的含今天。"""
    monkeypatch.setenv("DATA_SOURCE", "yahoo")
    seen: dict = {}
    _stub_yf(monkeypatch, seen)
    dm = DataManager("SPY", _StubClock("2026-09-03 09:25"))

    dm.download_history(
        start=pd.Timestamp("2026-09-02 19:59", tz=ET).to_pydatetime(),
        end=pd.Timestamp("2026-09-03 09:25", tz=ET).to_pydatetime(),
    )

    # yfinance 的 end 右开：写 09-03 拿回来的最后一天是 09-02，今天一根都没有
    assert seen.get("end") == "2026-09-04", (
        f"请求区间没盖住今天：start={seen.get('start')} end={seen.get('end')}")
    assert seen.get("start") <= "2026-09-03"
    assert seen.get("prepost") is True


def test_同一天内的缺口也要拿到今天(monkeypatch):
    """盘中断线重连的同日缺口：原先靠 period='1d' 兜，改区间后也不能丢。"""
    monkeypatch.setenv("DATA_SOURCE", "yahoo")
    seen: dict = {}
    _stub_yf(monkeypatch, seen)
    dm = DataManager("SPY", _StubClock("2026-09-03 11:00"))

    dm.download_history(
        start=pd.Timestamp("2026-09-03 10:30", tz=ET).to_pydatetime(),
        end=pd.Timestamp("2026-09-03 11:00", tz=ET).to_pydatetime(),
    )

    end = seen.get("end") or ""
    period = seen.get("period")
    assert period == "1d" or end >= "2026-09-04", (
        f"同日缺口的请求既不是 period=1d 也没盖住今天：{seen}")


class _Loops(QuoteLoopsMixin):
    """只装得下窗口判定所需的最小宿主（真身是 WatcherManager）。"""

    def __init__(self):
        self._running = True
        self.filled: list[str] = []

    async def _fill_gaps(self, source_name: str, tickers=None, **kwargs) -> None:
        self.filled.append(source_name)


def test_窗口从关到开必须补缺口(monkeypatch):
    """跨夜那一段：窗口开门的第一件事是补缺口，不是直接吃 tick。"""
    monkeypatch.setattr(quote_loops, "_CLOSED_RECHECK_S", 0)
    gate = iter([False, False, True])
    monkeypatch.setattr(market_gate, "data_window_open", lambda *a, **k: next(gate))
    loops = _Loops()

    async def _run():
        return [await loops._await_market_window("yahoo") for _ in range(3)]

    assert asyncio.run(_run()) == [False, False, True]
    assert loops.filled == ["yahoo"], "窗口开门时没补缺口"


def test_启动时正好在盘中不重复补(monkeypatch):
    """进程启动时窗口本来就开着：预热已经补过，这不算跳变。"""
    monkeypatch.setattr(market_gate, "data_window_open", lambda *a, **k: True)
    loops = _Loops()

    async def _run():
        return [await loops._await_market_window("yahoo") for _ in range(3)]

    assert asyncio.run(_run()) == [True, True, True]
    assert loops.filled == [], "盘中每一拍都补一次会把行情源打爆"


def _frame(end: str, periods: int = 240) -> pd.DataFrame:
    idx = pd.date_range(end=pd.Timestamp(end, tz=ET), periods=periods, freq="1min",
                        name="datetime")
    return pd.DataFrame({"open": 765.0, "high": 765.0, "low": 765.0,
                         "close": 765.0, "volume": 0}, index=idx)


def test_过夜重启后补齐起点取缓存最后一根(monkeypatch):
    """实时侧一根 bar 都还没有时，补缺口的起点必须退回缓存最后一根。

    `last_bar_time` 只在实时 tick 攒出 bar 时才赋值，表达的是「行情流活到哪一刻」，
    新鲜度展示（watcher_api）靠的就是这个语义，不能拿缓存去填。但补缺口问的是另一件
    事：「我手上的数据到哪儿为止」。服务 24 小时常驻，过夜或周末重启时实时侧是空的，
    若补齐还认 `last_bar_time`，`_fill_gaps` 会因为 None 直接跳过这只票 —— 窗口开门
    补缺口就成了空转。
    """
    monkeypatch.setenv("DATA_SOURCE", "yahoo")
    dm = DataManager("SPY", _StubClock("2026-09-03 09:25"))
    dm._df_1m = _frame("2026-09-02 19:59")

    assert dm.last_bar_time is None, "实时侧本来就该是空的"
    assert dm.last_known_bar_time == pd.Timestamp("2026-09-02 19:59", tz=ET)


def test_过夜重启后窗口开门不得空转(monkeypatch):
    """_fill_gaps 必须真给这只票排上补齐任务，而不是因为 last_bar_time is None 跳过。"""
    from app.watcher.watcher_manager import WatcherManager

    monkeypatch.setenv("DATA_SOURCE", "yahoo")
    dm = DataManager("SPY", _StubClock("2026-09-03 09:25"))
    dm._df_1m = _frame("2026-09-02 19:59")
    anchors: list = []

    async def _fill_gap(gap_start, gap_end):
        anchors.append(gap_start)
        return True

    monkeypatch.setattr(dm, "fill_gap", _fill_gap)
    host = SimpleNamespace(_watchers={"SPY": SimpleNamespace(data_manager=dm)})

    asyncio.run(WatcherManager._fill_gaps(host, "Yahoo", reason="行情窗口开门"))

    assert anchors == [pd.Timestamp("2026-09-02 19:59", tz=ET)], "跨夜缺口没排上补齐"
