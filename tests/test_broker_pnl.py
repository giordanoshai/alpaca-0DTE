"""券商成交归属与盈亏拆分。"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

from app.broker_pnl import PROGRAM_CLIENT_ORDER_PREFIX, compute_program_pnl
from app.order_pricing import build_limit_order


NOW = datetime(2026, 7, 27, 15, 0, tzinfo=timezone.utc)


def _order(order_id, symbol, side, qty, price, minute, client_order_id="legacy"):
    return SimpleNamespace(
        id=order_id,
        client_order_id=client_order_id,
        symbol=symbol,
        side=side,
        filled_qty=str(qty),
        filled_avg_price=str(price),
        filled_at=NOW - timedelta(minutes=minute),
    )


def test_new_orders_have_program_client_order_prefix():
    request = build_limit_order(
        "QQQ260727C00685000",
        "buy",
        1,
        Decimal("1.25"),
        extended_hours=False,
    )

    assert request.client_order_id.startswith(PROGRAM_CLIENT_ORDER_PREFIX)
    assert "open" not in request.client_order_id
    assert "close" not in request.client_order_id
    assert "reduce" not in request.client_order_id


def test_unprefixed_orders_are_always_external_even_if_ids_look_local():
    symbol = "GOOGL260727C00330000"
    orders = [
        _order("system-entry", symbol, "buy", 1, "1.12", 50),
        _order("system-add", symbol, "buy", 1, "0.80", 49),
        _order("system-reduce", symbol, "sell", 1, "1.20", 48),
        _order("system-close", symbol, "sell", 1, "0.95", 47),
        _order("external-entry", symbol, "buy", 8, "1.30", 20),
        _order("external-close", symbol, "sell", 8, "1.07", 19),
    ]

    result = compute_program_pnl(
        orders=orders,
        current_positions=[],
        daily_start=NOW - timedelta(hours=8),
        weekly_start=NOW - timedelta(days=1),
        account_daily_pnl=Decimal("-161"),
        account_weekly_pnl=Decimal("-161"),
        as_of=NOW,
    )

    assert result.daily.program_gross_pnl == Decimal("0")
    assert result.daily.external_gross_pnl == Decimal("-161")
    assert result.daily.account_adjustment == Decimal("0")
    assert result.daily.program_sell_contracts == Decimal("0")
    assert result.daily.external_sell_contracts == Decimal("10")
    assert result.program_open_qty == {}


def test_tagged_program_orders_are_not_closed_by_unprefixed_external_sell():
    symbol = "QQQ260727C00685000"
    orders = [
        _order("program-entry", symbol, "buy", 2, "1.00", 5, "ttt-entry"),
        _order("external-entry", symbol, "buy", 1, "1.00", 4, "external"),
        _order("external-sell", symbol, "sell", 1, "1.10", 3, "external"),
        _order("program-close", symbol, "sell", 2, "1.20", 2, "ttt-close"),
    ]

    result = compute_program_pnl(
        orders=orders,
        current_positions=[],
        daily_start=NOW - timedelta(hours=8),
        weekly_start=NOW - timedelta(days=1),
        account_daily_pnl=Decimal("50"),
        account_weekly_pnl=Decimal("50"),
        as_of=NOW,
    )

    assert result.daily.program_gross_pnl == Decimal("40")
    assert result.daily.external_gross_pnl == Decimal("10")
    assert result.daily.account_adjustment == Decimal("0")
    assert result.program_open_qty == {}


def test_open_program_position_is_marked_with_alpaca_current_price():
    symbol = "IWM260727P00294000"
    orders = [_order("entry", symbol, "buy", 2, "0.80", 5, "ttt-entry")]
    position = SimpleNamespace(symbol=symbol, qty="2", current_price="1.05")

    result = compute_program_pnl(
        orders=orders,
        current_positions=[position],
        daily_start=NOW - timedelta(hours=8),
        weekly_start=NOW - timedelta(days=1),
        account_daily_pnl=Decimal("50"),
        account_weekly_pnl=Decimal("50"),
        as_of=NOW,
    )

    assert result.daily.program_gross_pnl == Decimal("50")
    assert result.daily.external_gross_pnl == Decimal("0")
    assert result.daily.account_adjustment == Decimal("0")
    assert result.program_open_qty == {symbol: Decimal("2")}


def test_position_missing_from_snapshot_does_not_break_whole_pnl():
    """orders 与 positions 是两次 REST 调用，平仓成交瞬间必然短暂不一致。

    2026-07-31 实盘：IWM260731P00290000 最后一批平仓卖单成交的那一拍，
    订单流还认为有未平仓、持仓快照里已经没有该合约，旧实现直接 RuntimeError，
    使整份运行时状态刷新失败（trading_state_refresh_error），前端 PnL 卡住一拍。
    该合约按已平（mark=0）计入即可，不该让整份快照失败。
    """
    symbol = "IWM260731P00290000"
    other = "SPY260731C00741000"
    orders = [
        _order("entry-gone", symbol, "buy", 2, "1.00", 5, "ttt-entry"),
        _order("entry-live", other, "buy", 1, "1.60", 5, "ttt-entry"),
    ]
    position = SimpleNamespace(symbol=other, qty="1", current_price="1.80")

    result = compute_program_pnl(
        orders=orders,
        current_positions=[position],
        daily_start=NOW - timedelta(hours=8),
        weekly_start=NOW - timedelta(days=1),
        account_daily_pnl=Decimal("-180"),
        account_weekly_pnl=Decimal("-180"),
        as_of=NOW,
    )

    # 仍在券商持仓里的那张照常按现价 mark；消失的那张按 0 计，不抛异常。
    assert result.daily.program_gross_pnl == Decimal("-180")
    assert result.program_open_qty == {symbol: Decimal("2"), other: Decimal("1")}


def test_unprefixed_sell_closing_program_position_is_credited_to_program():
    """本程序开的仓被人在券商端手动平掉，收益必须记回本程序，不能单腿悬空。

    2026-09-03 实盘现场（美东 09:49-09:50）：程序买入 NVDA C227.5 两张
    @2.79，挂出的平仓限价单被撤后，持仓卡在 status='closing' 的死态（另修于
    b99ad43），前端卖出按钮全灰，用户只能去 Alpaca App 市价清仓 @3.05。
    这笔手动平仓单没有 ttt- 前缀，旧实现照外部单处理 —— 买入的 558 记在本程序
    账上、收回的 610 不认，当日「本程序盈亏」显示 -565，而账户实际 +44。

    券商持仓按合约合并、没有编号可分，所以按数量归属：外部卖单先冲抵外部自己
    买进的量，冲抵不掉的部分就是在平本程序的仓。
    """
    symbol = "NVDA260904C00227500"
    orders = [
        _order("program-entry", symbol, "buy", 2, "2.79", 5, "ttt-entry"),
        _order("manual-close", symbol, "sell", 2, "3.05", 3, "918d1423-cf9e"),
    ]

    result = compute_program_pnl(
        orders=orders,
        current_positions=[],
        daily_start=NOW - timedelta(hours=8),
        weekly_start=NOW - timedelta(days=1),
        account_daily_pnl=Decimal("52"),
        account_weekly_pnl=Decimal("52"),
        as_of=NOW,
    )

    assert result.daily.program_gross_pnl == Decimal("52")
    assert result.daily.external_gross_pnl == Decimal("0")
    assert result.daily.account_adjustment == Decimal("0")
    assert result.daily.program_sell_contracts == Decimal("2")
    assert result.daily.external_sell_contracts == Decimal("0")
    # 幽灵持仓：旧实现里这张合约会一直挂在本程序未平数量上
    assert result.program_open_qty == {}


def test_external_sell_covered_by_own_buy_never_touches_program_position():
    """外部自己买、自己卖的那一轮，不许去动本程序的仓。

    与上一条是同一段归属逻辑的两面：只有外部卖出量超过它自己买进量时，超出的
    部分才算平到了本程序头上。
    """
    symbol = "QQQ260727C00685000"
    orders = [
        _order("program-entry", symbol, "buy", 2, "1.00", 6, "ttt-entry"),
        _order("external-entry", symbol, "buy", 3, "1.00", 5, "external"),
        # 外部卖 4 张：3 张是它自己的，第 4 张才落到本程序头上
        _order("external-sell", symbol, "sell", 4, "1.10", 4, "external"),
    ]

    result = compute_program_pnl(
        orders=orders,
        current_positions=[SimpleNamespace(symbol=symbol, qty="1", current_price="1.30")],
        daily_start=NOW - timedelta(hours=8),
        weekly_start=NOW - timedelta(days=1),
        account_daily_pnl=Decimal("40"),
        account_weekly_pnl=Decimal("40"),
        as_of=NOW,
    )

    # 本程序：买 2 张 -200，被平掉 1 张 +110，剩 1 张按现价 mark +130
    assert result.daily.program_gross_pnl == Decimal("40")
    # 外部：买 3 张 -300，卖出自己的 3 张 +330
    assert result.daily.external_gross_pnl == Decimal("30")
    assert result.program_open_qty == {symbol: Decimal("1")}
