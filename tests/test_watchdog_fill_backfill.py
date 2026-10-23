# -*- coding: utf-8 -*-
"""成交时回填**券商真实成交价/成交量**（审查 3.2）。

此前 `_watchdog_tick` 在 pending→open 时只写了 `pos.status = "open"`，
`entry_mid` 一直是 ③选约时的 **mid**，而实际是以 ask（或 ask+1档）成交的。
于是 `entry_val = entry_mid×100×qty` 系统性偏低 → `$止损`/`%止损` 两条线
都算在一个虚的成本上，止损偏松。这条和 3.1（用 mid 计价）叠加，误差是双份的。

成交价只有券商知道，必须问它，不能自己推。
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


class _FakeEc:
    def __init__(self, broker_qty, order=None, raise_on_get=False):
        self._broker = {_CODE: broker_qty}
        self._order = order
        self._raise = raise_on_get

    def broker_position_qtys(self):
        return dict(self._broker)

    def get_order(self, oid):
        if self._raise:
            raise RuntimeError("券商查单超时")
        return self._order or {}

    def cancel(self, oid):
        pass


def _seed(qty=2, entry_mid="1.00"):
    REGISTRY._pos.clear(); JOURNAL.clear()
    p = ManagedPosition(option_code=_CODE, ticker="QQQ", direction="long", qty=qty,
                        entry_mid=D(entry_mid), entry_time=datetime.now(timezone.utc),
                        order_id="e1", status="pending")
    REGISTRY.record(p)
    JOURNAL.submitted("e1", "QQQ", _CODE, "buy", qty, price=D(entry_mid))
    return p


def test_entry_mid_backfilled_from_broker_fill_price(monkeypatch):
    """选约 mid 1.00、实际成交 1.15 → entry_mid 必须变成 1.15（止损线的分母）。"""
    p = _seed(qty=2, entry_mid="1.00")
    monkeypatch.setattr(m, "AlpacaTrading",
                        lambda: _FakeEc(2, {"status": "filled", "filled_qty": "2",
                                            "filled_avg_price": "1.15"}))
    monkeypatch.setattr(m.trade_recorder, "record_filled", lambda *a, **k: None)
    m._watchdog_tick(_src())
    assert p.status == "open"
    assert p.entry_mid == D("1.15")
    assert p.entry_val == D("230")          # 1.15 × 100 × 2
    REGISTRY._pos.clear()


def test_qty_backfilled_from_broker_on_partial_fill(monkeypatch):
    """下 3 张只成交 2 张 → 登记张数按券商为准，否则后续全平会超卖。"""
    p = _seed(qty=3, entry_mid="1.00")
    monkeypatch.setattr(m, "AlpacaTrading",
                        lambda: _FakeEc(2, {"status": "partially_filled", "filled_qty": "2",
                                            "filled_avg_price": "1.10"}))
    monkeypatch.setattr(m.trade_recorder, "record_filled", lambda *a, **k: None)
    m._watchdog_tick(_src())
    assert p.qty == 2
    REGISTRY._pos.clear()


def test_journal_shows_real_fill_price(monkeypatch):
    """订单流里那条买单的价格也要换成真实成交价，否则面板显示的是委托时的估价。"""
    _seed(qty=2, entry_mid="1.00")
    monkeypatch.setattr(m, "AlpacaTrading",
                        lambda: _FakeEc(2, {"status": "filled", "filled_qty": "2",
                                            "filled_avg_price": "1.15"}))
    monkeypatch.setattr(m.trade_recorder, "record_filled", lambda *a, **k: None)
    m._watchdog_tick(_src())
    assert JOURNAL._events["e1"].price == D("1.15")
    REGISTRY._pos.clear(); JOURNAL.clear()


def test_fill_price_persisted_to_db(monkeypatch):
    """落库同样要记真实成交价与成交量（⑧ ttt_trades 是事后复盘的依据）。"""
    _seed(qty=3, entry_mid="1.00")
    monkeypatch.setattr(m, "AlpacaTrading",
                        lambda: _FakeEc(2, {"status": "partially_filled", "filled_qty": "2",
                                            "filled_avg_price": "1.10"}))
    rec = {}
    monkeypatch.setattr(m.trade_recorder, "record_filled",
                        lambda oid, entry_mid=None, qty=None: rec.update(
                            oid=oid, entry_mid=entry_mid, qty=qty))
    m._watchdog_tick(_src())
    assert rec == {"oid": "e1", "entry_mid": D("1.10"), "qty": 2}
    REGISTRY._pos.clear()


def test_missing_fill_price_keeps_order_pending(monkeypatch):
    """券商没给成交价时不启用止损，避免把选约 mid 当真实成本。"""
    p = _seed(qty=2, entry_mid="1.00")
    monkeypatch.setattr(m, "AlpacaTrading",
                        lambda: _FakeEc(2, {"status": "filled", "filled_qty": "2",
                                            "filled_avg_price": None}))
    monkeypatch.setattr(m.trade_recorder, "record_filled", lambda *a, **k: None)
    m._watchdog_tick(_src())
    assert p.status == "pending"
    assert p.entry_mid == D("1.00")
    REGISTRY._pos.clear()


def test_later_rest_price_backfills_same_filled_qty(monkeypatch):
    """WS 已报 ×2 但无价格，REST 后报同样 ×2 + 真价时必须补价，不能因数量没变跳过。"""
    p = _seed(qty=2, entry_mid="1.00")
    p.status = "open"
    p.entry_filled_qty = 2
    p.entry_fill_mid = D("1.00")
    JOURNAL.mark_filled("e1")
    recorded = {}
    monkeypatch.setattr(
        m.trade_recorder,
        "record_position_sync",
        lambda oid, qty, entry_mid=None: recorded.update(
            oid=oid, qty=qty, entry_mid=entry_mid),
    )

    assert m._apply_entry_fill(p, {
        "status": "filled",
        "filled_qty": "2",
        "filled_avg_price": "1.15",
    })

    assert p.qty == 2
    assert p.entry_mid == D("1.15")
    assert p.entry_fill_mid == D("1.15")
    assert JOURNAL.get("e1").price == D("1.15")
    assert recorded == {"oid": "e1", "qty": 2, "entry_mid": D("1.15")}


def test_later_entry_price_correction_preserves_added_qty(monkeypatch):
    """首单 ×2 后已加仓 ×1；补首单真价只修正首单成本组件，不改变总张数。"""
    p = _seed(qty=3, entry_mid="1.333333333333333333333333333")
    p.status = "open"
    p.entry_filled_qty = 2
    p.entry_fill_mid = D("1.00")
    JOURNAL.mark_filled("e1")
    monkeypatch.setattr(m.trade_recorder, "record_position_sync", lambda *a, **k: None)

    m._apply_entry_fill(p, {
        "status": "filled",
        "filled_qty": "2",
        "filled_avg_price": "1.10",
    })

    assert p.qty == 3
    assert p.entry_mid.quantize(D("0.0001")) == D("1.4000")


def test_query_failure_keeps_pending_until_true_cost_arrives(monkeypatch):
    """查单失败时保留 pending；不能用 provisional mid 启动错误的美元止损。"""
    p = _seed(qty=2, entry_mid="1.00")
    monkeypatch.setattr(m, "AlpacaTrading", lambda: _FakeEc(2, raise_on_get=True))
    monkeypatch.setattr(m.trade_recorder, "record_filled", lambda *a, **k: None)
    m._watchdog_tick(_src())
    assert p.status == "pending"
    assert p.entry_mid == D("1.00")
    REGISTRY._pos.clear()


def test_journal_only_sell_missing_after_mock_restart_is_cancelled(monkeypatch):
    """MOCK 重启丢单后，明确 404 的本地幽灵卖单应由巡检自动了结。"""
    REGISTRY._pos.clear()
    JOURNAL.clear()
    JOURNAL.submitted(
        "missing-sell", "SOFI", "SOFI260731C00018500", "sell", 1,
        price=D("0.23"), reason="目标限价 +20%", action="reduce",
        origin="user", parent_order_id="entry-1")

    class _RestartedMock:
        def get_order(self, order_id):
            raise RuntimeError(f"404 Order {order_id} not found")

    recorded = {}
    monkeypatch.setattr(
        m.trade_recorder,
        "record_cancelled",
        lambda order_id, **kwargs: recorded.update(order_id=order_id, **kwargs),
    )

    m._sync_journal_only_orders(
        _RestartedMock(), {}, reconcile_rest=True)

    event = JOURNAL.get("missing-sell")
    assert event.status == "cancelled"
    assert event.fail_reason == "券商查无此订单，本地历史在途记录已清理"
    assert recorded == {"order_id": "missing-sell", "side": "sell"}
    JOURNAL.clear()


def test_journal_only_order_transient_query_error_stays_pending(monkeypatch):
    """只有明确 404 才能清理；普通网络错误必须继续等待。"""
    REGISTRY._pos.clear()
    JOURNAL.clear()
    JOURNAL.submitted(
        "query-timeout", "SOFI", "SOFI260731C00018500", "sell", 1,
        action="reduce", origin="user", parent_order_id="entry-1")

    class _UnavailableBroker:
        def get_order(self, order_id):
            raise RuntimeError("broker query timeout")

    monkeypatch.setattr(
        m.trade_recorder,
        "record_cancelled",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("transient error must not settle the order")),
    )

    m._sync_journal_only_orders(
        _UnavailableBroker(), {}, reconcile_rest=True)

    assert JOURNAL.get("query-timeout").status == "pending"
    JOURNAL.clear()


def test_add_fill_is_applied_once_from_cumulative_broker_update(monkeypatch):
    """加仓 submit 不改仓；WS/REST 的累计成交只把新增部分应用一次。"""
    p = _seed(qty=1, entry_mid="1.00")
    p.status = "open"
    JOURNAL.mark_filled("e1", price=D("1.00"), qty=1)
    JOURNAL.submitted(
        "add-1", "QQQ", _CODE, "buy", 2, price=D("1.10"),
        action="add", origin="user", parent_order_id="e1")
    monkeypatch.setattr(m.trade_recorder, "record_add", lambda *a, **k: None)

    update = {
        "status": "filled",
        "filled_qty": "2",
        "filled_avg_price": "1.20",
    }
    m._sync_journal_only_orders(
        _FakeEc(3), {"add-1": update}, reconcile_rest=False)
    m._sync_journal_only_orders(
        _FakeEc(3), {"add-1": update}, reconcile_rest=False)

    assert p.qty == 3
    assert p.entry_mid == D("1.133333333333333333333333333")
    assert JOURNAL.get("add-1").applied_qty == 2


def test_add_fill_delta_can_only_be_claimed_once_concurrently():
    """同步 submit 与 TradingStream 同时看见 filled，也只能有一方认领成交增量。"""
    from concurrent.futures import ThreadPoolExecutor

    JOURNAL.clear()
    JOURNAL.submitted(
        "add-race", "QQQ", _CODE, "buy", 3, price=D("0.93"),
        action="add", origin="user", parent_order_id="e1")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(
            lambda _n: JOURNAL.claim_fill_delta(
                "add-race", 3, D("0.93")),
            range(2),
        ))

    assert sorted(qty for qty, _price in results) == [0, 3]
    assert JOURNAL.get("add-race").applied_qty == 3
    JOURNAL.clear()


def test_partial_add_uses_incremental_not_cumulative_average():
    """累计均价变化时，第二段成本必须用名义额差分，不能重复套最新累计均价。"""
    JOURNAL.clear()
    JOURNAL.submitted(
        "add-partial", "QQQ", _CODE, "buy", 2, price=D("1.00"),
        action="add", origin="user", parent_order_id="e1")

    first_qty, first_px = JOURNAL.claim_fill_delta(
        "add-partial", 1, D("1.00"))
    second_qty, second_px = JOURNAL.claim_fill_delta(
        "add-partial", 2, D("1.10"))

    assert (first_qty, first_px) == (1, D("1.00"))
    assert (second_qty, second_px) == (1, D("1.20"))
    JOURNAL.clear()


def test_terminal_close_order_always_clears_tracking_even_if_settled_elsewhere(monkeypatch):
    """撤单终态无论由谁记账，本地平仓跟踪都必须清空。

    2026-08-01 MOCK 实测：结算令牌落败时直接 continue，跳过了
    `_clear_resting_close_tracking()`，`pos.close_order_id` 永远留着，watchdog 每拍
    都重新看到同一个终态订单 → 刷屏 `cancel_settle_skipped`，前端永久卡在「撤单中」。
    清理本地跟踪是幂等的，绝不能和「谁负责记账」共用一个闸门。
    """
    p = _seed(qty=1, entry_mid="1.00")
    p.status = "open"
    p.close_order_id = "sell-stuck"
    p.close_qty = 1
    p.close_reason = "目标限价 +70%"
    JOURNAL.submitted("sell-stuck", "QQQ", _CODE, "sell", 1, reason="目标限价 +70%")
    # 第三方（非 exec_close / 非 watchdog 主循环）已把该单推进终态：
    # watchdog 本拍必然拿不到结算令牌。
    JOURNAL.mark_cancelled("sell-stuck", broker_status="canceled")

    monkeypatch.setattr(m, "AlpacaTrading",
                        lambda: _FakeEc(1, {"id": "sell-stuck", "status": "canceled",
                                            "filled_qty": "0",
                                            "filled_avg_price": None}))
    monkeypatch.setattr(m.trade_recorder, "record_filled", lambda *a, **k: None)
    monkeypatch.setattr(m.trade_recorder, "record_closed", lambda *a, **k: None)

    m._watchdog_tick(_src())

    assert p.close_order_id is None, "终态平仓单必须清跟踪，否则前端永久卡在撤单中"
    assert p.close_qty == 0
    assert p.status == "open", "未成交的撤单不该改变持仓数量"
    assert p.qty == 1
    REGISTRY._pos.clear(); JOURNAL.clear()
