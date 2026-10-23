# -*- coding: utf-8 -*-
"""⑥ 挂单超时自动撤单（用户 2026-07-25 需求；审查 2.7）。

此前 watchdog 对在途单只问「成交了没」，**从不看挂了多久、也从不撤**：
  - pending 买单：只做 `code in broker` → 成交对账；没成交就一直挂着
  - resting 平仓卖单：只读券商订单状态；券商不动它，本地也不动它
而订单是 TimeInForce.DAY —— 一张挂在被动侧的单会安安静静排到收盘（2026-07-25 QQQ
减仓事件：卖一挂单在 Alpaca 侧挂了一整晚，用户以为已经卖掉了）。

超时阈值走模板参数 order_timeout_s（0 = 停用本机制）。
撤单后不自己臆断结果：买单读券商终态收尾，卖单交给既有的 resting 分支settle
（那条分支已经正确处理了「撤单终态前已部分成交」）。
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from types import SimpleNamespace

import app.watchdog as m
from app.order_state import JOURNAL, REGISTRY, ManagedPosition
from app.templates import get_store
from dataclasses import replace as dc_replace

_CODE = "QQQ260716C00500000"


def _set_timeout(seconds):
    """把当前生效模板的超时阈值改成 seconds（conftest 已隔离模板存储）。"""
    store = get_store()
    name = store.active_name()
    store._templates[name] = dc_replace(store.get(name), order_timeout_s=D(str(seconds)))


def _ago(seconds):
    return datetime.now(timezone.utc) - timedelta(seconds=seconds)


def _src():
    return SimpleNamespace(name="watcher",
                           alpaca=SimpleNamespace(option_quote=lambda code, fresh=False: None),
                           underlying_price=lambda t: None)


class _RecordingEc:
    def __init__(self, order=None, broker=None):
        self._order = order or {"status": "accepted", "filled_qty": "0", "filled_avg_price": None}
        self._broker = broker if broker is not None else set()
        self.cancelled = []

    def broker_position_qtys(self):
        return {s: 1 for s in self._broker}

    def get_order(self, oid):
        return self._order

    def cancel(self, oid):
        self.cancelled.append(oid)


# ── pending 买单 ──────────────────────────────────────────────────────────────
def _seed_pending(age_s):
    REGISTRY._pos.clear(); JOURNAL.clear()
    p = ManagedPosition(option_code=_CODE, ticker="QQQ", direction="long", qty=1,
                        entry_mid=D("1.00"), entry_time=_ago(age_s), order_id="e1",
                        status="pending")
    REGISTRY.record(p)
    JOURNAL.submitted("e1", "QQQ", _CODE, "buy", 1)
    return p


def test_pending_buy_cancelled_after_timeout(monkeypatch):
    """挂了超过阈值仍未成交的买单 → 向券商撤单，并记下已请求撤单（防重复发）。"""
    _set_timeout(30)
    p = _seed_pending(age_s=120)
    ec = _RecordingEc()
    monkeypatch.setattr(m, "AlpacaTrading", lambda: ec)
    m._watchdog_tick(_src())
    assert ec.cancelled == ["e1"]
    assert p.cancel_requested is True
    assert JOURNAL.get("e1").cancel_cause == "timeout"
    REGISTRY._pos.clear(); JOURNAL.clear()


def test_pending_buy_not_cancelled_before_timeout(monkeypatch):
    """还没到阈值 → 不动它，正常排队。"""
    _set_timeout(30)
    _seed_pending(age_s=5)
    ec = _RecordingEc()
    monkeypatch.setattr(m, "AlpacaTrading", lambda: ec)
    m._watchdog_tick(_src())
    assert ec.cancelled == []
    REGISTRY._pos.clear(); JOURNAL.clear()


def test_pending_buy_cancel_not_resent_while_awaiting_broker(monkeypatch):
    """已发过撤单、券商还没给终态（pending_cancel）→ 不重复发撤单请求。"""
    _set_timeout(30)
    p = _seed_pending(age_s=120)
    p.cancel_requested = True
    ec = _RecordingEc(order={"status": "pending_cancel", "filled_qty": "0"})
    monkeypatch.setattr(m, "AlpacaTrading", lambda: ec)
    m._watchdog_tick(_src())
    assert ec.cancelled == []
    assert p.status == "pending"          # 终态未到，不臆断
    REGISTRY._pos.clear(); JOURNAL.clear()


def test_pending_buy_settles_once_broker_confirms_cancelled(monkeypatch):
    """券商确认已撤 → 登记置 cancelled、订单流置已取消、落库。"""
    _set_timeout(30)
    p = _seed_pending(age_s=120)
    p.cancel_requested = True
    ec = _RecordingEc(order={"status": "canceled", "filled_qty": "0"})
    monkeypatch.setattr(m, "AlpacaTrading", lambda: ec)
    rec = {"called": False}
    monkeypatch.setattr(m.trade_recorder, "record_cancelled",
                        lambda oid: rec.update(called=True, oid=oid))
    m._watchdog_tick(_src())
    assert p.status == "cancelled"
    assert JOURNAL._events["e1"].status == "cancelled"
    assert rec == {"called": True, "oid": "e1"}
    REGISTRY._pos.clear(); JOURNAL.clear()


def test_pending_buy_that_filled_is_not_cancelled(monkeypatch):
    """超时前一刻成交且券商返回真实均价 → 走成交对账，绝不撤单。"""
    _set_timeout(30)
    p = _seed_pending(age_s=120)
    ec = _RecordingEc(
        order={"status": "filled", "filled_qty": "1", "filled_avg_price": "1.02"},
        broker={_CODE},
    )
    monkeypatch.setattr(m, "AlpacaTrading", lambda: ec)
    monkeypatch.setattr(m.trade_recorder, "record_filled", lambda *a, **k: None)
    m._watchdog_tick(_src())
    assert ec.cancelled == []
    assert p.status == "open"
    REGISTRY._pos.clear(); JOURNAL.clear()


# ── resting 平仓卖单 ──────────────────────────────────────────────────────────
def _seed_resting(age_s, qty=2):
    REGISTRY._pos.clear(); JOURNAL.clear()
    p = ManagedPosition(option_code=_CODE, ticker="QQQ", direction="long", qty=qty,
                        entry_mid=D("1.00"), entry_time=_ago(60), order_id="e1",
                        status="closing")
    p.close_order_id = "s1"
    p.close_qty = qty
    p.close_reason = "手动平仓"
    p.close_submitted_at = _ago(age_s)
    REGISTRY.record(p)
    JOURNAL.submitted("s1", "QQQ", _CODE, "sell", qty, reason="手动平仓")
    return p


def test_resting_close_cancelled_after_timeout(monkeypatch):
    """平仓挂单排队超时 → 撤单；本拍不改状态，交下一拍既有 resting 分支按券商终态settle。"""
    _set_timeout(30)
    p = _seed_resting(age_s=120)
    ec = _RecordingEc(order={"status": "accepted", "filled_qty": "0"}, broker={_CODE})
    monkeypatch.setattr(m, "AlpacaTrading", lambda: ec)
    m._watchdog_tick(_src())
    assert ec.cancelled == ["s1"]
    assert p.status == "closing"          # 终态由券商说了算，不自己改
    assert JOURNAL.get("s1").cancel_cause == "timeout"
    REGISTRY._pos.clear(); JOURNAL.clear()


def test_manual_resting_close_cancel_confirmation_is_not_an_error(monkeypatch):
    """手动撤单稍后才获券商终态时，只正常回退 open，不写“平仓未成交”错误。"""
    _set_timeout(30)
    p = _seed_resting(age_s=5)
    p.cancel_requested = True
    JOURNAL.mark_cancel_requested(
        "s1",
        cancel_origin="user",
        cancel_cause="manual",
        reason="用户手动撤单",
    )
    ec = _RecordingEc(
        order={"status": "canceled", "filled_qty": "0"},
        broker={_CODE},
    )
    monkeypatch.setattr(m, "AlpacaTrading", lambda: ec)

    m._watchdog_tick(_src())

    assert p.status == "open"
    assert p.close_order_id is None
    assert p.close_error is None
    assert JOURNAL.get("s1").status == "cancelled"
    assert JOURNAL.get("s1").cancel_cause == "manual"
    REGISTRY._pos.clear(); JOURNAL.clear()


def test_unexpected_broker_close_cancel_still_surfaces_retry_error(monkeypatch):
    """非用户发起的券商取消仍须提示，以免异常卖单终止被静默吞掉。"""
    _set_timeout(30)
    p = _seed_resting(age_s=5)
    ec = _RecordingEc(
        order={"status": "canceled", "filled_qty": "0"},
        broker={_CODE},
    )
    monkeypatch.setattr(m, "AlpacaTrading", lambda: ec)

    m._watchdog_tick(_src())

    assert p.status == "open"
    assert "平仓单券商侧未成交" in p.close_error
    REGISTRY._pos.clear(); JOURNAL.clear()


def test_resting_close_not_cancelled_before_timeout(monkeypatch):
    _set_timeout(30)
    _seed_resting(age_s=5)
    ec = _RecordingEc(order={"status": "accepted", "filled_qty": "0"}, broker={_CODE})
    monkeypatch.setattr(m, "AlpacaTrading", lambda: ec)
    m._watchdog_tick(_src())
    assert ec.cancelled == []
    REGISTRY._pos.clear(); JOURNAL.clear()


def test_persistent_target_close_ignores_short_timeout(monkeypatch):
    """手动目标限价单长期挂单，不受 quick 卖单的短超时限制。"""
    _set_timeout(5)
    p = _seed_resting(age_s=120)
    p.close_timeout_policy = "persistent"
    ec = _RecordingEc(order={"status": "accepted", "filled_qty": "0"}, broker={_CODE})
    monkeypatch.setattr(m, "AlpacaTrading", lambda: ec)
    m._watchdog_tick(_src())
    assert ec.cancelled == []
    assert p.status == "closing" and p.close_order_id == "s1"
    REGISTRY._pos.clear(); JOURNAL.clear()


# ── 停用开关 ──────────────────────────────────────────────────────────────────
def test_timeout_zero_disables_auto_cancel(monkeypatch):
    """order_timeout_s=0 → 完全停用自动撤单（买单/卖单都不撤）。"""
    _set_timeout(0)
    _seed_pending(age_s=99999)
    ec = _RecordingEc()
    monkeypatch.setattr(m, "AlpacaTrading", lambda: ec)
    m._watchdog_tick(_src())
    assert ec.cancelled == []

    _seed_resting(age_s=99999)
    ec2 = _RecordingEc(order={"status": "accepted", "filled_qty": "0"}, broker={_CODE})
    monkeypatch.setattr(m, "AlpacaTrading", lambda: ec2)
    m._watchdog_tick(_src())
    assert ec2.cancelled == []
    REGISTRY._pos.clear(); JOURNAL.clear()
