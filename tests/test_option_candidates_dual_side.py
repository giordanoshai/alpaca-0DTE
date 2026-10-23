"""候选协调器双边预取：每票 CALL/PUT 各维护一份候选（趋势分降级第二期）。

替代已删除的 `tests/test_option_candidates_primary_direction.py` —— 那个文件锁的是
「选约方向必须取 primary_direction 而非冲量 direction」，而 2026-08-08 起选约方向
根本不来自趋势分了（两边都选），该需求整体作废。

核心收益由 `test_one_side_blocked_other_side_still_selects` 守着：追高只拦一边时，
另一边必须照常有合约可买。旧实现是每票一个方向，那一边被拦就整票没合约。
"""
from __future__ import annotations

import asyncio
import inspect
from decimal import Decimal as D
from types import SimpleNamespace

from app.integrations.alpaca.data import SelectedContract
from app.option_stream import OptionQuoteStream
import app.services.option_candidates as option_candidates
from app.services.option_candidates import OptionCandidateCoordinator

C1 = SelectedContract("QQQ260727C00500000", D("1.00"), D("1.02"), D("0.42"), "alpaca")
C2 = SelectedContract("QQQ260727C00501000", D("0.90"), D("0.93"), D("0.41"), "alpaca")
P1 = SelectedContract("QQQ260727P00500000", D("1.10"), D("1.13"), D("-0.44"), "alpaca")


class FakeTemplateStore:
    @staticmethod
    def active_id():
        return 7


class FakeSource:
    name = "watcher"

    def __init__(self, by_direction):
        self.by_direction = dict(by_direction)
        self.calls = []

    def select_contract_shortlist(self, ticker, direction, price):
        self.calls.append((ticker, direction, price))
        return self.by_direction.get(direction, [])


def _setup(monkeypatch, by_direction):
    source = FakeSource(by_direction)
    monkeypatch.setattr(option_candidates, "current_source", lambda: source)
    monkeypatch.setattr(
        option_candidates,
        "select_contract_shortlist",
        lambda src, ticker, direction, price, **_kw: (
            src.select_contract_shortlist(ticker, direction, price)
        ),
    )
    monkeypatch.setattr(option_candidates, "get_store", lambda: FakeTemplateStore())
    stream = OptionQuoteStream(url="wss://test.invalid", symbol_budget=120)
    return OptionCandidateCoordinator(stream=stream), source, stream


def _frame(*, price="500", long_blocked=False, short_blocked=False, rule="chase"):
    def gate(blocked):
        return {
            "blocked": blocked,
            "rule": rule if blocked else "",
            "reason": rule if blocked else "",
        }

    return {
        "source": "watcher",
        "stale": False,
        "signal_online": True,
        "pool": [{"sym": "QQQ", "tradable": True}],
        "ticks": [{
            "sym": "QQQ",
            "price": price,
            "gates": {"long": gate(long_blocked), "short": gate(short_blocked)},
        }],
    }


def test_both_sides_selected_and_subscribed(monkeypatch):
    async def run():
        coordinator, source, stream = _setup(monkeypatch, {"long": [C1], "short": [P1]})
        frame = _frame()
        await coordinator.reconcile_snapshot(frame, now=10.0)
        await coordinator.reconcile_snapshot(frame, now=11.1)
        await coordinator.wait_idle()
        return coordinator.snapshot(), source.calls, stream.desired_symbols

    snapshot, calls, desired = asyncio.run(run())

    assert sorted(d for _t, d, _p in calls) == ["long", "short"]
    assert desired == {C1.option_code, P1.option_code}
    sides = snapshot["previews"]["QQQ"]["sides"]
    assert sides["long"]["selected"] == C1.option_code
    assert sides["short"]["selected"] == P1.option_code


def test_one_side_blocked_other_side_still_selects(monkeypatch):
    """追高只拦 CALL 时，PUT 侧必须照常选约——这是第二期的核心收益。"""
    async def run():
        coordinator, source, stream = _setup(monkeypatch, {"long": [C1], "short": [P1]})
        frame = _frame(long_blocked=True)
        await coordinator.reconcile_snapshot(frame, now=10.0)
        await coordinator.reconcile_snapshot(frame, now=11.1)
        await coordinator.wait_idle()
        return coordinator.snapshot(), source.calls, stream.desired_symbols

    snapshot, calls, desired = asyncio.run(run())

    assert [d for _t, d, _p in calls] == ["short"]
    assert desired == {P1.option_code}
    sides = snapshot["previews"]["QQQ"]["sides"]
    assert sides["long"]["gate_allowed"] is False
    assert sides["long"]["gate"]["rule"] == "chase"
    assert sides["short"]["selected"] == P1.option_code


