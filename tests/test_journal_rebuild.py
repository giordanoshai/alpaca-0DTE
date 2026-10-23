# -*- coding: utf-8 -*-
"""OrderJournal.rebuild_from_trades：进程重启后从 ttt_trades 行重建当日订单流，
修「重启后订单模块不显示今天交易记录」。ttt_trades 是持仓生命周期表（已平仓行 qty 归零、
无独立卖单行），故已平仓张数从 total_pnl 反推，external_closed（无成交价）无法恢复记 0。
"""
from datetime import datetime, timezone
from decimal import Decimal as D

from app.order_state import OrderJournal


def _row(**kw):
    base = dict(order_id="e1", ticker="qqq", option_code="QQQ260716C00500000",
                qty=2, entry_mid=D("1.00"), entry_time=datetime(2026, 7, 23, 14, 0, tzinfo=timezone.utc),
                status="open", realized_pnl=D("0"), exit_mid=None, exit_time=None,
                total_pnl=D("0"), close_reason=None, updated_at=None)
    base.update(kw)
    return base


def test_rebuild_open_row_makes_filled_buy():
    j = OrderJournal()
    j.rebuild_from_trades([_row(status="open", qty=2)])
    ev = j._events["e1"]
    assert ev.side == "buy" and ev.status == "filled" and ev.qty == 2
    assert ev.ticker == "QQQ"


def test_rebuild_pending_row_makes_pending_buy():
    j = OrderJournal()
    j.rebuild_from_trades([_row(status="pending", qty=3)])
    assert j._events["e1"].status == "pending" and j._events["e1"].qty == 3


def test_rebuild_cancelled_row_makes_cancelled_buy():
    j = OrderJournal()
    j.rebuild_from_trades([_row(status="cancelled", qty=1)])
    assert j._events["e1"].status == "cancelled"


def test_rebuild_closed_row_makes_filled_buy_and_sell():
    j = OrderJournal()
    # 全平：qty 归零，total_pnl=(exit-entry)*100*qty → 2 张 = (1.20-1.00)*100*2 = 40
    j.rebuild_from_trades([_row(status="closed", qty=0, entry_mid=D("1.00"),
                                exit_mid=D("1.20"), total_pnl=D("40"),
                                exit_time=datetime(2026, 7, 23, 15, 0, tzinfo=timezone.utc),
                                close_reason="手动平仓")])
    buy = j._events["e1"]
    sell = j._events["e1:sell"]
    assert buy.side == "buy" and buy.status == "filled" and buy.qty == 2
    assert sell.side == "sell" and sell.status == "filled" and sell.qty == 2
    assert sell.reason == "手动平仓"


def test_rebuild_external_closed_no_price_qty_zero():
    """external_closed 无成交价 → 张数无法反推，记 0（如实，不臆造）。"""
    j = OrderJournal()
    j.rebuild_from_trades([_row(status="external_closed", qty=0, exit_mid=None,
                                total_pnl=D("0"), close_reason="external_closed")])
    assert j._events["e1"].status == "filled"
    assert j._events["e1:sell"].qty == 0


def test_rebuilt_rows_visible_in_today():
    """重建的当日事件应在 today() 里可见（修重启后空白）。"""
    j = OrderJournal()
    now = datetime(2026, 7, 23, 16, 0, tzinfo=timezone.utc)
    j.rebuild_from_trades([_row(status="open", qty=2, entry_time=now)])
    groups = j.today(now=now)
    assert any(e["order_id"] == "e1" for e in groups["filled"])
