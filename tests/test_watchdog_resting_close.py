# -*- coding: utf-8 -*-
"""_watchdog_tick 对「平仓挂单在券商排队」(pos.close_order_id 已置) 的异步确认：
按券商订单状态(get_order)定最终成交/撤销，不依赖 code not in broker（部分平仓时剩余仓仍在券商）。
- 成交(filled / filled_qty≥close_qty) → 全平置 closed / 部分平减仓回 open + 订单流卖单 filled + 落库
- 券商侧撤销/拒绝 → 回退 open + close_error + 订单流卖单 cancelled
- 仍挂单中(new/accepted) → 保持 closing，不落库、不动订单流状态
"""
from datetime import datetime, timezone
from decimal import Decimal as D
from types import SimpleNamespace

import app.watchdog as m
from app.order_state import JOURNAL, REGISTRY, ManagedPosition


def _seed(qty=2, close_qty=2, close_order_id="s1"):
    REGISTRY._pos.clear()
    JOURNAL.clear()
    p = ManagedPosition(option_code="QQQ260716C00500000", ticker="QQQ", direction="long",
                        qty=qty, entry_mid=D("1.00"), entry_time=datetime.now(timezone.utc),
                        order_id="e1", status="closing")
    p.close_order_id = close_order_id
    p.close_qty = close_qty
    p.close_reason = "手动平仓"
    REGISTRY.record(p)
    # 订单流里已有 close_run 记的 pending 卖单
    JOURNAL.submitted(close_order_id, p.ticker, p.option_code, "sell", close_qty, reason="手动平仓")
    return p


def _src():
    return SimpleNamespace(name="watcher",
                           alpaca=SimpleNamespace(option_quote=lambda code: None),
                           underlying_price=lambda t: None)


def _fake_ec(order, broker=None):
    # broker 传符号集（这些用例只关心"在不在"）→ 转成 watchdog 要的 {合约: 张数}。
    # 张数取 0：本文件的用例都停在 pending / 平仓挂单在途分支，走不到张数对账。
    qtys = {s: 0 for s in (broker or set())}
    return SimpleNamespace(
        broker_position_qtys=lambda: dict(qtys),
        cancel=lambda oid: None,
        get_order=lambda oid: order)


def test_resting_full_fill_sets_closed(monkeypatch):
    p = _seed(qty=2, close_qty=2)
    monkeypatch.setattr(m, "AlpacaTrading",
                        lambda: _fake_ec({"status": "filled", "filled_qty": "2",
                                          "filled_avg_price": "1.05"}))
    rec = {}
    monkeypatch.setattr(m.trade_recorder, "record_closed",
                        lambda oid, mid, q, r: rec.update(oid=oid, q=q, reason=r))
    m._watchdog_tick(_src())
    assert p.status == "closed"
    assert p.close_order_id is None and p.close_qty == 0
    assert rec == {"oid": "e1", "q": 2, "reason": "手动平仓"}
    ev = JOURNAL._events.get("s1")
    assert ev is not None and ev.status == "filled"
    REGISTRY._pos.clear(); JOURNAL.clear()


def test_resting_partial_fill_reduces_reopens(monkeypatch):
    p = _seed(qty=3, close_qty=1)
    monkeypatch.setattr(m, "AlpacaTrading",
                        lambda: _fake_ec({"status": "filled", "filled_qty": "1",
                                          "filled_avg_price": "1.05"},
                                         broker={p.option_code}))
    monkeypatch.setattr(m.trade_recorder, "record_closed", lambda *a, **k: None)
    m._watchdog_tick(_src())
    assert p.status == "open" and p.qty == 2
    assert p.realized_pnl == D("5.00")
    assert p.close_order_id is None
    ev = JOURNAL._events.get("s1")
    assert ev is not None and ev.status == "filled"
    REGISTRY._pos.clear(); JOURNAL.clear()


def test_resting_broker_cancelled_reverts_open(monkeypatch):
    p = _seed(qty=2, close_qty=2)
    monkeypatch.setattr(m, "AlpacaTrading",
                        lambda: _fake_ec({"status": "canceled", "filled_qty": "0",
                                          "filled_avg_price": None},
                                         broker={p.option_code}))
    rec = {"called": False}
    monkeypatch.setattr(m.trade_recorder, "record_closed",
                        lambda *a, **k: rec.update(called=True))
    m._watchdog_tick(_src())
    assert p.status == "open"
    assert p.close_order_id is None
    assert p.close_error and "未成交" in p.close_error
    assert rec["called"] is False
    ev = JOURNAL._events.get("s1")
    assert ev is not None and ev.status == "cancelled"
    REGISTRY._pos.clear(); JOURNAL.clear()


def test_resting_cancelled_after_partial_fill_deducts_sold(monkeypatch):
    """券商撤单终态但撤前已部分成交（filled_qty ∈ 1..close_qty-1）：已卖张数必须从
    REGISTRY 扣减并落库，否则持仓高估 → 后续全量平仓超卖报错。"""
    p = _seed(qty=3, close_qty=3)
    monkeypatch.setattr(m, "AlpacaTrading",
                        lambda: _fake_ec({"status": "canceled", "filled_qty": "2",
                                          "filled_avg_price": "1.05"},
                                         broker={p.option_code}))
    rec = {}
    monkeypatch.setattr(m.trade_recorder, "record_closed",
                        lambda oid, mid, q, r: rec.update(oid=oid, q=q))
    m._watchdog_tick(_src())
    assert p.status == "open" and p.qty == 1      # 已卖 2 张必须扣减
    assert p.realized_pnl == D("10.00")
    assert p.close_order_id is None and p.close_qty == 0
    assert rec == {"oid": "e1", "q": 2}
    assert p.close_error and "2" in p.close_error  # 提示已部分成交
    REGISTRY._pos.clear(); JOURNAL.clear()


def test_resting_still_working_stays_closing(monkeypatch):
    p = _seed(qty=2, close_qty=2)
    monkeypatch.setattr(m, "AlpacaTrading",
                        lambda: _fake_ec({"status": "accepted", "filled_qty": "0",
                                          "filled_avg_price": None},
                                         broker={p.option_code}))
    rec = {"called": False}
    monkeypatch.setattr(m.trade_recorder, "record_closed",
                        lambda *a, **k: rec.update(called=True))
    m._watchdog_tick(_src())
    assert p.status == "closing"                 # 仍挂单中
    assert p.close_order_id == "s1"
    assert rec["called"] is False
    ev = JOURNAL._events.get("s1")
    assert ev is not None and ev.status == "pending"
    REGISTRY._pos.clear(); JOURNAL.clear()
