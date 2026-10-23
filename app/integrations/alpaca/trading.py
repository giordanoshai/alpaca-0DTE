"""Alpaca 订单侧（PAPER）—— 下单/撤单/查询/持仓。共享 client.CLIENTS 的 TradingClient。"""
from __future__ import annotations

from app.broker_pnl import PROGRAM_CLIENT_ORDER_PREFIX
from app.integrations.alpaca.client import CLIENTS


class AlpacaTrading:
    """Alpaca PAPER 下单/撤单/查询封装（⑤ 提交侧）。"""

    def __init__(self):
        self._tc = CLIENTS.trading()

    def submit(self, req) -> dict:
        o = self._tc.submit_order(req)
        return {"id": str(o.id),
                "status": str(o.status).rsplit(".", 1)[-1].lower(),
                "symbol": o.symbol,
                "side": str(o.side), "qty": str(o.qty),
                "filled_qty": str(o.filled_qty or 0),
                "filled_avg_price": (
                    str(o.filled_avg_price) if o.filled_avg_price is not None else None
                ),
                "client_order_id": str(o.client_order_id or ""),
                "position_intent": (
                    str(o.position_intent) if getattr(o, "position_intent", None) else None
                )}

    def get_order(self, order_id: str) -> dict:
        o = self._tc.get_order_by_id(order_id)
        return {"id": str(o.id),
                "status": str(o.status).rsplit(".", 1)[-1].lower(),
                "filled_qty": str(o.filled_qty),
                "filled_avg_price": (
                    str(o.filled_avg_price) if o.filled_avg_price is not None else None
                ),
                "symbol": o.symbol, "side": str(o.side), "qty": str(o.qty),
                "client_order_id": str(o.client_order_id or ""),
                "position_intent": (
                    str(o.position_intent) if getattr(o, "position_intent", None) else None
                )}

    def cancel(self, order_id: str) -> None:
        self._tc.cancel_order_by_id(order_id)

    def position_qty(self, symbol: str) -> int:
        for p in self._tc.get_all_positions():
            if p.symbol == symbol.upper():
                return int(float(p.qty))
        return 0

    def open_order_ids_for(self, symbol: str) -> list[str]:
        """只返回本程序拥有的同合约在途单，绝不撤其他系统/人工订单。"""
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        return [
            str(o.id)
            for o in self._tc.get_orders(GetOrdersRequest(
                status=QueryOrderStatus.OPEN,
                symbols=[symbol.upper()],
                limit=500,
            ))
            if o.symbol == symbol.upper()
            and str(getattr(o, "client_order_id", "") or "").startswith(
                PROGRAM_CLIENT_ORDER_PREFIX
            )
        ]

    def open_sell_orders_for(self, symbol: str) -> list[dict]:
        """同合约全部在途卖单及归属，供自动止损提交前做防超卖检查。"""
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        result = []
        orders = self._tc.get_orders(GetOrdersRequest(
            status=QueryOrderStatus.OPEN,
            symbols=[symbol.upper()],
        ))
        for order in orders:
            side = str(getattr(order, "side", "") or "").rsplit(".", 1)[-1].lower()
            if order.symbol != symbol.upper() or side != "sell":
                continue
            client_order_id = str(getattr(order, "client_order_id", "") or "")
            result.append({
                "id": str(order.id),
                "client_order_id": client_order_id,
                "owned": client_order_id.startswith(PROGRAM_CLIENT_ORDER_PREFIX),
            })
        return result

    def broker_order_session(self, after=None, until=None, limit: int = 500) -> list:
        """当前 ET 自然日终态订单 + 全部仍在途订单，按 Alpaca order id 去重。

        TradingStream 不提供历史回放或按时间订阅。REST 只在启动/WS 重连时建立这个小基线，
        随后的状态变化全部走 WS。单独补查 ``OPEN`` 是为了保留跨日挂单。
        """
        from alpaca.common.enums import Sort
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        session_orders = []
        if after is not None and until is not None and after < until:
            session_orders = self._tc.get_orders(GetOrdersRequest(
                status=QueryOrderStatus.ALL,
                after=after,
                until=until,
                direction=Sort.DESC,
                limit=limit,
            ))
        open_orders = self._tc.get_orders(GetOrdersRequest(
            status=QueryOrderStatus.OPEN,
            direction=Sort.DESC,
            limit=limit,
        ))
        merged = {}
        for order in [*session_orders, *open_orders]:
            merged[str(order.id)] = order
        return list(merged.values())

    def broker_position_symbols(self) -> set[str]:
        return {p.symbol for p in self._tc.get_all_positions()}

    def broker_position_qtys(self) -> dict[str, int]:
        """{合约代码: 券商侧真实张数}。

        watchdog 对账用：只比对「合约在不在」看不见张数漂移（2 手卖 1 手、卖单没成交时
        合约一直在），而本地 qty 错了会让 atomic_close 的 target 算错、全平卖错数量。
        """
        return {p.symbol: int(float(p.qty)) for p in self._tc.get_all_positions()}

    def broker_positions(self) -> list:
        """账户全部持仓对象（供对账/未登记仓扫描，替代 router 里 ec._tc.get_all_positions() 私有穿透）。"""
        return self._tc.get_all_positions()

    def closed_buy_orders(self, symbols, limit: int = 200) -> list:
        """指定合约的已成交买入单（供 reconcile 找回入场基准）。"""
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest
        return self._tc.get_orders(GetOrdersRequest(
            status=QueryOrderStatus.CLOSED, symbols=list(symbols), side="buy", limit=limit))
