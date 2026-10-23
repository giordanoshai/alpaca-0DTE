# -*- coding: utf-8 -*-
"""重启预热：断层必须补齐后再返回，第一份状态不能算在旧数据上。

现场（2026-08-28 21:44 崩溃 → 21:45:53 重启）：K 线缓存只在「启动补齐完 / 后台补齐完 /
优雅退出」三处写盘，崩溃没走优雅退出，磁盘缓存停在 09:27（美东，开盘前 3 分钟）。
重启时 `warmup()` 看缓存够长就立刻返回成功、把补齐扔进后台，`TickerWatcher.start()`
紧接着拿这份 09:27 的旧数据算了第一份每分钟状态。而 Yahoo 盘前分钟线成交量恒为 0，
流动性硬闸（liquidity.data_quality_ok，最近 30 根成交量中位数 ≥ 50）当场判 False ——
13 只票全部「盯盘：暂不可交易」，真下单也会被 market_context 抛「watcher 指示当前不可交易」
拦掉，直到第一根盘中 1m K 线收掉才自愈。

修法：补齐任务照建，但**等它跑完再返回**（最多 WARMUP_FILL_WAIT_S 秒，卡住就留给后台，
不阻断启动）。
"""
from __future__ import annotations

import asyncio
import time

import pandas as pd

import app.core.data_manager as data_manager
from app.core.data_manager import DataManager

ET = "America/New_York"


def _frame(end: str, periods: int = 200) -> pd.DataFrame:
    """一段到 ``end`` 为止的 1m 线（根数够过 MIN_WARMUP_BARS）。"""
    idx = pd.date_range(end=pd.Timestamp(end, tz=ET), periods=periods, freq="1min",
                        name="datetime")
    return pd.DataFrame({
        "open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0, "volume": 1000,
    }, index=idx)


class _StubClock:
    def __init__(self, now: str):
        self._now = pd.Timestamp(now, tz=ET)

    def get_ntp_time(self):
        return self._now


def _manager(monkeypatch, *, now: str, cached_end: str) -> DataManager:
    monkeypatch.setenv("DATA_SOURCE", "yahoo")
    dm = DataManager("SPY", _StubClock(now))

    def _load_cache() -> bool:
        dm._df_1m = _frame(cached_end)
        return True

    monkeypatch.setattr(dm, "load_cache", _load_cache)
    monkeypatch.setattr(dm, "save_cache", lambda: True)
    return dm


def test_预热返回时断层已补齐(monkeypatch):
    """崩溃重启的现场：缓存停在 09:27，现在 09:46。"""
    dm = _manager(monkeypatch, now="2026-08-28 09:46", cached_end="2026-08-28 09:27")
    filled = {"done": False}

    async def _fill_gap(gap_start, gap_end):
        await asyncio.sleep(0)          # 真实下载必有 await 点，别让协程一口气跑完
        dm._df_1m = _frame("2026-08-28 09:45")
        filled["done"] = True
        return True

    monkeypatch.setattr(dm, "fill_gap", _fill_gap)

    assert asyncio.run(dm.warmup()) is True
    # warmup 返回的那一刻，watcher 就要拿它算第一份状态 —— 此时断层必须已经补上
    assert filled["done"] is True
    assert dm._df_1m.index[-1].strftime("%H:%M") == "09:45"


def test_补齐卡住不阻断启动(monkeypatch):
    """下载卡死时只等到上限就放行，剩下的留给后台 —— 启动不能被行情源拖住。"""
    dm = _manager(monkeypatch, now="2026-08-28 09:46", cached_end="2026-08-28 09:27")
    monkeypatch.setattr(data_manager, "WARMUP_FILL_WAIT_S", 0.05, raising=False)

    async def _hang(gap_start, gap_end):
        await asyncio.sleep(30)
        return True

    monkeypatch.setattr(dm, "fill_gap", _hang)

    async def _run():
        started = time.monotonic()
        ok = await dm.warmup()
        return ok, time.monotonic() - started

    ok, elapsed = asyncio.run(_run())
    assert ok is True
    assert elapsed < 5.0


def test_缓存已是最新时不等待(monkeypatch):
    """没有断层就不该建补齐任务，更不该白等。"""
    dm = _manager(monkeypatch, now="2026-08-28 09:46", cached_end="2026-08-28 09:46")
    called = {"n": 0}

    async def _fill_gap(gap_start, gap_end):
        called["n"] += 1
        return True

    monkeypatch.setattr(dm, "fill_gap", _fill_gap)

    assert asyncio.run(dm.warmup()) is True
    assert called["n"] == 0
