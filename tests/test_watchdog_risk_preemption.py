"""已有卖单时的 watchdog 风险抢占状态机。"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

import app.watchdog as watchdog
from app.order_state import JOURNAL, REGISTRY, ManagedPosition

_CODE = "QQQ260731C00500000"


class _Broker:
    def __init__(self, order=None, *, cancel_error=None, open_sells=None):
        self.order = order or {"status": "accepted", "filled_qty": "0"}
        self.cancel_error = cancel_error
        self.open_sells = open_sells or []
        self.cancelled = []

    def broker_position_qtys(self):
        pos = REGISTRY.get(_CODE)
        return {_CODE: pos.qty} if pos and pos.qty > 0 else {}

    def get_order(self, order_id):
        return dict(self.order)

    def cancel(self, order_id):
        if self.cancel_error:
            raise RuntimeError(self.cancel_error)
        self.cancelled.append(order_id)

    def open_sell_orders_for(self, option_code):
        if isinstance(self.open_sells, Exception):
            raise self.open_sells
        return list(self.open_sells)


def _source(quote):
    return SimpleNamespace(
        name="watcher",
        alpaca=SimpleNamespace(option_quote=lambda code: quote),
        underlying_price=lambda ticker: None,
    )


def _seed(*, mode="amount", age_s=60, max_hold_s=900, with_target=True):
    pos = ManagedPosition(
        option_code=_CODE,
        ticker="QQQ",
        direction="long",
        qty=3,
        entry_mid=D("1.00"),
        entry_time=datetime.now(timezone.utc) - timedelta(seconds=age_s),
        order_id="entry-1",
        status="closing" if with_target else "open",
        stop_mode=mode,
        stop_pct=D("0.20"),
        dollar_line=D("80"),
        max_hold_s=max_hold_s,
    )
    if with_target:
        pos.close_order_id = "target-1"
        pos.close_qty = 3
        pos.close_reason = "目标止盈"
        pos.close_submitted_at = datetime.now(timezone.utc)
        pos.close_timeout_policy = "persistent"
        JOURNAL.submitted(
            "target-1", "QQQ", _CODE, "sell", 3,
            reason="目标止盈", origin="manual", action="target_close",
        )
    REGISTRY.record(pos)
    return pos


@pytest.mark.parametrize(
    ("mode", "quote", "age_s", "max_hold_s", "reason_part"),
    [
        ("amount", (D("0.50"), D("0.52")), 60, 900, "$止损"),
        ("percent", (D("0.70"), D("0.72")), 60, 900, "%止损"),
        ("percent", None, 120, 30, "超时"),
    ],
)
def test_risk_lines_preempt_persistent_target_then_close(
    monkeypatch, mode, quote, age_s, max_hold_s, reason_part,
):
    pos = _seed(mode=mode, age_s=age_s, max_hold_s=max_hold_s)
    pos.last_owned_buy_fill_at = datetime.now(timezone.utc)
    broker = _Broker()
    submitted = []
    executor_contexts = []
    monkeypatch.setattr(watchdog, "AlpacaTrading", lambda: broker)
    monkeypatch.setattr(watchdog.OPTION_STREAM, "quote", lambda code: quote)
    monkeypatch.setattr(
        watchdog,
        "atomic_close",
        lambda *args, **kwargs: (
            executor_contexts.append(kwargs)
            or submitted.append(args[3])
            or {
                "closed": True,
                "filled_qty": args[3],
                "fill": "0.49",
                "order": {"id": "stop-1"},
            }
        ),
    )

    watchdog._watchdog_tick(_source(quote))
    assert broker.cancelled == ["target-1"]
    assert submitted == []
    assert pos.risk_exit_state == "canceling_existing"
    assert reason_part in pos.risk_exit_reason
    assert pos.risk_exit_context["trigger_qty"] == 3
    assert JOURNAL.get("target-1").cancel_cause == "risk_preempt"

    broker.order = {"status": "canceled", "filled_qty": "0"}
    watchdog._watchdog_tick(_source(quote))
    assert submitted == [3]
    assert "trigger_qty" not in executor_contexts[0]["trigger_context"]
    assert executor_contexts[0]["confirmed_position_qty"] == 3
    assert (
        executor_contexts[0]["confirmed_position_at"]
        == pos.last_owned_buy_fill_at
    )
    assert pos.status == "closed"
    assert pos.risk_exit_state is None


def test_partial_fill_before_cancel_only_closes_remaining(monkeypatch):
    pos = _seed()
    broker = _Broker()
    submitted = []
    monkeypatch.setattr(watchdog, "AlpacaTrading", lambda: broker)
    monkeypatch.setattr(
        watchdog.OPTION_STREAM,
        "quote",
        lambda code: (D("0.50"), D("0.52")),
    )
    monkeypatch.setattr(
        watchdog,
        "atomic_close",
        lambda *args, **kwargs: (
            submitted.append(args[3])
            or {
                "closed": True,
                "filled_qty": args[3],
                "fill": "0.48",
                "order": {"id": "stop-remaining"},
            }
        ),
    )

    watchdog._watchdog_tick(_source((D("0.50"), D("0.52"))))
    broker.order = {
        "status": "canceled",
        "filled_qty": "1",
        "filled_avg_price": "1.20",
    }
    watchdog._watchdog_tick(_source((D("0.50"), D("0.52"))))

    assert submitted == [2]
    assert pos.status == "closed"


def test_target_fills_while_canceling_never_submits_stop(monkeypatch):
    pos = _seed()
    broker = _Broker()
    submitted = []
    monkeypatch.setattr(watchdog, "AlpacaTrading", lambda: broker)
    monkeypatch.setattr(
        watchdog.OPTION_STREAM,
        "quote",
        lambda code: (D("0.50"), D("0.52")),
    )
    monkeypatch.setattr(
        watchdog,
        "atomic_close",
        lambda *args, **kwargs: submitted.append(args[3]),
    )

    watchdog._watchdog_tick(_source((D("0.50"), D("0.52"))))
    broker.order = {
        "status": "filled",
        "filled_qty": "3",
        "filled_avg_price": "1.10",
    }
    watchdog._watchdog_tick(_source((D("0.50"), D("0.52"))))

    assert submitted == []
    assert pos.status == "closed"
    assert pos.risk_exit_state is None


def test_confirmed_cancel_is_not_blocked_by_stale_open_snapshot(monkeypatch):
    """get_order 已确认 canceled 后，同一 ID 在 OPEN 列表短暂残留不应制造一拍假 blocked。"""
    pos = _seed()
    broker = _Broker(open_sells=[{
        "id": "target-1",
        "client_order_id": "ttt-old",
        "owned": True,
    }])
    submitted = []
    terminal_ids = []
    monkeypatch.setattr(watchdog, "AlpacaTrading", lambda: broker)
    monkeypatch.setattr(
        watchdog.OPTION_STREAM,
        "quote",
        lambda code: (D("0.50"), D("0.52")),
    )
    monkeypatch.setattr(
        watchdog,
        "atomic_close",
        lambda *args, **kwargs: (
            submitted.append(args[3])
            or terminal_ids.append(kwargs["confirmed_terminal_order_ids"])
            or {
                "closed": True,
                "filled_qty": args[3],
                "fill": "0.49",
                "order": {"id": "stop-1"},
            }
        ),
    )

    watchdog._watchdog_tick(_source((D("0.50"), D("0.52"))))
    broker.order = {"status": "canceled", "filled_qty": "0"}
    watchdog._watchdog_tick(_source((D("0.50"), D("0.52"))))

    assert submitted == [3]
    assert terminal_ids == [{"target-1"}]
    assert pos.status == "closed"
    assert pos.close_error is None


def test_untracked_owned_sell_is_cleaned_by_atomic_close_instead_of_blocking(
    monkeypatch,
):
    """重启后本地丢了 close_order_id，Alpaca 的 ttt-* 卖单仍须由原子平仓安全接管。"""
    pos = _seed(with_target=False)
    broker = _Broker(open_sells=[{
        "id": "orphan-target",
        "client_order_id": "ttt-orphan",
        "owned": True,
    }])
    submitted = []
    monkeypatch.setattr(watchdog, "AlpacaTrading", lambda: broker)
    monkeypatch.setattr(
        watchdog.OPTION_STREAM,
        "quote",
        lambda code: (D("0.50"), D("0.52")),
    )
    monkeypatch.setattr(
        watchdog,
        "atomic_close",
        lambda *args, **kwargs: (
            submitted.append(args[3])
            or {
                "closed": True,
                "filled_qty": args[3],
                "fill": "0.49",
                "order": {"id": "stop-1"},
            }
        ),
    )

    watchdog._watchdog_tick(_source((D("0.50"), D("0.52"))))

    assert submitted == [3]
    assert pos.status == "closed"
    assert pos.close_error is None


def test_cancel_failure_and_pending_cancel_never_double_sell(monkeypatch):
    pos = _seed()
    broker = _Broker(cancel_error="broker unavailable")
    submitted = []
    monkeypatch.setattr(watchdog, "AlpacaTrading", lambda: broker)
    monkeypatch.setattr(
        watchdog.OPTION_STREAM,
        "quote",
        lambda code: (D("0.50"), D("0.52")),
    )
    monkeypatch.setattr(
        watchdog,
        "atomic_close",
        lambda *args, **kwargs: submitted.append(args[3]),
    )

    watchdog._watchdog_tick(_source((D("0.50"), D("0.52"))))
    watchdog._watchdog_tick(_source((D("0.50"), D("0.52"))))
    assert submitted == []
    assert pos.risk_exit_state == "canceling_existing"
    assert "撤销失败" in pos.close_error

    broker.cancel_error = None
    watchdog._watchdog_tick(_source((D("0.50"), D("0.52"))))
    broker.order = {"status": "pending_cancel", "filled_qty": "0"}
    watchdog._watchdog_tick(_source((D("0.50"), D("0.52"))))
    assert broker.cancelled == ["target-1"]
    assert submitted == []


def test_long_pending_cancel_surfaces_emergency_without_second_sell(monkeypatch):
    pos = _seed()
    broker = _Broker(order={"status": "pending_cancel", "filled_qty": "0"})
    submitted = []
    monkeypatch.setattr(watchdog, "AlpacaTrading", lambda: broker)
    monkeypatch.setattr(
        watchdog.OPTION_STREAM,
        "quote",
        lambda code: (D("0.50"), D("0.52")),
    )
    monkeypatch.setattr(
        watchdog,
        "atomic_close",
        lambda *args, **kwargs: submitted.append(args[3]),
    )

    watchdog._watchdog_tick(_source((D("0.50"), D("0.52"))))
    pos.risk_exit_context["triggered_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=61)
    )
    watchdog._watchdog_tick(_source((D("0.50"), D("0.52"))))

    assert submitted == []
    assert pos.risk_exit_state == "canceling_existing"
    assert "仍未获券商终态" in pos.close_error


def test_external_sell_order_blocks_new_risk_order(monkeypatch):
    pos = _seed(with_target=False)
    broker = _Broker(open_sells=[{
        "id": "external-sell",
        "client_order_id": "other-app",
        "owned": False,
    }])
    submitted = []
    monkeypatch.setattr(watchdog, "AlpacaTrading", lambda: broker)
    monkeypatch.setattr(
        watchdog.OPTION_STREAM,
        "quote",
        lambda code: (D("0.50"), D("0.52")),
    )
    monkeypatch.setattr(
        watchdog,
        "atomic_close",
        lambda *args, **kwargs: submitted.append(args[3]),
    )

    watchdog._watchdog_tick(_source((D("0.50"), D("0.52"))))

    assert submitted == []
    assert pos.status == "closing"
    assert pos.risk_exit_state == "closing_remaining"
    assert "非本程序在途卖单" in pos.close_error


def test_open_sell_scan_failure_stays_protected_and_does_not_submit(monkeypatch):
    pos = _seed(with_target=False)
    broker = _Broker(open_sells=RuntimeError("broker unavailable"))
    submitted = []
    monkeypatch.setattr(watchdog, "AlpacaTrading", lambda: broker)
    monkeypatch.setattr(
        watchdog.OPTION_STREAM,
        "quote",
        lambda code: (D("0.50"), D("0.52")),
    )
    monkeypatch.setattr(
        watchdog,
        "atomic_close",
        lambda *args, **kwargs: submitted.append(args[3]),
    )

    watchdog._watchdog_tick(_source((D("0.50"), D("0.52"))))

    assert submitted == []
    assert pos.status == "closing"
    assert pos.risk_exit_state == "closing_remaining"
    assert "核对在途卖单失败" in pos.close_error
