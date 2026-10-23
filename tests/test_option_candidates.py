"""闸门驱动候选协调器：WS Top1、换档先订新后退旧、硬闸立即释放。

本文件只覆盖 long 一侧的生命周期（这些行为与方向无关，两侧同构）；
CALL/PUT 双边并行的行为见 `test_option_candidates_dual_side.py`。
"""
from __future__ import annotations

import asyncio
from decimal import Decimal as D
from types import SimpleNamespace

from app.integrations.alpaca.data import SelectedContract
from app.option_stream import OptionQuoteStream
import app.services.option_candidates as option_candidates
from app.services.option_candidates import OptionCandidateCoordinator

A = SelectedContract("QQQ260727C00500000", D("1.00"), D("1.02"), D("0.42"), "alpaca")
B = SelectedContract("QQQ260727C00501000", D("0.90"), D("0.93"), D("0.41"), "alpaca")


class FakeTemplateStore:
    @staticmethod
    def active_id():
        return 7


class FakeSource:
    name = "watcher"

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def select_contract_shortlist(self, ticker, direction, price):
        self.calls.append((ticker, direction, price))
        index = min(len(self.calls) - 1, len(self.responses) - 1)
        return self.responses[index]


def _frame(*, price="500", blocked=False, rule="", stale=False):
    return {
        "source": "watcher",
        "stale": stale,
        "signal_online": not stale,
        "pool": [{"sym": "QQQ", "tradable": True}],
        "ticks": [{
            "sym": "QQQ",
            "price": price,
            "gates": {
                "long": {"blocked": blocked, "rule": rule, "reason": rule},
                "short": {"blocked": True, "rule": "chase", "reason": "chase"},
            },
        }],
    }


def _setup(monkeypatch, responses):
    source = FakeSource(responses)
    monkeypatch.setattr(
        option_candidates,
        "current_source",
        lambda: source,
    )
    monkeypatch.setattr(
        option_candidates,
        "select_contract_shortlist",
        lambda src, ticker, direction, price, **_kwargs: (
            src.select_contract_shortlist(ticker, direction, price)
        ),
    )
    monkeypatch.setattr(
        option_candidates,
        "get_store",
        lambda: FakeTemplateStore(),
    )
    stream = OptionQuoteStream(url="wss://test.invalid", symbol_budget=120)
    return OptionCandidateCoordinator(stream=stream), source, stream


def test_blocked_ticker_never_requests_chain(monkeypatch):
    async def run():
        coordinator, source, stream = _setup(monkeypatch, [[A]])
        await coordinator.reconcile_snapshot(
            _frame(blocked=True, rule="breaker"),
            now=10.0,
        )
        await coordinator.wait_idle()
        return source.calls, stream.desired_symbols

    calls, desired = asyncio.run(run())
    assert calls == []
    assert desired == set()


def test_allowed_ticker_waits_one_second_then_subscribes_only_top1(monkeypatch):
    async def run():
        coordinator, source, stream = _setup(monkeypatch, [[A, B]])
        frame = _frame()
        await coordinator.reconcile_snapshot(frame, now=10.0)
        await coordinator.reconcile_snapshot(frame, now=10.9)
        assert source.calls == []
        await coordinator.reconcile_snapshot(frame, now=11.1)
        await coordinator.wait_idle()
        return coordinator.snapshot(), source.calls, stream.desired_symbols

    snapshot, calls, desired = asyncio.run(run())
    assert calls == [("QQQ", "long", D("500"))]
    assert desired == {A.option_code}
    assert snapshot["previews"]["QQQ"]["sides"]["long"]["selected"] == A.option_code
    assert len(snapshot["previews"]["QQQ"]["sides"]["long"]["shortlist"]) == 2


