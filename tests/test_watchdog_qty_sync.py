# -*- coding: utf-8 -*-
"""⑥ 持仓张数以 Alpaca 为准（审查 7.4③；QQQ 事件的系统性根因）。

此前 watchdog 的持仓对账**只比对合约在不在券商**（`code in broker`），从不比对张数。
于是「2 手卖 1 手、卖单没成交」这种情况完全看不见：合约一直在 broker 里，本地
REGISTRY 也一直写着自己推演出来的张数，两边可以长期不一致而无人发现。

危险不止于显示错：`atomic_close` 用 `target = 券商持仓 − 要卖的量` 判定成交，
本地 qty 错了会让"全平"卖错数量（少卖留仓 / 多卖超卖报错）。故一律以券商为准覆盖。

不覆盖的两种情形（会误判）：
  - pending：还没成交，券商本来就没有
  - 平仓挂单在途（close_order_id 已置）：部分成交期间两边本就应该不同，
    终态由 _finalize_resting_close / 撤单分支按券商订单 settle，此处插手会重复扣减
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from types import SimpleNamespace

import app.watchdog as m
from app.order_state import JOURNAL, REGISTRY, ManagedPosition

_CODE = "QQQ260716C00500000"


def _src():
    return SimpleNamespace(name="watcher",
                           alpaca=SimpleNamespace(option_quote=lambda code, fresh=False: None),
                           underlying_price=lambda t: None)


class _FakeEc:
    """券商侧持仓张数由 qtys 给定（缺席 = 券商无此仓）。"""

    def __init__(self, qtys=None, order=None):
        self._qtys = dict(qtys or {})
        self._order = order or {"status": "accepted", "filled_qty": "0"}

    def broker_position_qtys(self):
        return dict(self._qtys)

    def get_order(self, oid):
        return self._order

    def cancel(self, oid):
        pass


def _seed(qty, status="open", close_order_id=None):
    REGISTRY._pos.clear(); JOURNAL.clear()
    p = ManagedPosition(option_code=_CODE, ticker="QQQ", direction="long", qty=qty,
                        entry_mid=D("1.00"), entry_time=datetime.now(timezone.utc),
                        order_id="e1", status=status)
    if close_order_id:
        p.close_order_id = close_order_id
        p.close_qty = 1
        p.close_submitted_at = datetime.now(timezone.utc)
    REGISTRY.record(p)
    return p


def test_local_qty_follows_broker_when_lower(monkeypatch):
    """本地记 2 手、券商实际 1 手 → 覆盖为 1，并留下可见的同步说明。"""
    p = _seed(qty=2)
    monkeypatch.setattr(m, "AlpacaTrading", lambda: _FakeEc({_CODE: 1}))
    m._watchdog_tick(_src())
    assert p.qty == 1
    assert p.sync_note is not None and "1" in p.sync_note
    REGISTRY._pos.clear()


def test_recent_owned_add_fill_is_not_overwritten_by_stale_broker_snapshot(monkeypatch):
    """加仓成交刚把本地 2→3 时，同拍 REST 仍返回旧 2 张，不得把真实成交抹掉。"""
    p = _seed(qty=2)
    REGISTRY.add_qty(_CODE, 1, D("0.90"))
    assert p.qty == 3
    assert p.last_owned_buy_fill_at is not None
    monkeypatch.setattr(m, "AlpacaTrading", lambda: _FakeEc({_CODE: 2}))
    persisted = {}
    monkeypatch.setattr(
        m.trade_recorder,
        "record_position_sync",
        lambda oid, qty, new_entry_mid=None: persisted.update(
            order_id=oid, qty=qty, entry_mid=new_entry_mid,
        ),
    )

    m._watchdog_tick(_src())

    assert p.qty == 3
    assert p.sync_note is not None and "等待券商持仓快照追上" in p.sync_note
    assert persisted == {}
    REGISTRY._pos.clear()


def test_lower_broker_qty_is_applied_after_owned_fill_grace_expires(monkeypatch):
    """宽限期只屏蔽 REST 最终一致性窗口；过期后仍以券商真仓向下校正。"""
    p = _seed(qty=2)
    REGISTRY.add_qty(_CODE, 1, D("0.90"))
    p.last_owned_buy_fill_at = datetime.now(timezone.utc) - timedelta(
        seconds=m._BROKER_POSITION_SETTLE_GRACE_S + 1,
    )
    monkeypatch.setattr(m, "AlpacaTrading", lambda: _FakeEc({_CODE: 2}))

    m._watchdog_tick(_src())

    assert p.qty == 2
    assert p.sync_note is not None and "Alpaca 2" in p.sync_note
    REGISTRY._pos.clear()


def test_broker_extra_qty_is_not_absorbed_into_managed_position(monkeypatch):
    """券商同合约比本程序多 → 视为外部数量，不接管、不写生命周期。"""
    p = _seed(qty=1)
    monkeypatch.setattr(m, "AlpacaTrading", lambda: _FakeEc({_CODE: 3}))
    persisted = {}
    monkeypatch.setattr(
        m.trade_recorder,
        "record_position_sync",
        lambda oid, qty, new_entry_mid=None: persisted.update(
            order_id=oid, qty=qty, entry_mid=new_entry_mid,
        ),
    )
    m._watchdog_tick(_src())
    assert p.qty == 1
    assert p.sync_note is not None and "额外 2 张未接管" in p.sync_note
    assert persisted == {}
    REGISTRY._pos.clear()


def test_matching_qty_leaves_position_untouched(monkeypatch):
    """两边一致 → 不动，也不留噪音说明。"""
    p = _seed(qty=2)
    monkeypatch.setattr(m, "AlpacaTrading", lambda: _FakeEc({_CODE: 2}))
    m._watchdog_tick(_src())
    assert p.qty == 2
    assert p.sync_note is None
    REGISTRY._pos.clear()


def test_in_flight_close_qty_not_overridden(monkeypatch):
    """平仓挂单在途：部分成交期间两边本就不同，此处插手会与 settle 分支重复扣减。"""
    p = _seed(qty=3, status="closing", close_order_id="s1")
    monkeypatch.setattr(m, "AlpacaTrading", lambda: _FakeEc({_CODE: 2}))
    m._watchdog_tick(_src())
    assert p.qty == 3
    assert p.sync_note is None
    REGISTRY._pos.clear()


def test_pending_position_qty_not_overridden(monkeypatch):
    """pending 买单还没成交，券商没有这仓是正常的，不算不一致。"""
    p = _seed(qty=2, status="pending")
    monkeypatch.setattr(m, "AlpacaTrading", lambda: _FakeEc({}))
    m._watchdog_tick(_src())
    assert p.qty == 2
    assert p.sync_note is None
    REGISTRY._pos.clear()


def test_sync_note_cleared_once_back_in_sync(monkeypatch):
    """恢复一致后要清掉旧说明，不能一直挂着过期告警。"""
    p = _seed(qty=2)
    monkeypatch.setattr(m, "AlpacaTrading", lambda: _FakeEc({_CODE: 1}))
    m._watchdog_tick(_src())
    assert p.sync_note is not None
    m._watchdog_tick(_src())          # 本地已被改成 1，与券商一致
    assert p.sync_note is None
    REGISTRY._pos.clear()
