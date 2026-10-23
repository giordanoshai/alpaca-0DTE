"""订单读模型的数据对象。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

_HUNDRED = Decimal("100")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class OrderEvent:
    order_id: str
    ticker: str
    option_code: str
    side: str
    qty: int
    status: str = "pending"
    price: Decimal | None = None
    reason: str | None = None
    ts: datetime = field(default_factory=_utcnow)
    updated_ts: datetime = field(default_factory=_utcnow)
    price_type: str | None = None
    limit_price: Decimal | None = None
    fail_reason: str | None = None
    counts_toward_risk: bool = True
    origin: str = "unknown"
    action: str = "unknown"
    cancel_origin: str | None = None
    cancel_cause: str | None = None
    broker_status: str | None = None
    client_order_id: str | None = None
    parent_order_id: str | None = None
    triggered_at: datetime | None = None
    trigger_bid: Decimal | None = None
    trigger_ask: Decimal | None = None
    trigger_value: Decimal | None = None
    trigger_loss: Decimal | None = None
    stop_mode: str | None = None
    stop_threshold: Decimal | None = None
    entry_value: Decimal | None = None
    applied_qty: int = 0
    applied_notional: Decimal = Decimal("0")
    #: 终态审计是否已写过。纯进程内去重锚点，不进 to_dict()/不落库——重启后
    #: JOURNAL 由 DB 重建，届时补写一条审计也比整笔丢失好。
    terminal_audited: bool = False
    #: 最近一帧券商快照上的 updated_at（**券商时钟**），只用于判帧新旧。
    #: 不进 to_dict()/不落库，更不参与耗时计算 —— `ts`/`updated_ts` 必须同为本地
    #: 时钟，否则两者之差会混进机器间的时钟偏差。2026-08-03 的「成交耗时 0ms」
    #: 正是券商钟把 `updated_ts` 覆盖回 `ts` 造成的。
    broker_updated_ts: datetime | None = None

    def notional(self) -> Decimal | None:
        price = (
            self.price
            if self.status == "filled" and self.price is not None
            else self.limit_price if self.limit_price is not None else self.price
        )
        return (
            None
            if price is None
            else price * _HUNDRED * Decimal(self.qty)
        )

    def to_dict(self) -> dict:
        notional = self.notional()
        return {
            "order_id": self.order_id,
            "ticker": self.ticker,
            "option_code": self.option_code,
            "side": self.side,
            "qty": self.qty,
            "status": self.status,
            "price": None if self.price is None else str(self.price),
            "reason": self.reason,
            "price_type": self.price_type,
            "limit_price": (
                None if self.limit_price is None else str(self.limit_price)
            ),
            "notional": None if notional is None else f"{notional:.2f}",
            "fail_reason": self.fail_reason,
            "origin": self.origin,
            "action": self.action,
            "cancel_origin": self.cancel_origin,
            "cancel_cause": self.cancel_cause,
            "broker_status": self.broker_status,
            "client_order_id": self.client_order_id,
            "parent_order_id": self.parent_order_id,
            "triggered_at": (
                None if self.triggered_at is None else self.triggered_at.isoformat()
            ),
            "trigger_bid": None if self.trigger_bid is None else str(self.trigger_bid),
            "trigger_ask": None if self.trigger_ask is None else str(self.trigger_ask),
            "trigger_value": (
                None if self.trigger_value is None else str(self.trigger_value)
            ),
            "trigger_loss": (
                None if self.trigger_loss is None else str(self.trigger_loss)
            ),
            "stop_mode": self.stop_mode,
            "stop_threshold": (
                None if self.stop_threshold is None else str(self.stop_threshold)
            ),
            "entry_value": (
                None if self.entry_value is None else str(self.entry_value)
            ),
            "ts": self.ts.isoformat(),
            "updated_ts": self.updated_ts.isoformat(),
        }

    def to_wire_dict(self) -> dict:
        return {
            key: value
            for key, value in self.to_dict().items()
            if value is not None
        }
