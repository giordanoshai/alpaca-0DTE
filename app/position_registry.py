"""本系统持仓登记。"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

_HUNDRED = Decimal("100")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class ManagedPosition:
    option_code: str
    ticker: str
    direction: str
    qty: int
    entry_mid: Decimal
    entry_time: datetime
    order_id: str
    status: str = "pending"
    realized_pnl: Decimal = Decimal("0")
    stop_pct: Decimal = Decimal("0.20")
    dollar_line: Decimal = Decimal("80")
    stop_mode: str = "percent"
    max_hold_s: float = 900.0
    close_error: str | None = None
    close_order_id: str | None = None
    close_qty: int = 0
    close_applied_qty: int = 0
    close_reason: str | None = None
    close_submitted_at: datetime | None = None
    cancel_requested: bool = False
    close_timeout_policy: str = "quick"
    risk_exit_state: str | None = None
    risk_exit_reason: str | None = None
    risk_exit_context: dict | None = None
    entry_filled_qty: int = 0
    entry_fill_mid: Decimal | None = None
    sync_note: str | None = None
    last_owned_buy_fill_at: datetime | None = None
    # `trade_watchdog_result` 的去重指纹与被吞条数。风险退出是按拍重试的，一个卡住
    # 的状态会把同一份内容复读成每秒一条（2026-08-14 INTC：46 条）。隔壁
    # `_audit_block_once` 早就有去重，这条一直没有。
    watchdog_audit_key: str | None = None
    watchdog_audit_repeats: int = 0

    @property
    def entry_val(self) -> Decimal:
        return self.entry_mid * _HUNDRED * Decimal(self.qty)


class PositionRegistry:
    def __init__(self) -> None:
        self._pos: dict[str, ManagedPosition] = {}
        self._lock = threading.RLock()

    def record(self, position: ManagedPosition) -> None:
        with self._lock:
            self._pos[position.option_code] = position

    def get(self, code: str) -> ManagedPosition | None:
        with self._lock:
            return self._pos.get(code)

    def by_order_id(self, order_id: str) -> ManagedPosition | None:
        with self._lock:
            return next(
                (position for position in self._pos.values()
                 if position.order_id == order_id),
                None,
            )

    def by_close_order_id(self, order_id: str) -> ManagedPosition | None:
        with self._lock:
            return next(
                (position for position in self._pos.values()
                 if position.close_order_id == order_id),
                None,
            )

    def active(self) -> list[ManagedPosition]:
        with self._lock:
            return [
                position
                for position in self._pos.values()
                if position.status in ("pending", "open", "closing")
            ]

    def open_positions(self) -> list[ManagedPosition]:
        with self._lock:
            return [
                position
                for position in self._pos.values()
                if position.status == "open"
            ]

    def set_status(self, code: str, status: str) -> None:
        with self._lock:
            position = self._pos.get(code)
            if position:
                position.status = status

    def compare_and_set_status(
        self,
        code: str,
        expected: str,
        status: str,
    ) -> bool:
        with self._lock:
            position = self._pos.get(code)
            if position is None or position.status != expected:
                return False
            position.status = status
            return True

    def add_qty(self, code: str, add_qty: int, new_mid: Decimal) -> None:
        with self._lock:
            position = self._pos.get(code)
            if position:
                total_qty = position.qty + add_qty
                position.entry_mid = (
                    position.entry_mid * Decimal(position.qty)
                    + new_mid * Decimal(add_qty)
                ) / Decimal(total_qty)
                position.qty = total_qty
                position.last_owned_buy_fill_at = _utcnow()

    def realize(
        self,
        code: str,
        sold: int,
        exit_mid: Decimal | None,
    ) -> Decimal:
        with self._lock:
            position = self._pos.get(code)
            if not position:
                return Decimal("0")
            actual_sold = min(max(0, int(sold)), position.qty)
            if exit_mid is not None and actual_sold > 0:
                position.realized_pnl += (
                    Decimal(str(exit_mid)) - position.entry_mid
                ) * _HUNDRED * Decimal(actual_sold)
            return position.realized_pnl

    def reduce(
        self,
        code: str,
        sold: int,
        exit_mid: Decimal | None = None,
    ) -> int:
        with self._lock:
            position = self._pos.get(code)
            if not position:
                return 0
            actual_sold = min(max(0, int(sold)), position.qty)
            self.realize(code, actual_sold, exit_mid)
            position.qty = max(0, position.qty - actual_sold)
            return position.qty

    def codes(self) -> set[str]:
        with self._lock:
            return set(self._pos)


REGISTRY = PositionRegistry()
