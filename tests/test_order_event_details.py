# -*- coding: utf-8 -*-
"""⑦ 订单流要能看出「挂的什么价、多少钱、为什么没成」（审查 2.9）。

用户 2026-07-25：「在待成交的页面，看不到这个挂单的是哪一个价格类型，这个我得看到。
还要看到这个订单的总金额是多少。现在只看到10手，还有STRIK的价格，和标的。信息量太少了。」
以及：「在成交和未成交的分类中，都要展示是什么东西导致成交了/没成交」。

这不是前端能补的 —— `OrderEvent` 里根本没存这些字段。
notional 由 price×100×qty 现算（不落冗余字段，避免两处口径漂移）。
"""
from decimal import Decimal as D

from app.order_state import OrderJournal


def _j():
    return OrderJournal()


def test_submitted_records_price_type_and_limit():
    """挂的是哪一档、委托价多少 —— 待成交面板的核心信息。"""
    j = _j()
    ev = j.submitted("o1", "QQQ", "QQQ260716C00500000", "buy", 10,
                     price=D("1.20"), price_type="ask", limit_price=D("1.22"))
    d = ev.to_dict()
    assert d["price_type"] == "ask"
    assert d["limit_price"] == "1.22"


def test_notional_computed_from_limit_price():
    """总金额优先用**委托价**算：待成交时这才是要付的钱（10 张 × 1.22 × 100 = $1220）。"""
    j = _j()
    ev = j.submitted("o1", "QQQ", "C", "buy", 10, price=D("1.20"), limit_price=D("1.22"))
    assert ev.to_dict()["notional"] == "1220.00"


def test_notional_falls_back_to_price_when_no_limit():
    """没有委托价（如系统平仓单）→ 退回用 price 估算，而不是留空。"""
    j = _j()
    ev = j.submitted("o1", "QQQ", "C", "sell", 2, price=D("1.50"))
    assert ev.to_dict()["notional"] == "300.00"


def test_notional_is_none_when_no_price_at_all():
    """两个价都没有 → None（如实，不臆造金额）。"""
    j = _j()
    ev = j.submitted("o1", "QQQ", "C", "sell", 2)
    assert ev.to_dict()["notional"] is None


def test_cancel_records_fail_reason():
    """未成交的单要说清楚为什么：超时？手动撤？券商拒？"""
    j = _j()
    j.submitted("o1", "QQQ", "C", "buy", 1)
    j.mark_cancelled("o1", fail_reason="挂单超时 30 秒未成交，自动撤单")
    d = j.today()["cancelled"][0]
    assert d["fail_reason"] == "挂单超时 30 秒未成交，自动撤单"


def test_cancel_without_reason_keeps_none():
    j = _j()
    j.submitted("o1", "QQQ", "C", "buy", 1)
    j.mark_cancelled("o1")
    assert j.today()["cancelled"][0]["fail_reason"] is None


def test_fill_price_overrides_notional():
    """成交后总金额按**真实成交价**重算，不能还挂着委托估价。"""
    j = _j()
    j.submitted("o1", "QQQ", "C", "buy", 10, price=D("1.20"), limit_price=D("1.22"))
    j.mark_filled("o1", price=D("1.25"))
    d = j.today()["filled"][0]
    assert d["price"] == "1.25"
    assert d["notional"] == "1250.00"


def test_existing_fields_unchanged():
    """既有字段一个不能少（前端订单流面板依赖它们）。"""
    j = _j()
    ev = j.submitted("o1", "QQQ", "QQQ260716C00500000", "sell", 2, reason="watchdog:超时 15分")
    d = ev.to_dict()
    for k in ("order_id", "ticker", "option_code", "side", "qty", "status",
              "price", "reason", "ts", "updated_ts"):
        assert k in d, f"缺字段 {k}"
    assert d["reason"] == "watchdog:超时 15分"
