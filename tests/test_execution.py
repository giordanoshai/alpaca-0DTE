"""⑤ 执行层纯逻辑测试（定价 / 订单构造）。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

import app.order_pricing as pricing
from app.order_pricing import (
    atomic_close,
    build_limit_order,
    marketable_limit_price,
    price_for,
    reduce_qty,
)


# ---- 可成交限价定价 ----

def test_buy_uses_ask():
    assert marketable_limit_price("buy", D("1.00"), D("1.10")) == D("1.10")


def test_sell_uses_bid():
    assert marketable_limit_price("sell", D("1.00"), D("1.10")) == D("1.00")


def test_buy_chases_up_by_ticks():
    assert marketable_limit_price("buy", D("1.00"), D("1.10"), aggressive_ticks=1) == D("1.11")


def test_sell_chases_down_by_ticks():
    assert marketable_limit_price("sell", D("1.00"), D("1.10"), aggressive_ticks=2) == D("0.98")


def test_invalid_side_raises():
    with pytest.raises(ValueError):
        marketable_limit_price("hold", D("1"), D("2"))


# ---- ⑩ 价格类型（限价/买一bid/卖一ask/市价） ----

def test_price_for_bid_is_buy_one():
    # 买一 = bid，无论买卖
    assert price_for("buy", "bid", D("1.00"), D("1.10")) == D("1.00")
    assert price_for("sell", "bid", D("1.00"), D("1.10")) == D("1.00")


def test_price_for_ask_is_sell_one():
    assert price_for("buy", "ask", D("1.00"), D("1.10")) == D("1.10")
    assert price_for("sell", "ask", D("1.00"), D("1.10")) == D("1.10")


def test_price_for_market_buy_crosses_up():
    # 市价买 = ask + 追档（不裸市价）
    assert price_for("buy", "market", D("1.00"), D("1.10"), aggressive_ticks=1) == D("1.11")


def test_price_for_market_sell_crosses_down():
    # 市价卖 = bid − 追档 = 1.00 − 0.01 = 0.99（向下吃单确保成交）
    assert price_for("sell", "market", D("1.00"), D("1.10"), aggressive_ticks=1) == D("0.99")


def test_price_for_limit_uses_given_price():
    assert price_for("buy", "limit", D("1.00"), D("1.10"), limit_price=D("1.05")) == D("1.05")


def test_price_for_normalizes_sub_cent_quote_to_tick():
    """回归 2026-07-29 QQQ 4.059 被 Alpaca 拒绝：定价结果必须先变成 4.06。"""
    assert price_for("buy", "ask", D("4.01"), D("4.059")) == D("4.06")
    assert price_for(
        "buy", "limit", D("4.01"), D("4.05"), limit_price=D("4.059")
    ) == D("4.06")


def test_price_for_limit_without_price_raises():
    with pytest.raises(ValueError):
        price_for("buy", "limit", D("1.00"), D("1.10"))


def test_price_for_unknown_type_raises():
    with pytest.raises(ValueError):
        price_for("buy", "foo", D("1.00"), D("1.10"))


# ---- ⑪ 分批减仓张数（1/3、减半，小仓向上取整/全平） ----

_THIRD = D(1) / D(3)


def test_reduce_third_of_ten():
    assert reduce_qty(10, _THIRD) == 3        # round(3.33)=3


def test_reduce_half_of_ten():
    assert reduce_qty(10, D("0.5")) == 5


def test_reduce_third_of_three():
    assert reduce_qty(3, _THIRD) == 1


def test_reduce_third_of_two_rounds_to_one():
    assert reduce_qty(2, _THIRD) == 1         # round(0.67)=1


def test_reduce_one_lot_sells_all():
    assert reduce_qty(1, _THIRD) == 1         # 不足 1 手 → 至少 1（=全平）


def test_reduce_caps_at_total():
    assert reduce_qty(2, D("0.9")) == 2       # round(1.8)=2，不超过 total


def test_reduce_zero_total():
    assert reduce_qty(0, _THIRD) == 0


# ---- 订单构造 ----

def test_build_limit_order_fields():
    from alpaca.trading.enums import OrderSide, PositionIntent, TimeInForce
    req = build_limit_order("SPY", "buy", 2, D("1.10"), extended_hours=True)
    assert req.symbol == "SPY"
    assert req.qty == 2
    assert req.side == OrderSide.BUY
    assert req.time_in_force == TimeInForce.DAY
    assert D(str(req.limit_price)) == D("1.10")
    assert req.extended_hours is True
    assert req.position_intent == PositionIntent.BUY_TO_OPEN


def test_build_limit_order_sell_side():
    from alpaca.trading.enums import OrderSide, PositionIntent
    req = build_limit_order("QQQ", "sell", 1, D("2.00"), extended_hours=False)
    assert req.side == OrderSide.SELL
    assert req.extended_hours is False
    assert req.position_intent == PositionIntent.SELL_TO_CLOSE


def test_build_limit_order_normalizes_price_as_final_broker_guard():
    """即使调用方绕过 price_for，券商请求也不能携带三位小数。"""
    req = build_limit_order(
        "QQQ260729P00667000", "buy", 1, D("4.059"), extended_hours=False
    )
    assert D(str(req.limit_price)) == D("4.06")


# ---- atomic_close 返回 mid（⑧ 平仓落库 exit_mid 来源） ----

def test_atomic_close_returns_mid():
    """单发回退（chase=False）：撤在途 → 一次性挂 bid → 券商确认成交（持仓归零）→ 返回 mid 供 ⑧ 落库。

    注：修「谎报成交」后，单发回退也须先确认成交才报 closed（submit≠成交）；此处 FakeEc 模拟已成交。
    """
    from decimal import Decimal as D
    from app.order_pricing import atomic_close

    class FakeEc:
        def __init__(self):
            self.qtys = [1, 0]  # target=0；poll 读到 0 → 已成交
        def open_order_ids_for(self, code):
            return []
        def submit(self, req):
            return {"id": "sell-1"}
        def position_qty(self, code):
            v = self.qtys[0]
            if len(self.qtys) > 1:
                self.qtys.pop(0)
            return v
        def get_order(self, oid):
            return {"status": "filled", "filled_qty": "1",
                    "filled_avg_price": "2.20"}

    class FakeAlpaca:
        def option_quote(self, code, fresh=False):
            return (D("2.00"), D("2.40"))    # mid = 2.20

    res = atomic_close(FakeEc(), FakeAlpaca(), "QQQ260719C00500000", 1, "手动平仓",
                       chase=False, wait_s=0.05, poll_s=0.01)
    assert res["closed"] is True
    assert res["mid"] == "2.20"


# ---- 穿价 + 追单（Layer 1 系统主止损，修 #3） ----

class _ChaseEc:
    """成交时点由"第几手提交后成交"驱动（与轮询次数解耦，稳定可测）。

    持仓恒为 start 张，直到累计提交手数 ≥ fill_after_submit，之后 position_qty→start−qty（已平）。
    fill_after_submit 极大 = 永不成交。
    """
    def __init__(self, fill_after_submit, start=1, qty=1, fill_price="1.19"):
        self._fill_after = fill_after_submit
        self._start = start
        self._qty = qty
        self.submits = []        # 每手 (qty, limit)
        self.cancels = 0
        self.cancelled_ids = set()
        self._fill_price = fill_price

    def open_order_ids_for(self, code):
        return []

    def position_qty(self, code):
        return self._start - self._qty if len(self.submits) >= self._fill_after else self._start

    def submit(self, req):
        self.submits.append((int(req.qty), str(req.limit_price)))
        return {"id": f"sell-{len(self.submits)}"}

    def get_order(self, oid):
        order_no = int(str(oid).rsplit("-", 1)[-1])
        filled = (
            len(self.submits) >= self._fill_after
            and order_no == len(self.submits)
        )
        return {
            "id": oid,
            "status": (
                "filled" if filled
                else "canceled" if oid in self.cancelled_ids
                else "new"
            ),
            "filled_qty": str(self._qty if filled else 0),
            "filled_avg_price": self._fill_price if filled else None,
        }

    def cancel(self, oid):
        self.cancels += 1
        self.cancelled_ids.add(oid)


def _fast(**kw):
    kw.setdefault("wait_s", 0.02)
    kw.setdefault("poll_s", 0.005)
    return kw


def test_chase_liquid_fills_first_attempt():
    """厚票 spread=1 档：第 1 手挂 bid（cross≈0），一拍即成，不追不撤。"""
    from decimal import Decimal as D
    from app.order_pricing import atomic_close

    class FakeAlpaca:
        def option_quote(self, code, fresh=False):
            return (D("1.79"), D("1.80"))    # spread 1 档

    ec = _ChaseEc(fill_after_submit=1, fill_price="1.79")   # 第 1 手即成
    res = atomic_close(ec, FakeAlpaca(), "QQQ260721C00706000", 1, "止损", **_fast())
    assert res["closed"] is True
    assert res["attempts"] == 1
    assert ec.cancels == 0
    assert ec.submits[0][1] == "1.79"          # cross=round(0.01×1/3)=0 → 挂 bid
    assert res["fill"] == "1.79"


def test_watchdog_trigger_quote_is_used_at_bid_without_refetch():
    """止损首手复用击穿当刻盘口：不等第二次 REST，也不主动把限价压到 bid 以下。"""
    from datetime import datetime, timezone
    from decimal import Decimal as D
    from app.order_pricing import atomic_close
    from app.order_state import JOURNAL

    class NoRefetchAlpaca:
        def option_quote(self, code, fresh=False):
            raise AssertionError("首手不应重新取价")

    ec = _ChaseEc(fill_after_submit=1, fill_price="0.76")
    triggered_at = datetime(2026, 7, 28, 8, 41, 44, tzinfo=timezone.utc)
    res = atomic_close(
        ec, NoRefetchAlpaca(), "TSLA260728P00311000", 1, "watchdog:$止损",
        trigger_quote=(D("0.76"), D("0.82")),
        trigger_context={
            "triggered_at": triggered_at,
            "trigger_bid": D("0.76"),
            "trigger_ask": D("0.82"),
            "trigger_value": D("380"),
            "trigger_loss": D("130"),
            "stop_mode": "amount",
            "stop_threshold": D("100"),
            "entry_value": D("510"),
        },
        **_fast(),
    )

    assert res["closed"] is True
    assert ec.submits == [(1, "0.76")]
    assert res["attempts"] == 1
    event = JOURNAL.get("sell-1")
    assert event.triggered_at == triggered_at
    assert event.trigger_bid == D("0.76")
    assert event.trigger_loss == D("130")
    assert event.stop_mode == "amount"
    assert event.stop_threshold == D("100")


def test_chase_illiquid_deepens_until_fill():
    """薄票 bid=1.36/ask=1.51(spread0.15)：前两手不成→撤→第 3 手穿到 1.36−0.15=1.21 成交。"""
    from decimal import Decimal as D
    from app.order_pricing import atomic_close

    class FakeAlpaca:
        def option_quote(self, code, fresh=False):
            return (D("1.36"), D("1.51"))

    ec = _ChaseEc(fill_after_submit=3, fill_price="1.21")   # 第 3 手才成
    res = atomic_close(ec, FakeAlpaca(), "IONQ260721C00035000", 1, "止损", **_fast())
    assert res["closed"] is True
    assert res["attempts"] == 3
    # 三手 cross = round(0.15×{1,2,3}/3) = 0.05,0.10,0.15 → limit 1.31,1.26,1.21
    assert [s[1] for s in ec.submits] == ["1.31", "1.26", "1.21"]
    assert ec.cancels == 2                      # 前两手未成被撤
    assert res["fill"] == "1.21"


def test_only_first_watchdog_chase_order_keeps_trigger_snapshot():
    from datetime import datetime, timezone
    from app.order_pricing import atomic_close
    from app.order_state import JOURNAL

    class FakeAlpaca:
        def option_quote(self, code, fresh=False):
            return (D("1.36"), D("1.51"))

    triggered_at = datetime(2026, 7, 28, 8, 41, 44, tzinfo=timezone.utc)
    atomic_close(
        _ChaseEc(fill_after_submit=3, fill_price="1.21"),
        FakeAlpaca(), "IONQ260721C00035000", 1, "watchdog:%止损",
        trigger_quote=(D("1.36"), D("1.51")),
        trigger_context={
            "triggered_at": triggered_at,
            "trigger_bid": D("1.36"),
            "stop_mode": "percent",
            "stop_threshold": D("0.50"),
            "entry_value": D("300"),
        },
        **_fast(),
    )

    assert JOURNAL.get("sell-1").triggered_at == triggered_at
    assert JOURNAL.get("sell-2").triggered_at is None
    assert JOURNAL.get("sell-3").triggered_at is None


def test_chase_audits_each_submit_reprice_and_fill(monkeypatch):
    from decimal import Decimal as D
    import app.order_pricing as pricing

    class FakeAlpaca:
        def option_quote(self, code, fresh=False):
            return (D("1.36"), D("1.51"))

    events = []
    monkeypatch.setattr(
        pricing.trade_recorder, "audit",
        lambda event, **fields: events.append((event, fields)),
    )
    ec = _ChaseEc(fill_after_submit=3, fill_price="1.21")

    pricing.atomic_close(
        ec, FakeAlpaca(), "IONQ260721C00035000", 1, "watchdog:止损", **_fast())

    assert [event for event, _ in events] == [
        "trade_close_order_submitted",
        "trade_close_order_repriced",
        "trade_close_order_submitted",
        "trade_close_order_repriced",
        "trade_close_order_submitted",
        "trade_close_order_status",
    ]
    submitted = [fields for event, fields in events if event == "trade_close_order_submitted"]
    assert [fields["close_order_id"] for fields in submitted] == ["sell-1", "sell-2", "sell-3"]
    assert [fields["attempt"] for fields in submitted] == [1, 2, 3]
    assert events[-1][1]["status"] == "filled"
    assert events[-1][1]["fill_price"] == "1.21"


def test_chase_exhausts_returns_not_closed():
    """三手全不成交：closed=False、attempts=3，交回调用方（watchdog 下一拍再来）。"""
    from decimal import Decimal as D
    from app.order_pricing import atomic_close

    class FakeAlpaca:
        def option_quote(self, code, fresh=False):
            return (D("1.36"), D("1.51"))

    ec = _ChaseEc(fill_after_submit=99, fill_price=None)    # 永不成交
    res = atomic_close(ec, FakeAlpaca(), "IONQ260721C00035000", 1, "止损", **_fast())
    assert res["closed"] is False
    assert res["attempts"] == 3
    assert ec.cancels == 3
    assert res["remaining"] == 1


def test_chase_aggregates_partial_fills_by_own_order():
    """第一手成交 1/2、第二手成交余下 1 张时，数量与均价都按本系统订单加权。"""
    from decimal import Decimal as D
    from app.order_pricing import atomic_close

    class FakeAlpaca:
        def option_quote(self, code, fresh=False):
            return (D("1.30"), D("1.45"))

    class PartialEc:
        def __init__(self):
            self.submits = []
            self.cancels = []

        def open_order_ids_for(self, code):
            return []

        def position_qty(self, code):
            return 2

        def submit(self, req):
            oid = f"sell-{len(self.submits) + 1}"
            self.submits.append((oid, int(req.qty)))
            return {"id": oid}

        def get_order(self, oid):
            if oid == "sell-1":
                status = "canceled" if oid in self.cancels else "partially_filled"
                return {"status": status, "filled_qty": "1",
                        "filled_avg_price": "1.30"}
            return {"status": "filled", "filled_qty": "1",
                    "filled_avg_price": "1.20"}

        def cancel(self, oid):
            self.cancels.append(oid)

    ec = PartialEc()
    res = atomic_close(
        ec, FakeAlpaca(), "IONQ260721C00035000", 2, "止损", **_fast())

    assert res["closed"] is True
    assert res["filled_qty"] == 2
    assert D(res["fill"]) == D("1.25")
    assert [qty for _, qty in ec.submits] == [2, 1]
    assert ec.cancels == ["sell-1"]


def test_open_order_cleanup_only_returns_program_owned_orders():
    from types import SimpleNamespace
    from app.integrations.alpaca.trading import AlpacaTrading

    owned = SimpleNamespace(
        id="owned", symbol="QQQ260721C00500000", client_order_id="ttt-123")
    external = SimpleNamespace(
        id="external", symbol="QQQ260721C00500000", client_order_id="other-system")
    trading = AlpacaTrading.__new__(AlpacaTrading)
    trading._tc = SimpleNamespace(get_orders=lambda _request=None: [owned, external])

    assert trading.open_order_ids_for("QQQ260721C00500000") == ["owned"]


def test_chase_does_not_reprice_before_cancel_is_confirmed():
    """旧单仍是 pending_cancel/new 时禁止发下一张，避免两张同时成交造成超卖。"""
    from decimal import Decimal as D
    from app.order_pricing import atomic_close

    class FakeAlpaca:
        def option_quote(self, code, fresh=False):
            return (D("1.00"), D("1.10"))

    class SlowCancelEc:
        def __init__(self):
            self.submits = 0

        def open_order_ids_for(self, code):
            return []

        def position_qty(self, code):
            return 1

        def submit(self, req):
            self.submits += 1
            return {"id": "sell-1"}

        def get_order(self, oid):
            return {"status": "pending_cancel", "filled_qty": "0",
                    "filled_avg_price": None}

        def cancel(self, oid):
            pass

    ec = SlowCancelEc()
    res = atomic_close(
        ec, FakeAlpaca(), "QQQ260721C00500000", 1, "止损", **_fast())

    assert res["closed"] is False
    assert res["resting"] is True
    assert res["cancel_pending"] is True
    assert ec.submits == 1


def test_close_cleanup_cancel_failure_blocks_new_sell():
    """Alpaca 撤单调用失败时，旧单仍可能成交；必须停止而不是继续提交第二张卖单。"""
    class FakeEc:
        def position_qty(self, _code):
            return 1

        def open_order_ids_for(self, _code):
            return ["old-1"]

        def cancel(self, _oid):
            raise RuntimeError("cancel timeout")

        def submit(self, _req):
            raise AssertionError("撤单失败后不得提交新卖单")

    class FakeAlpaca:
        def option_quote(self, _code, fresh=False):
            return (D("1.00"), D("1.10"))

    res = atomic_close(
        FakeEc(), FakeAlpaca(), "QQQ260721C00500000", 1, "手动平仓",
        wait_s=0, poll_s=0,
    )

    assert res["closed"] is False
    assert res["cleanup_order_id"] == "old-1"
    assert "避免超卖" in res["error"]


def test_close_cleanup_waits_for_alpaca_terminal_before_new_sell():
    """DELETE 204 只代表受理；pending_cancel 不是终态，不能立刻重挂。"""
    class FakeEc:
        def __init__(self):
            self.submitted = False

        def position_qty(self, _code):
            return 1

        def open_order_ids_for(self, _code):
            return ["old-1"]

        def cancel(self, _oid):
            pass

        def get_order(self, _oid):
            return {"status": "pending_cancel"}

        def submit(self, _req):
            self.submitted = True
            raise AssertionError("pending_cancel 时不得提交新卖单")

    class FakeAlpaca:
        def option_quote(self, _code, fresh=False):
            return (D("1.00"), D("1.10"))

    ec = FakeEc()
    res = atomic_close(
        ec, FakeAlpaca(), "QQQ260721C00500000", 1, "手动平仓",
        wait_s=0, poll_s=0,
    )

    assert res["cleanup_pending"] is True
    assert ec.submitted is False


def test_close_cleanup_uses_one_shared_deadline_for_all_orders(monkeypatch):
    """全部旧单共享等待窗；第二张不能在第一张之后再获得完整 wait_s。"""
    class FakeClock:
        now = 0.0

        @classmethod
        def monotonic(cls):
            return cls.now

        @classmethod
        def sleep(cls, seconds):
            cls.now += seconds

    class FakeEc:
        submitted = False

        def position_qty(self, _code):
            return 2

        def open_order_ids_for(self, _code):
            return ["old-1", "old-2"]

        def cancel(self, _oid):
            pass

        def get_order(self, oid):
            terminal_at = {"old-1": 0.5, "old-2": 1.0}[oid]
            return {
                "status": (
                    "canceled"
                    if FakeClock.now >= terminal_at
                    else "pending_cancel"
                ),
                "filled_qty": "0",
            }

        def submit(self, _req):
            self.submitted = True
            raise AssertionError("共享 deadline 内仍有旧单未终态，不得提交新卖单")

    class FakeAlpaca:
        def option_quote(self, _code, fresh=False):
            return (D("1.00"), D("1.10"))

    monkeypatch.setattr(pricing.time, "monotonic", FakeClock.monotonic)
    monkeypatch.setattr(pricing.time, "sleep", FakeClock.sleep)
    ec = FakeEc()

    res = atomic_close(
        ec,
        FakeAlpaca(),
        "QQQ260721C00500000",
        2,
        "watchdog:止损",
        wait_s=0.6,
        poll_s=0.1,
    )

    assert res["cleanup_pending"] is True
    assert res["cleanup_order_ids"] == ["old-2"]
    assert FakeClock.now <= 0.6
    assert ec.submitted is False


def test_recent_confirmed_buy_qty_is_floor_for_stale_position_snapshot():
    """trade_updates 刚确认 2 张时，positions REST 的旧 1 张不能让止损少卖。"""
    class FakeEc:
        def __init__(self):
            self.submitted_qty = None

        def position_qty(self, _code):
            return 1

        def open_order_ids_for(self, _code):
            return []

        def submit(self, req):
            self.submitted_qty = int(req.qty)
            return {"id": "stop-1", "client_order_id": "ttt-stop"}

        def get_order(self, _oid):
            return {
                "status": "filled",
                "filled_qty": "2",
                "filled_avg_price": "0.90",
            }

    class FakeAlpaca:
        def option_quote(self, _code, fresh=False):
            return (D("0.90"), D("1.00"))

    ec = FakeEc()
    res = atomic_close(
        ec,
        FakeAlpaca(),
        "QQQ260721C00500000",
        2,
        "watchdog:止损",
        attempts=1,
        wait_s=0.05,
        poll_s=0,
        confirmed_position_qty=2,
        confirmed_position_at=datetime.now(timezone.utc),
    )

    assert res["closed"] is True
    assert ec.submitted_qty == 2
    assert res["filled_qty"] == 2


def test_expired_confirmed_qty_floor_does_not_override_broker_position():
    """宽限期过后恢复以 positions REST 为上限。"""
    class FakeEc:
        def __init__(self):
            self.submitted_qty = None

        def position_qty(self, _code):
            return 1

        def open_order_ids_for(self, _code):
            return []

        def submit(self, req):
            self.submitted_qty = int(req.qty)
            return {"id": "stop-1", "client_order_id": "ttt-stop"}

        def get_order(self, _oid):
            return {
                "status": "filled",
                "filled_qty": "1",
                "filled_avg_price": "0.90",
            }

    class FakeAlpaca:
        def option_quote(self, _code, fresh=False):
            return (D("0.90"), D("1.00"))

    ec = FakeEc()
    res = atomic_close(
        ec,
        FakeAlpaca(),
        "QQQ260721C00500000",
        2,
        "watchdog:止损",
        attempts=1,
        wait_s=0.05,
        poll_s=0,
        confirmed_position_qty=2,
        confirmed_position_at=(
            datetime.now(timezone.utc)
            - timedelta(seconds=pricing.POSITION_SETTLE_GRACE_S + 1)
        ),
    )

    assert res["closed"] is True
    assert ec.submitted_qty == 1
    assert res["filled_qty"] == 1


def test_close_ignores_only_explicitly_confirmed_terminal_id_from_stale_open_list():
    """逐笔 get_order 已确认终态时，可忽略 OPEN 列表短暂残留的同一 ID，但不放宽其它单。"""
    class FakeEc:
        def __init__(self):
            self.cancelled = []

        def position_qty(self, _code):
            return 1

        def open_order_ids_for(self, _code):
            return ["old-target"]

        def cancel(self, order_id):
            self.cancelled.append(order_id)

        def submit(self, _req):
            return {"id": "stop-1"}

        def get_order(self, order_id):
            assert order_id == "stop-1"
            return {
                "status": "filled",
                "filled_qty": "1",
                "filled_avg_price": "0.90",
            }

    class FakeAlpaca:
        def option_quote(self, _code, fresh=False):
            return (D("0.90"), D("1.00"))

    ec = FakeEc()
    res = atomic_close(
        ec,
        FakeAlpaca(),
        "QQQ260721C00500000",
        1,
        "watchdog:止损",
        confirmed_terminal_order_ids={"old-target"},
        wait_s=0.05,
        poll_s=0,
    )

    assert res["closed"] is True
    assert ec.cancelled == []


def test_close_ignores_post_cancel_open_snapshot_for_directly_confirmed_id():
    """本次 cleanup 已逐笔确认 canceled 后，同一 ID 的 OPEN 残影不得再次阻断新卖单。"""
    class FakeEc:
        def __init__(self):
            self.cancelled = []
            self.submitted = []

        def position_qty(self, _code):
            return 1

        def open_order_ids_for(self, _code):
            return ["old-target"]

        def cancel(self, order_id):
            self.cancelled.append(order_id)

        def submit(self, _req):
            self.submitted.append("stop-1")
            return {"id": "stop-1"}

        def get_order(self, order_id):
            if order_id == "old-target":
                return {"status": "canceled", "filled_qty": "0"}
            return {
                "status": "filled",
                "filled_qty": "1",
                "filled_avg_price": "0.90",
            }

    class FakeAlpaca:
        def option_quote(self, _code, fresh=False):
            return (D("0.90"), D("1.00"))

    ec = FakeEc()
    res = atomic_close(
        ec,
        FakeAlpaca(),
        "QQQ260721C00500000",
        1,
        "watchdog:止损",
        wait_s=0.05,
        poll_s=0,
    )

    assert res["closed"] is True
    assert ec.cancelled == ["old-target"]
    assert ec.submitted == ["stop-1"]


def test_close_accepts_cancel_race_when_direct_query_is_already_terminal():
    """DELETE 与终态竞态时以单笔 get_order 为准；已 canceled 不应误报撤单失败。"""
    class FakeEc:
        def __init__(self):
            self.submitted = []

        def position_qty(self, _code):
            return 1

        def open_order_ids_for(self, _code):
            return ["old-target"]

        def cancel(self, _order_id):
            raise RuntimeError("order is no longer cancelable")

        def submit(self, _req):
            self.submitted.append("stop-1")
            return {"id": "stop-1"}

        def get_order(self, order_id):
            if order_id == "old-target":
                return {"status": "canceled", "filled_qty": "0"}
            return {
                "status": "filled",
                "filled_qty": "1",
                "filled_avg_price": "0.90",
            }

    class FakeAlpaca:
        def option_quote(self, _code, fresh=False):
            return (D("0.90"), D("1.00"))

    ec = FakeEc()
    res = atomic_close(
        ec,
        FakeAlpaca(),
        "QQQ260721C00500000",
        1,
        "watchdog:止损",
        wait_s=0.05,
        poll_s=0,
    )

    assert res["closed"] is True
    assert ec.submitted == ["stop-1"]


def test_chase_fill_price_backfill_recomputes_weighted_average():
    """同一订单先报数量无均价、后补均价时，不得把无价张数按 0 元摊薄。"""
    class FakeEc:
        def __init__(self):
            self.frames = iter([
                {"status": "partially_filled", "filled_qty": "2",
                 "filled_avg_price": None},
                {"status": "partially_filled", "filled_qty": "2",
                 "filled_avg_price": "1.50"},
                {"status": "filled", "filled_qty": "5",
                 "filled_avg_price": "1.60"},
            ])

        def position_qty(self, _code):
            return 5

        def open_order_ids_for(self, _code):
            return []

        def submit(self, _req):
            return {"id": "sell-1"}

        def get_order(self, _oid):
            return next(self.frames)

    class FakeAlpaca:
        def option_quote(self, _code, fresh=False):
            return (D("1.60"), D("1.70"))

    res = atomic_close(
        FakeEc(), FakeAlpaca(), "QQQ260721C00500000", 5, "止损",
        attempts=1, wait_s=0.1, poll_s=0,
    )

    assert res["closed"] is True
    assert res["filled_qty"] == 5
    assert D(res["fill"]) == D("1.60")
