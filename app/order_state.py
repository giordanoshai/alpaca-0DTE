"""订单状态兼容门面。

新代码应按职责直接导入 ``order_journal``、``position_registry`` 或 ``buy_intent``。
"""
from app.buy_intent import BUY_INTENTS, BuyIntent, BuyIntentTracker
from app.order_journal import JOURNAL, OrderEvent, OrderJournal
from app.core.market_session import order_session_window
from app.position_registry import ManagedPosition, PositionRegistry, REGISTRY

__all__ = [
    "BUY_INTENTS",
    "BuyIntent",
    "BuyIntentTracker",
    "JOURNAL",
    "ManagedPosition",
    "OrderEvent",
    "OrderJournal",
    "PositionRegistry",
    "REGISTRY",
    "order_session_window",
]
