# -*- coding: utf-8 -*-
"""DATA_SOURCE=mock 的历史预热必须来自回放服务器，且不得污染生产缓存。"""

import asyncio

import pandas as pd

import app.core.data_manager as data_manager
from app.core.data_manager import DataManager


def _warmup_df():
    idx = pd.date_range(
        "2026-07-21 09:30", periods=120, freq="1min", tz="America/New_York",
        name="datetime",
    )
    return pd.DataFrame({
        "open": range(120),
        "high": range(1, 121),
        "low": range(120),
        "close": range(1, 121),
        "volume": [1000] * 120,
    }, index=idx)


def test_download_history_mock_reads_previous_day_payload(monkeypatch):
    monkeypatch.setenv("DATA_SOURCE", "mock")
    captured = {}

    def fake_fetch(url, timeout):
        captured["url"] = url
        captured["timeout"] = timeout
        return {
            "symbol": "QQQ",
            "replay_date": "2026-07-22",
            "warmup_date": "2026-07-21",
            "bars": [
                {
                    "timestamp": "2026-07-21T09:30:00-04:00",
                    "open": 500.0,
                    "high": 501.0,
                    "low": 499.0,
                    "close": 500.5,
                    "volume": 1000,
                }
            ],
        }

    monkeypatch.setattr(data_manager, "_fetch_json_no_proxy", fake_fetch)
    df = DataManager("QQQ").download_history(period="2d")

    assert captured["url"] == "http://127.0.0.1:9000/sim/history/QQQ"
    assert captured["timeout"] == 3.0
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert str(df.index.tz) == "America/New_York"
    assert str(df.index[0].date()) == "2026-07-21"


def test_mock_history_url_can_be_overridden(monkeypatch):
    monkeypatch.setenv("DATA_SOURCE", "mock")
    monkeypatch.setenv("MOCK_HISTORY_URL", "http://localhost:9100/custom/")
    captured = {}

    def fake_fetch(url, timeout):
        captured["url"] = url
        return {"bars": []}

    monkeypatch.setattr(data_manager, "_fetch_json_no_proxy", fake_fetch)
    DataManager("SPY").download_history()
    assert captured["url"] == "http://localhost:9100/custom/SPY"


def test_mock_warmup_skips_production_cache_load_and_save(monkeypatch):
    monkeypatch.setenv("DATA_SOURCE", "mock")
    dm = DataManager("QQQ")
    df = _warmup_df()

    monkeypatch.setattr(
        dm, "load_cache",
        lambda: (_ for _ in ()).throw(AssertionError("mock 不应加载生产缓存")),
    )
    monkeypatch.setattr(dm, "download_history", lambda **kwargs: df)
    monkeypatch.setattr(
        dm, "save_cache",
        lambda: (_ for _ in ()).throw(AssertionError("mock 不应保存生产缓存")),
    )

    assert asyncio.run(dm.warmup()) is True
    assert dm.bar_count == 120


def test_save_cache_is_noop_in_mock_mode(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_SOURCE", "mock")
    dm = DataManager("QQQ")
    dm.cache_file = tmp_path / "QQQ_1m.parquet"
    dm._df_1m = _warmup_df()

    assert dm.save_cache() is True
    assert not dm.cache_file.exists()


def test_fill_gap_is_noop_in_mock_mode(monkeypatch):
    monkeypatch.setenv("DATA_SOURCE", "mock")
    dm = DataManager("QQQ")
    monkeypatch.setattr(
        dm, "download_history",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("mock 断线不应拉生产历史")),
    )

    now = pd.Timestamp("2026-07-22 10:00", tz="America/New_York").to_pydatetime()
    assert asyncio.run(dm.fill_gap(now, now)) is False
