"""券商订单状态的无副作用归一化 helper。"""
from __future__ import annotations


def broker_status(order: dict | None) -> str:
    """Normalize SDK enums (``OrderStatus.FILLED``) and plain mock strings."""
    return str((order or {}).get("status") or "").rsplit(".", 1)[-1].lower()


def is_order_not_found(exc: Exception) -> bool:
    """只把券商明确返回的 404/查无此单识别为订单不存在。"""
    if getattr(exc, "status_code", None) == 404:
        return True
    message = str(exc).lower()
    return "404" in message or "not found" in message
