"""买入请求进入券商前的轻量受理状态。"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class BuyIntent:
    intent_id: str
    ticker: str
    direction: str
    status: str = "processing"
    reason: str | None = None
    result: dict | None = None
    ts: datetime = field(default_factory=_utcnow)
    # 浏览器侧 RTT 只在**首次看到终态的那一拍**落一条日志；前端每 500ms 轮一次，
    # 不去重就会给每笔单刷十几行。不进 to_dict()，纯服务端记账。
    client_timing_logged: bool = False

    def to_dict(self) -> dict:
        return {
            "intent_id": self.intent_id,
            "ticker": self.ticker,
            "direction": self.direction,
            "status": self.status,
            "reason": self.reason,
            "result": self.result,
        }


class BuyIntentTracker:
    def __init__(self) -> None:
        self._items: dict[str, BuyIntent] = {}

    def create(self, ticker: str, direction: str) -> BuyIntent:
        intent = BuyIntent(
            intent_id="BI" + uuid.uuid4().hex[:10],
            ticker=ticker.upper(),
            direction=direction,
        )
        self._items[intent.intent_id] = intent
        return intent

    def get(self, intent_id: str) -> BuyIntent | None:
        return self._items.get(intent_id)

    def resolve(
        self,
        intent_id: str,
        *,
        status: str,
        reason: str | None = None,
        result: dict | None = None,
    ) -> None:
        intent = self._items.get(intent_id)
        if intent is not None:
            intent.status = status
            intent.reason = reason
            intent.result = result

    def clear(self) -> None:
        self._items.clear()


BUY_INTENTS = BuyIntentTracker()
