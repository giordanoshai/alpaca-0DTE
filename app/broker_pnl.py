"""按 Alpaca 实际成交重建本程序与外部程序的盈亏。

订单归属只认券商订单上的 ``client_order_id=ttt-*``。没有此前缀的订单无论是否
曾写入旧表，一律按外部订单处理；不再通过合约、时间线或本地数据库猜测归属。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from app.core.dailylogger import get_logger

logger = get_logger("tt.gate")

PROGRAM_CLIENT_ORDER_PREFIX = "ttt-"

# 同一个 Alpaca 账户里**其他自营系统**的订单前缀。这些单有自己的 UI 与风控闭环，
# 0DTE 侧不显示、不跟踪、更不代撤——与「手动/外部单」不是一回事：手动单该看见
# （前端有「外部程序」标签），兄弟系统的单看见了只会招来误撤。
# 目前只有 SOS(nDTE)。**SOS 已于 2026-08-15 整体移出本项目（备份 D:\SOS_BAK），
# 但这条过滤必须留着**：券商账户里可能还残留 SOS 时期未终态的 `sos-` 挂单（典型是
# 止盈单），它们仍会从 TradingStream 流过来。删掉过滤，这些单就会出现在 0DTE 订单流
# 的「未成交」组里、每行旁边带撤单按钮——一键就能撤掉一个已无人看管的保护单。
SIBLING_PROGRAM_ORDER_PREFIXES = ("sos-",)

_MULTIPLIER = Decimal("100")


def sibling_program_of(client_order_id: str | None) -> str | None:
    """订单属于哪个兄弟系统；不属于任何兄弟系统（本程序单/手动单）返回 None。"""
    cid = str(client_order_id or "").lower()
    for prefix in SIBLING_PROGRAM_ORDER_PREFIXES:
        if cid.startswith(prefix):
            return prefix.rstrip("-").upper()
    return None


@dataclass(frozen=True)
class BrokerFill:
    client_order_id: str
    symbol: str
    side: str
    qty: Decimal
    price: Decimal
    filled_at: datetime


@dataclass(frozen=True)
class OwnedFill:
    owner: str  # "program" | "external"
    symbol: str
    side: str
    qty: Decimal
    price: Decimal
    filled_at: datetime


@dataclass(frozen=True)
class PnlBreakdown:
    program_gross_pnl: Decimal
    external_gross_pnl: Decimal
    account_net_pnl: Decimal
    account_adjustment: Decimal
    program_sell_contracts: Decimal
    external_sell_contracts: Decimal


@dataclass(frozen=True)
class ProgramPnlSnapshot:
    daily: PnlBreakdown
    weekly: PnlBreakdown
    program_open_qty: dict[str, Decimal]
    as_of: datetime


def _enum_text(value) -> str:
    raw = getattr(value, "value", value)
    return str(raw).lower().split(".")[-1]


def fills_from_orders(orders) -> list[BrokerFill]:
    """只保留有真实成交量、成交均价和成交时间的订单。"""
    fills: list[BrokerFill] = []
    for order in orders:
        qty = Decimal(str(getattr(order, "filled_qty", 0) or 0))
        price_raw = getattr(order, "filled_avg_price", None)
        filled_at = getattr(order, "filled_at", None)
        if qty <= 0 or price_raw is None or filled_at is None:
            continue
        fills.append(BrokerFill(
            client_order_id=str(getattr(order, "client_order_id", "") or ""),
            symbol=str(order.symbol).upper(),
            side=_enum_text(order.side),
            qty=qty,
            price=Decimal(str(price_raw)),
            filled_at=filled_at,
        ))
    return sorted(fills, key=lambda fill: fill.filled_at)


def classify_fills(
    fills: list[BrokerFill],
) -> tuple[list[OwnedFill], dict[str, Decimal]]:
    """给成交标记归属，并返回当前仍属于本程序的未平数量。

    带 ``ttt-`` 前缀的订单直接计入本程序。没有前缀的**卖单**要按数量分账：先冲抵
    外部自己买进的量，冲抵不掉的部分说明它平掉的是本程序的仓，那部分算本程序。

    这条分账规则是 2026-09-03 的实盘教训。当天程序买入 NVDA C227.5 两张 @2.79 后，
    平仓限价单被撤、持仓卡死（另修于 b99ad43），用户只能去 Alpaca App 市价清仓
    @3.05。这笔手动平仓单没有 ttt- 前缀，旧实现照外部单处理，于是买入的 558 记在
    本程序账上、收回的 610 不认 —— 仓位单腿悬空，当日「本程序盈亏」显示 -565，而
    账户实际 +44，且这张合约永远挂在 ``program_open_qty`` 里变成幽灵持仓。成本既然
    记了，收益就必须记回来，否则这个数字连喂给日内熔断闸都不安全。

    券商持仓按合约合并、没有编号可分，能做的只有按数量归属。冲抵顺序保证了外部
    自己买、自己卖的那一轮完整交易不会去动本程序的仓。兄弟系统（``sos-``）的卖单
    不参与分账：它们有独立的持仓与风控闭环，卖的是自己的仓。
    """
    program_qty: dict[str, Decimal] = {}
    external_qty: dict[str, Decimal] = {}
    owned: list[OwnedFill] = []

    def append(owner: str, fill: BrokerFill, qty: Decimal) -> None:
        if qty > 0:
            owned.append(OwnedFill(
                owner=owner,
                symbol=fill.symbol,
                side=fill.side,
                qty=qty,
                price=fill.price,
                filled_at=fill.filled_at,
            ))

    for fill in fills:
        symbol = fill.symbol
        tagged = fill.client_order_id.startswith(PROGRAM_CLIENT_ORDER_PREFIX)
        if not tagged:
            outside_qty = external_qty.get(symbol, Decimal(0))
            if fill.side == "buy":
                external_qty[symbol] = outside_qty + fill.qty
                append("external", fill, fill.qty)
                continue
            # 卖单：先卖外部自己手上的，剩下的才是在平本程序的仓。
            from_outside = min(outside_qty, fill.qty)
            external_qty[symbol] = outside_qty - from_outside
            remainder = fill.qty - from_outside
            if sibling_program_of(fill.client_order_id) is not None:
                remainder = Decimal(0)  # 兄弟系统卖的是自己的仓，不碰本程序
            open_qty = program_qty.get(symbol, Decimal(0))
            from_program = min(open_qty, remainder)
            if from_program > 0:
                program_qty[symbol] = open_qty - from_program
                append("program", fill, from_program)
            append("external", fill, fill.qty - from_program)
            continue

        append("program", fill, fill.qty)
        open_qty = program_qty.get(symbol, Decimal(0))
        if fill.side == "buy":
            program_qty[symbol] = open_qty + fill.qty
        elif fill.side == "sell":
            program_qty[symbol] = max(Decimal(0), open_qty - fill.qty)

    open_qty = {
        symbol: qty
        for symbol, qty in program_qty.items()
        if qty > 0
    }
    return owned, open_qty


def _period_breakdown(
    owned: list[OwnedFill],
    *,
    since: datetime,
    account_net_pnl: Decimal,
    program_marks: dict[str, Decimal],
    external_marks: dict[str, Decimal],
) -> PnlBreakdown:
    program = Decimal(0)
    external = Decimal(0)
    program_sells = Decimal(0)
    external_sells = Decimal(0)
    for fill in owned:
        if fill.filled_at < since:
            continue
        value = fill.qty * fill.price * _MULTIPLIER
        signed = value if fill.side == "sell" else -value
        if fill.owner == "program":
            program += signed
            if fill.side == "sell":
                program_sells += fill.qty
        else:
            external += signed
            if fill.side == "sell":
                external_sells += fill.qty
    program += sum(program_marks.values(), Decimal(0))
    external += sum(external_marks.values(), Decimal(0))
    adjustment = account_net_pnl - program - external
    return PnlBreakdown(
        program_gross_pnl=program,
        external_gross_pnl=external,
        account_net_pnl=account_net_pnl,
        account_adjustment=adjustment,
        program_sell_contracts=program_sells,
        external_sell_contracts=external_sells,
    )


def compute_program_pnl(
    *,
    orders,
    current_positions,
    daily_start: datetime,
    weekly_start: datetime,
    account_daily_pnl: Decimal,
    account_weekly_pnl: Decimal,
    as_of: datetime,
) -> ProgramPnlSnapshot:
    """用券商成交价/当前价拆分本程序、外部程序和账户调整。"""
    owned, program_open_qty = classify_fills(fills_from_orders(orders))

    position_by_symbol = {
        str(position.symbol).upper(): position
        for position in current_positions
    }
    program_marks: dict[str, Decimal] = {}
    external_marks: dict[str, Decimal] = {}
    for symbol, position in position_by_symbol.items():
        total_qty = Decimal(str(position.qty))
        current_price_raw = getattr(position, "current_price", None)
        if current_price_raw is None:
            raise RuntimeError(f"Alpaca position {symbol} 缺少 current_price")
        current_price = Decimal(str(current_price_raw))
        own_qty = min(program_open_qty.get(symbol, Decimal(0)), total_qty)
        other_qty = max(Decimal(0), total_qty - own_qty)
        if own_qty:
            program_marks[symbol] = own_qty * current_price * _MULTIPLIER
        if other_qty:
            external_marks[symbol] = other_qty * current_price * _MULTIPLIER

    # orders 与 positions 是两次独立 REST 调用：平仓卖单成交的那一拍，订单流可能
    # 还没含最后一笔 sell fill，而持仓快照里该合约已经归零。这是快照竞态而非数据
    # 损坏——按已平（mark 记 0）计入本拍即可，下一拍订单流补齐后自动恢复。
    # 绝不能 raise：那会让整份运行时状态刷新失败，前端 PnL 直接卡住一拍。
    missing_program_marks = set(program_open_qty) - set(position_by_symbol)
    if missing_program_marks:
        logger.warning(json.dumps({
            "event": "program_open_contract_missing_from_positions",
            "contracts": sorted(missing_program_marks),
            "note": "订单流与持仓快照暂不一致，本拍按已平计 mark=0",
        }, ensure_ascii=False))

    daily = _period_breakdown(
        owned,
        since=daily_start,
        account_net_pnl=account_daily_pnl,
        program_marks=program_marks,
        external_marks=external_marks,
    )
    weekly = _period_breakdown(
        owned,
        since=weekly_start,
        account_net_pnl=account_weekly_pnl,
        program_marks=program_marks,
        external_marks=external_marks,
    )
    return ProgramPnlSnapshot(
        daily=daily,
        weekly=weekly,
        program_open_qty=program_open_qty,
        as_of=as_of,
    )