def test_both_sides_blocked_requests_nothing(monkeypatch):
    async def run():
        coordinator, source, stream = _setup(monkeypatch, {"long": [C1], "short": [P1]})
        frame = _frame(long_blocked=True, short_blocked=True, rule="breaker")
        await coordinator.reconcile_snapshot(frame, now=10.0)
        await coordinator.reconcile_snapshot(frame, now=11.1)
        await coordinator.wait_idle()
        return source.calls, stream.desired_symbols

    calls, desired = asyncio.run(run())

    assert calls == []
    assert desired == set()


def test_each_side_owns_its_own_ws_scope(monkeypatch):
    """两侧的 scope owner 必须分开，否则一侧换档会连带释放另一侧的订阅。"""
    async def run():
        coordinator, _source, stream = _setup(monkeypatch, {"long": [C1], "short": [P1]})
        frame = _frame()
        await coordinator.reconcile_snapshot(frame, now=1.0)
        await coordinator.reconcile_snapshot(frame, now=2.1)
        await coordinator.wait_idle()
        return (
            stream.scope_symbols("candidates:QQQ:long"),
            stream.scope_symbols("candidates:QQQ:short"),
        )

    long_scope, short_scope = asyncio.run(run())

    assert long_scope == {C1.option_code}
    assert short_scope == {P1.option_code}


def test_contract_switch_only_affects_its_own_side(monkeypatch):
    """CALL 侧换档期间 PUT 侧订阅不得受影响。"""
    async def run():
        coordinator, source, stream = _setup(monkeypatch, {"long": [C1], "short": [P1]})
        first = _frame(price="500")
        await coordinator.reconcile_snapshot(first, now=1.0)
        await coordinator.reconcile_snapshot(first, now=2.1)
        await coordinator.wait_idle()

        source.by_direction["long"] = [C2]
        moved = _frame(price="503.1")           # >0.5%，触发重新选约
        await coordinator.reconcile_snapshot(moved, now=2.2)
        await coordinator.wait_idle()
        during_long = stream.scope_symbols("candidates:QQQ:long")
        during_short = stream.scope_symbols("candidates:QQQ:short")

        stream.handle_messages([{
            "T": "subscription",
            "quotes": [C1.option_code, C2.option_code, P1.option_code],
        }])
        await coordinator.reconcile_snapshot(moved, now=2.3)
        sides = coordinator.snapshot()["previews"]["QQQ"]["sides"]
        return during_long, during_short, sides["long"]["selected"], sides["short"]["selected"]

    during_long, during_short, long_sel, short_sel = asyncio.run(run())

    assert during_long == {C1.option_code, C2.option_code}   # make-before-break
    assert during_short == {P1.option_code}
    assert long_sel == C2.option_code
    assert short_sel == P1.option_code


def test_held_ticker_releases_both_sides(monkeypatch):
    async def run():
        coordinator, _source, stream = _setup(monkeypatch, {"long": [C1], "short": [P1]})
        frame = _frame()
        await coordinator.reconcile_snapshot(frame, now=1.0)
        await coordinator.reconcile_snapshot(frame, now=2.1)
        await coordinator.wait_idle()

        monkeypatch.setattr(
            "app.services.option_candidates.REGISTRY",
            SimpleNamespace(active=lambda: [
                SimpleNamespace(ticker="QQQ", status="open", option_code=C1.option_code),
            ]),
        )
        await coordinator.reconcile_snapshot(frame, now=2.2)
        return (
            coordinator.snapshot(),
            stream.scope_symbols("candidates:QQQ:long"),
            stream.scope_symbols("candidates:QQQ:short"),
        )

    snapshot, long_scope, short_scope = asyncio.run(run())

    assert long_scope == set() and short_scope == set()
    preview = snapshot["previews"]["QQQ"]
    assert preview["held"] is True
    assert preview["sides"]["long"]["status"] == "HELD"
    assert preview["sides"]["short"]["status"] == "HELD"


def test_block_reason_has_no_chop_branch():
    """趋势分方向已退出选约，不该再有「趋势分说不出方向所以没合约」这种状态。

    行为断言：一份完全不含任何趋势方向字段的 pool 条目，只要闸门放行、
    数据质量 OK、有现价，就必须可以选约。
    """
    allowed = option_candidates._block_reason(
        {"tradable": True},                              # 无 direction / primary_direction
        D("500"),
        {"blocked": False, "rule": "", "reason": ""},
    )

    assert allowed is None
    # 兼带守住实现：函数体（去掉说明删除历史的 docstring）里不得再出现 chop 分支。
    body = inspect.getsource(option_candidates._block_reason).split('"""')[-1]
    assert "chop" not in body


def test_worker_concurrency_raised_for_dual_side():
    """双边后候选槽位翻倍（33 票 66 槽）；并发仍是 2 会让扫描周期 14s→28s，
    超过 _IDENTITY_TTL_S=15s，一半时间在展示过期报价。"""
    coordinator = OptionCandidateCoordinator(stream=OptionQuoteStream(url="wss://x.invalid"))

    assert coordinator._semaphore._value >= 4
