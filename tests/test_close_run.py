"""close_run：成交→closed/减仓+落库；失败/异常→回退 open + close_error。"""
from datetime import datetime, timezone
from decimal import Decimal as D
from types import SimpleNamespace

import app.exec_close as order_exec
from app.order_state import JOURNAL, REGISTRY, ManagedPosition


def _req(code="QQQ260716C00500000", qty=None, timeout_policy="quick"):
    return SimpleNamespace(option_code=code, reason="手动平仓", qty=qty,
                           price_type=None, limit_price=None,
                           timeout_policy=timeout_policy)


def _src():
    return SimpleNamespace(name="watcher", alpaca=SimpleNamespace())


def _seed(qty=2, status="closing"):
    REGISTRY._pos.clear()
    p = ManagedPosition(option_code="QQQ260716C00500000", ticker="QQQ", direction="long",
                        qty=qty, entry_mid=D("1.00"), entry_time=datetime.now(timezone.utc),
                        order_id="e1", status=status)
    REGISTRY.record(p)
    return p


def test_run_full_close_success_sets_closed(monkeypatch):
    p = _seed(qty=2)
    monkeypatch.setattr(order_exec, "atomic_close",
                        lambda *a, **k: {"closed": True, "filled_qty": 2,
                                         "fill": "1.20", "order": {"id": "s1"}})
    recorded = {}
    monkeypatch.setattr(order_exec.trade_recorder, "record_closed",
                        lambda oid, mid, q, r: recorded.update(oid=oid, mid=mid, q=q))
    order_exec.close_run(_src(), _req())
    assert p.status == "closed"
    assert recorded["oid"] == "e1" and recorded["q"] == 2
    REGISTRY._pos.clear()


def test_run_partial_close_reduces_and_reopens(monkeypatch):
    p = _seed(qty=3)
    monkeypatch.setattr(order_exec, "atomic_close",
                        lambda *a, **k: {"closed": True, "filled_qty": 1,
                                         "fill": "1.10", "order": {"id": "s1"}})
    monkeypatch.setattr(order_exec.trade_recorder, "record_closed", lambda *a, **k: None)
    order_exec.close_run(_src(), _req(qty=1))
    assert p.status == "open" and p.qty == 2
    assert p.realized_pnl == D("10.00")
    REGISTRY._pos.clear()


def test_run_multiple_partial_closes_accumulates_realized_pnl(monkeypatch):
    """TSLA P310 场景：3 张分两次各减 1 张，持仓卡累计两次已实现盈利。"""
    p = _seed(qty=3)
    p.entry_mid = D("0.96")
    fills = iter(("2.69", "3.21"))
    monkeypatch.setattr(
        order_exec,
        "atomic_close",
        lambda *a, **k: {
            "closed": True,
            "filled_qty": 1,
            "fill": next(fills),
            "order": {"id": "sell-partial"},
        },
    )
    monkeypatch.setattr(order_exec.trade_recorder, "record_closed", lambda *a, **k: None)

    order_exec.close_run(_src(), _req(qty=1))
    assert p.status == "open" and p.qty == 2
    assert p.realized_pnl == D("173.00")

    p.status = "closing"
    order_exec.close_run(_src(), _req(qty=1))
    assert p.status == "open" and p.qty == 1
    assert p.realized_pnl == D("398.00")
    REGISTRY._pos.clear()


def test_run_not_filled_reverts_open_with_error(monkeypatch):
    p = _seed(qty=2)
    monkeypatch.setattr(order_exec, "atomic_close",
                        lambda *a, **k: {"closed": False, "error": "穿价追单 3 手未成交"})
    order_exec.close_run(_src(), _req())
    assert p.status == "open"
    assert p.close_error == "穿价追单 3 手未成交"
    REGISTRY._pos.clear()


def test_run_does_not_guess_qty_from_closed_flag(monkeypatch):
    p = _seed(qty=2)
    monkeypatch.setattr(
        order_exec,
        "atomic_close",
        lambda *a, **k: {"closed": True, "fill": "1.20", "order": {"id": "s1"}},
    )

    result = order_exec.close_run(_src(), _req())

    assert result["closed"] is False
    assert result["reconcile_required"] is True
    assert p.qty == 2
    assert p.status == "open"


def test_run_does_not_use_quote_mid_as_unknown_fill_price(monkeypatch):
    p = _seed(qty=2)
    monkeypatch.setattr(
        order_exec,
        "atomic_close",
        lambda *a, **k: {
            "closed": True,
            "filled_qty": 2,
            "fill": None,
            "mid": "1.50",
            "order": {"id": "s1"},
        },
    )
    recorded = []
    monkeypatch.setattr(
        order_exec.trade_recorder,
        "record_closed",
        lambda oid, mid, qty, reason: recorded.append(mid),
    )

    order_exec.close_run(_src(), _req())

    assert recorded == [None]
    assert p.realized_pnl == D("0")


def test_run_exception_reverts_open_with_error(monkeypatch):
    p = _seed(qty=2)
    def boom(*a, **k):
        raise RuntimeError("券商挂了")
    monkeypatch.setattr(order_exec, "atomic_close", boom)
    order_exec.close_run(_src(), _req())
    assert p.status == "open"
    assert "券商挂了" in p.close_error
    REGISTRY._pos.clear()


