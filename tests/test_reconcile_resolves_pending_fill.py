# -*- coding: utf-8 -*-
"""「对账」必须能救「券商已成交、系统还当未成交」这种仓。

2026-08-25 现场：QQQ 认沽 23:06:34.551 就在券商成交了，但 watchdog 卡死，
登记表里那笔仓的状态一直停在 `pending`。而持仓总览按设计把 `pending` 藏起来
（见 `exec_reconcile.list_positions` 的注释），于是这笔真实存在的仓在界面上
完全不存在 —— 看不到、没有卖出按钮、F 快捷键也点不到。

用户点了「对账」，没有用。原因是 `reconcile()` 只找「券商有、登记表完全没有」的孤儿仓：

    known = {p.option_code for p in REGISTRY.active()}   # ← 含 pending
    orphans = [bp for bp in broker_positions if bp.symbol not in known]
    if not orphans: return ...                            # ← 直接返回

**恰恰因为系统"认识"这笔单（登记着，只是状态没更新），对账才认定没事可做。**
反过来如果系统压根不知道有这单，对账倒能找回来。这是个正好卡在中间的盲区。

注：`reconcile_pending_journal()` 虽然会拿在途买单去问券商，但它按自己的注释
「只动 JOURNAL 展示 + DB 落库状态，不碰 REGISTRY/持仓」，救不了持仓总览，
而且只在进程启动时调一次，没有任何按钮能触发。
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

import app.exec_reconcile as m
import app.trade_recorder as trade_recorder
from app.order_state import JOURNAL, REGISTRY, ManagedPosition

_CODE = "QQQ260825P00709000"


def _src():
    # alpaca=None → 报价取不到，quote 退化为 None；持仓可见性不受报价影响
    return SimpleNamespace(name="watcher", alpaca=None)


def _seed_pending(qty=4, entry_mid="1.24"):
    """还原现场：券商已成交，本地登记表还停在「未成交」。"""
    REGISTRY._pos.clear()
    JOURNAL.clear()
    pos = ManagedPosition(
        option_code=_CODE, ticker="QQQ", direction="short", qty=qty,
        entry_mid=D(entry_mid), entry_time=datetime.now(timezone.utc),
        order_id="e1", status="pending",
    )
    REGISTRY.record(pos)
    JOURNAL.submitted("e1", "QQQ", _CODE, "buy", qty, price=D(entry_mid))
    return pos


class _FakeEc:
    """券商侧：持仓真实存在，订单也确实成交了。"""

    def __init__(self, order, broker_qty=4):
        self._order = order
        self._broker_qty = broker_qty
        self.get_order_calls: list[str] = []

    def broker_positions(self):
        return [SimpleNamespace(symbol=_CODE, qty=str(self._broker_qty),
                                avg_entry_price="1.24")]

    def get_order(self, order_id):
        self.get_order_calls.append(order_id)
        if isinstance(self._order, Exception):
            raise self._order
        return self._order


@pytest.fixture(autouse=True)
def _no_db(monkeypatch):
    monkeypatch.setattr(trade_recorder, "record_filled", lambda *a, **k: None)
    monkeypatch.setattr(trade_recorder, "record_position_sync", lambda *a, **k: None)
    yield
    REGISTRY._pos.clear()
    JOURNAL.clear()


def test_对账把已成交的在途仓转成持仓(monkeypatch):
    """这是用户点「对账」时真正想要的结果：那笔仓出现在持仓总览里。"""
    pos = _seed_pending()
    ec = _FakeEc({"status": "filled", "filled_qty": "4", "filled_avg_price": "1.24"})
    monkeypatch.setattr(m, "AlpacaTrading", lambda: ec)

    result = m.reconcile(_src())

    assert pos.status == "open", "对账后仍是 pending，持仓总览照样看不到"
    assert pos.entry_mid == D("1.24")
    assert "e1" in ec.get_order_calls, "对账根本没去问券商这单成交了没"

    visible = m.list_positions(_src(), include_unmanaged=False)["positions"]
    assert [p["option_code"] for p in visible] == [_CODE], "持仓总览还是空的"
    assert result["resolved_pending"] == [
        {"option_code": _CODE, "qty": 4, "entry_mid": "1.24"}
    ]


def test_券商说还在排队就保持未成交(monkeypatch):
    """真没成交的单不能被对账"变"成持仓，否则会凭空多出一笔不存在的仓。"""
    pos = _seed_pending()
    ec = _FakeEc({"status": "new", "filled_qty": "0", "filled_avg_price": None})
    monkeypatch.setattr(m, "AlpacaTrading", lambda: ec)

    result = m.reconcile(_src())

    assert pos.status == "pending"
    assert result["resolved_pending"] == []
    assert m.list_positions(_src(), include_unmanaged=False)["positions"] == []


def test_查单失败不得臆断成交(monkeypatch):
    """网络抖动 ≠ 成交。查不到就保持原样，下一次对账/watchdog 再核。"""
    pos = _seed_pending()
    ec = _FakeEc(ConnectionError("('Connection aborted.', RemoteDisconnected(...))"))
    monkeypatch.setattr(m, "AlpacaTrading", lambda: ec)

    result = m.reconcile(_src())

    assert pos.status == "pending", "查单失败却把仓标成了 open"
    assert result["resolved_pending"] == []


def test_watchdog抢先处理时对账不重复上报(monkeypatch):
    """`ec.get_order()` 那发 REST 要走网络往返（实测 200~500ms），期间 watchdog
    完全可能已经落地同一笔成交。

    此时再调 `_apply_entry_fill` 会走「非 pending」分支并**照样返回 True** ——
    不重新看一眼状态就会谎报一笔「对账补上的成交」。已实测：去掉那句状态复查，
    这里就变成 `resolved_pending=['QQQ260825P00709000']`。
    （`record_filled` 那条是附带的弱守卫：它的 SQL 带 `and status='pending'`，
    本来就不会重复更新行，两种写法下都只调一次。）
    """
    pos = _seed_pending()
    filled = {"status": "filled", "filled_qty": "4", "filled_avg_price": "1.24"}
    calls: list = []
    monkeypatch.setattr(trade_recorder, "record_filled",
                        lambda *a, **k: calls.append(a))

    class _RacingEc(_FakeEc):
        def get_order(self, order_id):
            # 模拟：查单在途期间 watchdog 已经把这笔仓转成 open
            from app.watchdog_order_sync import _apply_entry_fill
            _apply_entry_fill(pos, filled)
            return super().get_order(order_id)

    ec = _RacingEc(filled)
    monkeypatch.setattr(m, "AlpacaTrading", lambda: ec)

    result = m.reconcile(_src())

    assert pos.status == "open"          # watchdog 那次是有效的
    assert result["resolved_pending"] == [], "对账把别人干的活算成了自己的"
    assert len(calls) == 1, f"同一笔成交落库了 {len(calls)} 次"


def test_没有在途仓时对账行为不变(monkeypatch):
    """已有的孤儿仓找回逻辑不能被这一步影响。"""
    REGISTRY._pos.clear()
    JOURNAL.clear()
    ec = _FakeEc({"status": "filled", "filled_qty": "4", "filled_avg_price": "1.24"})
    monkeypatch.setattr(m, "AlpacaTrading", lambda: ec)
    monkeypatch.setattr(trade_recorder, "load_today", lambda: [])
    monkeypatch.setattr(trade_recorder, "load_order_events", lambda *a: [])

    result = m.reconcile(_src())

    assert ec.get_order_calls == [], "没有在途仓却去问了券商"
    assert result["resolved_pending"] == []
    assert result["recovered"] == []
