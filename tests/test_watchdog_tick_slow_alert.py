# -*- coding: utf-8 -*-
"""watchdog 一拍跑太久必须留下痕迹。

2026-08-25 那次卡死之所以只能靠 8 分 48 秒后的 `RemoteDisconnected` 事后倒推，
是因为卡住期间**一条日志都没有**：`trade_rest_reconciled` 是在一拍**跑完之后**才打的，
一拍没跑完就永远不打，看上去像「什么都没发生」。

历史日志里同形态的静默共 13 次（08-05 26 分钟、08-10 55 分钟、08-14 16.9 分钟……），
每一次都要靠"心跳断档"去反推。加一条超时告警之后，下次能直接看出卡了多久。
"""
from __future__ import annotations

import asyncio
import logging
import time
from types import SimpleNamespace

import app.watchdog as m


def _src():
    return SimpleNamespace(name="watcher", alpaca=None, underlying_price=lambda t: None)


def test_一拍超过阈值必须告警并报出耗时(monkeypatch, caplog):
    """卡住多久要能一眼看到，否则只能靠心跳断档反推。"""
    monkeypatch.setattr(m, "WATCHDOG_TICK_SLOW_S", 0.05)
    monkeypatch.setattr(m, "_watchdog_tick", lambda *a, **k: time.sleep(0.2))

    with caplog.at_level(logging.WARNING, logger="tt.gate"):
        asyncio.run(m._run_watchdog_tick(_src(), reconcile_rest=True, order_updates={}))

    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "watchdog_tick_slow" in text, f"一拍超时没有告警：{text}"
    assert "elapsed_s" in text, f"告警没报耗时，看不出卡了多久：{text}"


def test_正常一拍不打扰(monkeypatch, caplog):
    """一秒一拍的循环，正常情况绝不能刷屏。"""
    monkeypatch.setattr(m, "WATCHDOG_TICK_SLOW_S", 5.0)
    monkeypatch.setattr(m, "_watchdog_tick", lambda *a, **k: None)

    with caplog.at_level(logging.WARNING, logger="tt.gate"):
        asyncio.run(m._run_watchdog_tick(_src(), reconcile_rest=True, order_updates={}))

    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "watchdog_tick_slow" not in text, f"正常一拍不该告警：{text}"


def test_一拍抛异常也要报耗时(monkeypatch, caplog):
    """卡到被对端掐断（RemoteDisconnected）正是现场的样子，那一拍同样要留痕。"""
    monkeypatch.setattr(m, "WATCHDOG_TICK_SLOW_S", 0.05)

    def _boom(*a, **k):
        time.sleep(0.2)
        raise ConnectionError("('Connection aborted.', RemoteDisconnected(...))")

    monkeypatch.setattr(m, "_watchdog_tick", _boom)

    with caplog.at_level(logging.WARNING, logger="tt.gate"):
        try:
            asyncio.run(m._run_watchdog_tick(_src(), reconcile_rest=True, order_updates={}))
        except ConnectionError:
            pass  # 异常照旧向上抛给循环的兜底 except，这里只关心有没有留下耗时

    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "watchdog_tick_slow" in text, f"卡住后抛异常的那一拍没留痕：{text}"
