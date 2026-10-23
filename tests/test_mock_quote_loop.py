# -*- coding: utf-8 -*-
"""DATA_SOURCE=mock：连本地回放服务器取 tick。

盘中可测时间太短，收盘后没有 tick → BarBuilder 不出 bar → 没有 trend_score →
闸门/买入/持仓全链路都动不了。加这一路让任何时候都能跑通全流程。
"""
import json
from datetime import datetime

from app.watcher.quote_loops import _mock_extract_ticks


TS = "2026-07-24T09:30:00-04:00"
DT = datetime.fromisoformat(TS)


def test_parses_single_tick():
    assert _mock_extract_ticks(json.dumps({
        "sym": "QQQ", "price": 500.5, "volume": 250, "timestamp": TS,
    })) == [("QQQ", 500.5, 250, DT)]


def test_volume_defaults_to_zero_when_missing():
    assert _mock_extract_ticks(json.dumps({
        "sym": "QQQ", "price": 500.5, "timestamp": TS,
    })) == [("QQQ", 500.5, 0, DT)]


def test_skips_tick_without_price():
    """缺 price 的消息必须跳过——喂 0 进去会造出 low=0 的毒 bar（2026-07 接新数据源时踩过）。"""
    assert _mock_extract_ticks(json.dumps({"sym": "QQQ"})) == []


def test_skips_zero_price():
    assert _mock_extract_ticks(json.dumps({
        "sym": "QQQ", "price": 0, "timestamp": TS,
    })) == []
    assert _mock_extract_ticks(json.dumps({
        "sym": "QQQ", "price": "0", "timestamp": TS,
    })) == []


def test_skips_tick_without_symbol():
    assert _mock_extract_ticks(json.dumps({"price": 500.5, "timestamp": TS})) == []


def test_bad_json_yields_nothing_instead_of_raising():
    """脏帧不得让整条 WS 循环崩掉。"""
    assert _mock_extract_ticks("not json at all") == []


def test_symbol_is_upcased():
    assert _mock_extract_ticks(json.dumps({
        "sym": "qqq", "price": 1.0, "volume": 1, "timestamp": TS,
    })) == [("QQQ", 1.0, 1, DT)]


def test_missing_timestamp_is_rejected_instead_of_using_wall_clock():
    assert _mock_extract_ticks(json.dumps({"sym": "QQQ", "price": 500.5})) == []


def test_naive_or_invalid_timestamp_is_rejected():
    assert _mock_extract_ticks(json.dumps({
        "sym": "QQQ", "price": 500.5, "timestamp": "2026-07-24T09:30:00",
    })) == []
    assert _mock_extract_ticks(json.dumps({
        "sym": "QQQ", "price": 500.5, "timestamp": "not-a-time",
    })) == []