def test_hard_gate_releases_candidate_but_not_other_scopes(monkeypatch):
    async def run():
        coordinator, _source, stream = _setup(monkeypatch, [[A]])
        stream.replace_scope("positions", {A.option_code}, priority=1000)
        frame = _frame()
        await coordinator.reconcile_snapshot(frame, now=1.0)
        await coordinator.reconcile_snapshot(frame, now=2.1)
        await coordinator.wait_idle()
        await coordinator.reconcile_snapshot(
            _frame(blocked=True, rule="breaker"),
            now=2.2,
        )
        return stream.scope_symbols("candidates:QQQ:long"), stream.desired_symbols

    candidate_scope, desired = asyncio.run(run())
    assert candidate_scope == set()
    assert desired == {A.option_code}


def test_contract_switch_waits_for_new_subscription_ack(monkeypatch):
    async def run():
        coordinator, _source, stream = _setup(monkeypatch, [[A], [B]])
        first = _frame(price="500")
        await coordinator.reconcile_snapshot(first, now=1.0)
        await coordinator.reconcile_snapshot(first, now=2.1)
        await coordinator.wait_idle()

        moved = _frame(price="503.1")  # >0.5%，触发重新选约
        await coordinator.reconcile_snapshot(moved, now=2.2)
        await coordinator.wait_idle()
        during = stream.scope_symbols("candidates:QQQ:long")

        stream.handle_messages([{
            "T": "subscription",
            "quotes": [A.option_code, B.option_code],
        }])
        await coordinator.reconcile_snapshot(moved, now=2.3)
        after = stream.scope_symbols("candidates:QQQ:long")
        selected = coordinator.snapshot()["previews"]["QQQ"]["sides"]["long"]["selected"]
        return during, after, selected

    during, after, selected = asyncio.run(run())
    assert during == {A.option_code, B.option_code}
    assert after == {B.option_code}
    assert selected == B.option_code


def test_held_ticker_releases_candidate_scope_but_keeps_position_scope(monkeypatch):
    async def run():
        coordinator, source, stream = _setup(monkeypatch, [[A]])
        frame = _frame()
        await coordinator.reconcile_snapshot(frame, now=1.0)
        await coordinator.reconcile_snapshot(frame, now=2.1)
        await coordinator.wait_idle()
        assert source.calls

        held_code = B.option_code
        stream.replace_scope("positions", {held_code}, priority=1000)
        monkeypatch.setattr(
            "app.services.option_candidates.REGISTRY",
            SimpleNamespace(active=lambda: [
                SimpleNamespace(ticker="QQQ", status="open", option_code=held_code),
            ]),
        )
        await coordinator.reconcile_snapshot(frame, now=2.2)
        return coordinator.snapshot(), stream.scope_symbols("candidates:QQQ:long"), stream.desired_symbols

    snapshot, candidate_scope, desired = asyncio.run(run())
    assert candidate_scope == set()
    assert desired == {B.option_code}
    assert snapshot["previews"]["QQQ"]["sides"]["long"]["status"] == "HELD"
    assert snapshot["previews"]["QQQ"]["sides"]["long"]["gate"]["reason"] == "已持仓"


def test_closed_ticker_leaves_held_state_immediately(monkeypatch):
    """持仓清零后的下一拍必须撤掉 HELD，不能在重新选约稳定窗里继续显示“已持仓”。"""
    async def run():
        coordinator, _, _ = _setup(monkeypatch, [[A]])
        frame = _frame()
        active = [
            SimpleNamespace(ticker="QQQ", status="open", option_code=A.option_code),
        ]
        monkeypatch.setattr(
            "app.services.option_candidates.REGISTRY",
            SimpleNamespace(active=lambda: list(active)),
        )

        await coordinator.reconcile_snapshot(frame, now=1.0)
        assert coordinator.snapshot()["previews"]["QQQ"]["sides"]["long"]["status"] == "HELD"

        active.clear()
        await coordinator.reconcile_snapshot(frame, now=1.1)
        return coordinator.snapshot()["previews"]["QQQ"]["sides"]["long"]

    preview = asyncio.run(run())
    assert preview["status"] != "HELD"
    assert preview["gate"]["reason"] != "已持仓"
