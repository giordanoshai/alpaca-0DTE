# -*- coding: utf-8 -*-
"""atomic_close 单发回退路径（price_type≠bid / 显式限价 / chase=False）必须先确认成交，
再报 closed。此前它 submit() 后立即谎报 closed=True，导致券商侧挂单未成交、本系统却显示
「已成交」（严重资金 BUG）。未成交 → 如实回报 resting（挂单在券商排队），非 closed、非 error。
"""
from decimal import Decimal as D

import app.order_pricing as pricing
from app.order_pricing import atomic_close

_CODE = "QQQ260716C00500000"


class _FakeEC:
    """position_qty 按 qtys 序列返回（末值保持）；submit 固定 id=s1。"""

    def __init__(self, qtys, fill_px="1.05"):
        self.qtys = list(qtys)
        self.fill_px = fill_px
        self.submitted = []
        self.cancelled = []

    def open_order_ids_for(self, symbol):
        return []

    def cancel(self, oid):
        self.cancelled.append(oid)

    def submit(self, req):
        self.submitted.append(req)
        return {"id": "s1"}

    def position_qty(self, symbol):
        v = self.qtys[0]
        if len(self.qtys) > 1:
            self.qtys.pop(0)
        return v

    def get_order(self, oid):
        filled = self.qtys[0] == 0
        return {
            "status": "filled" if filled else "new",
            "filled_qty": "2" if filled else "0",
            "filled_avg_price": self.fill_px if filled else None,
        }


class _FakeAlpaca:
    def __init__(self, quote):
        self._q = quote

    def option_quote(self, symbol, fresh=False):
        return self._q


def test_fallback_unfilled_returns_resting_not_closed():
    """卖一(ask)被动挂单未成交 → resting=True、closed=False，绝不谎报已成交。"""
    ec = _FakeEC(qtys=[2])  # 持仓始终 2，从不下降 → 未成交
    alpaca = _FakeAlpaca((D("1.00"), D("1.10")))
    res = atomic_close(ec, alpaca, _CODE, 2, "手动平仓",
                       price_type="ask", wait_s=0.03, poll_s=0.01)
    assert res["closed"] is False
    assert res.get("resting") is True
    assert res["order"] == {"id": "s1"}
    assert "error" not in res  # 挂单排队不是错误


def test_fallback_filled_returns_closed_with_fill():
    """卖一挂单在窗口内成交（持仓降到目标）→ closed=True 且带真实成交价。"""
    ec = _FakeEC(qtys=[2, 0])  # 提交前有 2 张；券商订单随后明确报告已成交
    alpaca = _FakeAlpaca((D("1.00"), D("1.10")))
    res = atomic_close(ec, alpaca, _CODE, 2, "手动平仓",
                       price_type="ask", wait_s=0.2, poll_s=0.01)
    assert res["closed"] is True
    assert res.get("fill") == "1.05"


def test_fallback_resting_flags_passive_for_ask():
    """卖一(ask)挂单排队 → resting 结果标 passive=True。

    上层据此如实告诉用户"这单是被动排队、可能不会成交"，而不是让他以为已经卖掉了
    （2026-07-25 QQQ 减仓事件：前端把买入用的 ask 传给平仓接口，用户以为卖了、券商仍 2 手）。
    """
    ec = _FakeEC(qtys=[2])
    alpaca = _FakeAlpaca((D("1.00"), D("1.10")))
    res = atomic_close(ec, alpaca, _CODE, 2, "手动平仓",
                       price_type="ask", wait_s=0.03, poll_s=0.01)
    assert res.get("resting") is True
    assert res.get("passive") is True


def test_fallback_resting_not_passive_for_explicit_limit():
    """显式限价是调用方自己定的价位，不替他判被动 → passive=False。"""
    ec = _FakeEC(qtys=[2])
    alpaca = _FakeAlpaca((D("1.00"), D("1.10")))
    res = atomic_close(ec, alpaca, _CODE, 2, "手动平仓",
                       price_type="limit", limit_price=D("0.90"), wait_s=0.03, poll_s=0.01)
    assert res.get("resting") is True
    assert res.get("passive") is False


def test_fallback_no_quote_still_errors():
    """取不到报价 → error（无法平仓），保持既有行为。"""
    ec = _FakeEC(qtys=[2])
    alpaca = _FakeAlpaca(None)
    res = atomic_close(ec, alpaca, _CODE, 2, "手动平仓", price_type="ask")
    assert res["closed"] is False
    assert res.get("resting") is not True
    assert "error" in res


def test_fallback_audits_broker_order_and_resting_state(monkeypatch):
    events = []
    monkeypatch.setattr(
        pricing.trade_recorder, "audit",
        lambda event, **fields: events.append((event, fields)),
    )
    ec = _FakeEC(qtys=[2])
    alpaca = _FakeAlpaca((D("1.00"), D("1.10")))

    atomic_close(
        ec, alpaca, _CODE, 2, "手动平仓",
        price_type="ask", wait_s=0.03, poll_s=0.01,
    )

    assert [event for event, _ in events] == [
        "trade_close_order_submitted",
        "trade_close_order_status",
    ]
    assert events[0][1]["close_order_id"] == "s1"
    assert events[0][1]["limit_price"] == D("1.10")
    assert events[0][1]["attempt"] == 1
    assert events[1][1]["status"] == "resting"
