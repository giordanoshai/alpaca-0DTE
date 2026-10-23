# -*- coding: utf-8 -*-
"""止损按**可变现价值(bid)**计价，不是 mid（审查 3.1）。

`watchdog_check` 原来用 `(bid+ask)/2 × 100 × qty` 算浮亏，但真实平仓成交在 bid 附近。
0DTE 宽 spread 上，mid 显示还没到止损线、实际能拿回来的钱已经击穿了 —— 触发止损那一刻
的账面亏损被系统性低估，止损偏松。配合 3.2（entry_mid 回填真实成交价 ≈ ask），
现在 entry_val→current_val 的比较就是**真实的往返盈亏**：买入付出的 vs 现在能卖回的。
"""
from datetime import datetime, timezone
from decimal import Decimal as D
from types import SimpleNamespace

import app.watchdog as m
from app.order_state import JOURNAL, REGISTRY, ManagedPosition

_CODE = "QQQ260716C00500000"


def _src():
    return SimpleNamespace(name="watcher",
                           alpaca=SimpleNamespace(option_quote=lambda code, fresh=False: None),
                           underlying_price=lambda t: None)


def _ec():
    return SimpleNamespace(broker_position_qtys=lambda: {_CODE: 1},
                           get_order=lambda oid: {}, cancel=lambda oid: None)


def _seed(
    entry_mid="1.00",
    stop_pct="0.20",
    dollar_line="1000",
    stop_mode="percent",
    max_hold_s=99999,
):
    REGISTRY._pos.clear(); JOURNAL.clear()
    p = ManagedPosition(option_code=_CODE, ticker="QQQ", direction="long", qty=1,
                        entry_mid=D(entry_mid), entry_time=datetime.now(timezone.utc),
                        order_id="e1", status="open",
                        stop_pct=D(stop_pct), dollar_line=D(dollar_line),
                        stop_mode=stop_mode, max_hold_s=max_hold_s)
    REGISTRY.record(p)
    return p


def _run(monkeypatch, quote):
    """跑一拍，返回是否触发了平仓。"""
    monkeypatch.setattr(m, "AlpacaTrading", _ec)
    monkeypatch.setattr(m.OPTION_STREAM, "quote", lambda code: quote)
    monkeypatch.setattr(m.trade_recorder, "record_closed", lambda *a, **k: None)
    fired = {"hit": False}

    def fake_close(*a, **k):
        fired["hit"] = True
        return {"closed": True, "fill": "0.78", "order": {"id": "s1"}}

    monkeypatch.setattr(m, "atomic_close", fake_close)
    m._watchdog_tick(_src())
    return fired["hit"]


def test_stop_fires_on_bid_even_when_mid_looks_safe(monkeypatch):
    """入场 1.00、止损 20%。bid 0.78 / ask 0.88 → mid 0.83 只亏 17%（看着安全），
    但真要卖只能拿回 0.78 = 亏 22%，已经击穿。必须触发。"""
    _seed(entry_mid="1.00", stop_pct="0.20")
    assert _run(monkeypatch, (D("0.78"), D("0.88"))) is True
    REGISTRY._pos.clear()


def test_stop_holds_when_bid_within_threshold(monkeypatch):
    """bid 0.85 = 亏 15% < 20% → 不触发（没有变得过度敏感）。"""
    _seed(entry_mid="1.00", stop_pct="0.20")
    assert _run(monkeypatch, (D("0.85"), D("0.95"))) is False
    REGISTRY._pos.clear()


def test_dollar_line_also_uses_bid(monkeypatch):
    """$金额线同口径：入场 $100(1张×1.00)，bid 0.20 → 可变现 $20、亏 $80 ≥ 线 $80 → 触发。
    若按 mid 0.25 算只亏 $75，不到线。"""
    _seed(entry_mid="1.00", stop_pct="0.99", dollar_line="80", stop_mode="amount")
    assert _run(monkeypatch, (D("0.20"), D("0.30"))) is True
    REGISTRY._pos.clear()


def test_profitable_position_not_stopped(monkeypatch):
    """盈利中（bid 1.30 > 入场 1.00）显然不触发止损。"""
    _seed(entry_mid="1.00", stop_pct="0.20")
    assert _run(monkeypatch, (D("1.30"), D("1.40"))) is False
    REGISTRY._pos.clear()


def test_time_exit_does_not_require_an_option_quote(monkeypatch):
    """时间纪律到点必须平仓；WS/REST 同时无报价也不能把持仓无限延长。"""
    p = _seed(max_hold_s=0)
    seen = {}
    monkeypatch.setattr(m, "AlpacaTrading", _ec)
    monkeypatch.setattr(m.OPTION_STREAM, "quote", lambda code: None)
    monkeypatch.setattr(m.trade_recorder, "record_closed", lambda *a, **k: None)

    def fake_close(*args, **kwargs):
        seen.update(kwargs)
        return {
            "closed": True,
            "filled_qty": 1,
            "fill": "0.80",
            "order": {"id": "s1"},
        }

    monkeypatch.setattr(m, "atomic_close", fake_close)
    m._watchdog_tick(_src())

    assert p.status == "closed"
    assert seen["trigger_quote"] is None
    REGISTRY._pos.clear()