def test_run_resting_keeps_closing_no_false_fill(monkeypatch):
    """atomic_close 回报 resting（挂单在券商排队未成交）→ 保持 closing（平仓挂单中），
    绝不谎报成交：不落库成交、订单流卖单记 pending（非 filled），记住 close_order_id 供撤单/对账。"""
    JOURNAL.clear()
    p = _seed(qty=2)
    monkeypatch.setattr(order_exec, "atomic_close",
                        lambda *a, **k: {"closed": False, "resting": True,
                                         "order": {"id": "s1"}, "qty": 2})
    called = {"closed": False}
    monkeypatch.setattr(order_exec.trade_recorder, "record_closed",
                        lambda *a, **k: called.update(closed=True))
    order_exec.close_run(_src(), _req())
    assert p.status == "closing"                 # 仍挂单中，不掉出持仓、不谎报成交
    assert p.close_order_id == "s1" and p.close_qty == 2
    assert p.close_reason == "手动平仓"
    assert called["closed"] is False             # 未成交不落库
    ev = JOURNAL._events.get("s1")
    assert ev is not None and ev.side == "sell" and ev.status == "pending"  # 订单流：平仓挂单中
    assert ev.reason == "手动平仓"
    REGISTRY._pos.clear()
    JOURNAL.clear()


def test_run_resting_applies_confirmed_partial_fill_once(monkeypatch):
    """执行器返回挂单中时，已确认成交部分先减仓，当前单累计量交给 watchdog 幂等续接。"""
    p = _seed(qty=5)
    monkeypatch.setattr(
        order_exec,
        "atomic_close",
        lambda *a, **k: {
            "closed": False,
            "resting": True,
            "order": {"id": "s1"},
            "filled_qty": 2,
            "fill": "1.20",
            "current_order_qty": 3,
            "current_order_filled_qty": 1,
        },
    )
    recorded = []
    monkeypatch.setattr(
        order_exec.trade_recorder,
        "record_closed",
        lambda oid, mid, qty, reason: recorded.append((oid, mid, qty, reason)),
    )

    order_exec.close_run(_src(), _req())

    assert p.status == "closing"
    assert p.qty == 3
    assert p.close_order_id == "s1"
    assert p.close_qty == 3
    assert p.close_applied_qty == 1
    assert recorded == [("e1", D("1.20"), 2, "手动平仓")]


def test_run_resting_remembers_persistent_target_policy(monkeypatch):
    JOURNAL.clear()
    p = _seed(qty=2)
    monkeypatch.setattr(order_exec, "atomic_close",
                        lambda *a, **k: {"closed": False, "resting": True,
                                         "order": {"id": "s1"}, "qty": 2})
    order_exec.close_run(_src(), _req(timeout_policy="persistent"))
    assert p.close_timeout_policy == "persistent"
    assert p.close_order_id == "s1"
    REGISTRY._pos.clear()
    JOURNAL.clear()


def test_run_resting_passive_warns_user(monkeypatch):
    """被动挂单（如平仓挂卖一）排队中 → 写 close_error 明确告知"被动排队、可能不成交"。

    2026-07-25 QQQ 事件：这单在券商侧永远排着，用户界面上只看到"平仓中…"，
    以为在追单、其实根本吃不到单。必须说清楚，并提示可撤单改用买一。
    """
    p = _seed(qty=2)
    monkeypatch.setattr(order_exec, "atomic_close",
                        lambda *a, **k: {"closed": False, "resting": True, "passive": True,
                                         "order": {"id": "s1"}, "qty": 2})
    order_exec.close_run(_src(), _req())
    assert p.status == "closing"          # 仍是挂单中，不改既有状态语义
    assert p.close_order_id == "s1"
    assert p.close_error is not None and "被动" in p.close_error
    REGISTRY._pos.clear()
    JOURNAL.clear()


def test_run_resting_marketable_sets_no_warning(monkeypatch):
    """可成交价格类型只是暂时没成交（正常排队）→ 不写 close_error，不打扰用户。"""
    p = _seed(qty=2)
    monkeypatch.setattr(order_exec, "atomic_close",
                        lambda *a, **k: {"closed": False, "resting": True, "passive": False,
                                         "order": {"id": "s1"}, "qty": 2})
    order_exec.close_run(_src(), _req())
    assert p.close_error is None
    REGISTRY._pos.clear()
    JOURNAL.clear()


def test_run_success_records_journal_reason(monkeypatch):
    """#9：手动平仓的卖单要在订单流留下 reason（谁平的+为什么），供前端标签展示。"""
    JOURNAL.clear()
    p = _seed(qty=2)
    monkeypatch.setattr(order_exec, "atomic_close",
                        lambda *a, **k: {"closed": True, "filled_qty": 2,
                                         "fill": "1.20", "order": {"id": "s1"}})
    monkeypatch.setattr(order_exec.trade_recorder, "record_closed", lambda *a, **k: None)
    order_exec.close_run(_src(), _req())
    ev = JOURNAL._events.get("s1")
    assert ev is not None and ev.reason == "手动平仓"
    REGISTRY._pos.clear()
    JOURNAL.clear()
