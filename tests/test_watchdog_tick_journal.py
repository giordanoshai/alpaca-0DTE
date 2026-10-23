# -*- coding: utf-8 -*-
"""_watchdog_tick：#9 系统强制平仓（watchdog 止损/触墙止盈）与对账检测到的外部平仓，
都要在订单流（JOURNAL）留一条带 reason 的卖单记录——此前完全没有，用户只看到仓位消失、
不知道是谁平的/为什么。只测这次新增的 JOURNAL 记录行为，不复现整条止损判定逻辑。
"""
from datetime import datetime, timezone
from decimal import Decimal as D
from types import SimpleNamespace

import app.watchdog as m
from app.order_state import JOURNAL, REGISTRY, ManagedPosition


def _seed(status="open"):
    REGISTRY._pos.clear()
    JOURNAL.clear()
    p = ManagedPosition(option_code="QQQ260716C00500000", ticker="QQQ", direction="long",
                        qty=2, entry_mid=D("1.00"), entry_time=datetime.now(timezone.utc),
                        order_id="e1", status=status)
    REGISTRY.record(p)
    return p


def _src():
    return SimpleNamespace(name="watcher", alpaca=SimpleNamespace(option_quote=lambda code: None),
                           underlying_price=lambda t: None)


def test_reconcile_external_close_records_journal_entry(monkeypatch):
    """券商侧已无此仓（非本系统 closing 中）→ external_closed，订单流留痕。"""
    p = _seed(status="open")
    monkeypatch.setattr(m, "AlpacaTrading", lambda: SimpleNamespace(broker_position_qtys=lambda: {}))
    monkeypatch.setattr(m.trade_recorder, "record_closed", lambda *a, **k: None)
    m._watchdog_tick(_src())
    assert p.status == "external_closed"
    ev = JOURNAL._events.get("e1:ext")
    assert ev is not None
    assert ev.side == "sell" and ev.reason == "external_closed" and ev.status == "filled"
    REGISTRY._pos.clear()
    JOURNAL.clear()


def test_reconcile_does_not_duplicate_close_worker_sell(monkeypatch):
    """closing 且尚无挂单号表示 close_run 正在提交；watchdog 不得抢先伪造卖单。"""
    p = _seed(status="closing")
    monkeypatch.setattr(m, "AlpacaTrading", lambda: SimpleNamespace(broker_position_qtys=lambda: {}))
    monkeypatch.setattr(m.trade_recorder, "record_closed", lambda *a, **k: None)
    m._watchdog_tick(_src())
    assert p.status == "closing"
    assert JOURNAL._events.get("e1:ext") is None
    REGISTRY._pos.clear()
    JOURNAL.clear()


def test_watchdog_close_records_journal_entry_with_reason(monkeypatch):
    """watchdog 止损三条线触发平仓 → 订单流留一条 reason 前缀 'watchdog:' 的卖单。"""
    p = _seed(status="open")
    broker = {p.option_code: p.qty}      # 券商张数与本地一致，不触发张数校正
    monkeypatch.setattr(m, "AlpacaTrading", lambda: SimpleNamespace(broker_position_qtys=lambda: broker))
    monkeypatch.setattr(m.OPTION_STREAM, "quote", lambda code: (D("0.10"), D("0.12")))
    monkeypatch.setattr(m, "watchdog_check", lambda *a, **k: (True, "超时 15分"))
    monkeypatch.setattr(m, "atomic_close",
                        lambda *a, **k: {
                            "closed": True,
                            "filled_qty": 2,
                            "fill": "0.11",
                            "order": {"id": "sell1"},
                        })
    monkeypatch.setattr(m.trade_recorder, "record_closed", lambda *a, **k: None)
    m._watchdog_tick(_src())
    assert p.status == "closed"
    ev = JOURNAL._events.get("sell1")
    assert ev is not None
    assert ev.side == "sell" and ev.reason == "watchdog:超时 15分" and ev.status == "filled"
    assert ev.triggered_at is not None
    assert ev.stop_mode == "percent"
    assert ev.stop_threshold == D("0.20")
    assert ev.entry_value == D("200")
    REGISTRY._pos.clear()
    JOURNAL.clear()


def test_watchdog_close_not_filled_records_no_journal_entry(monkeypatch):
    """追单未成交时不留成交记录，并锁存保护态供下一拍继续退出。"""
    p = _seed(status="open")
    broker = {p.option_code: p.qty}      # 券商张数与本地一致，不触发张数校正
    monkeypatch.setattr(m, "AlpacaTrading", lambda: SimpleNamespace(broker_position_qtys=lambda: broker))
    monkeypatch.setattr(m.OPTION_STREAM, "quote", lambda code: (D("0.10"), D("0.12")))
    monkeypatch.setattr(m, "watchdog_check", lambda *a, **k: (True, "超时 15分"))
    monkeypatch.setattr(m, "atomic_close", lambda *a, **k: {"closed": False, "error": "未成交"})
    m._watchdog_tick(_src())
    assert p.status == "closing"
    assert p.risk_exit_state == "closing_remaining"
    assert len(JOURNAL._events) == 0
    REGISTRY._pos.clear()
    JOURNAL.clear()
