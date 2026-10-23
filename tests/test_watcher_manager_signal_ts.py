# -*- coding: utf-8 -*-
"""WatcherManager.record_signal：#4/#5 票池排序+信号定位打点的支撑数据结构。"""
from app.watcher.watcher_manager import WatcherManager


def test_record_signal_sets_timestamp():
    m = WatcherManager()
    assert m.last_signal_ts == {}
    m.record_signal("qqq")
    assert "QQQ" in m.last_signal_ts          # 统一大写
    assert m.last_signal_ts["QQQ"] > 0


def test_record_signal_overwrites_with_latest():
    m = WatcherManager()
    m.record_signal("QQQ")
    first = m.last_signal_ts["QQQ"]
    m.record_signal("QQQ")
    assert m.last_signal_ts["QQQ"] >= first
