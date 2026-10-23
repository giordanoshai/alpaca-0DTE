"""⑥ 期权报价 WS 流测试 —— 缓存新鲜度 / 帧处理 / 订阅差异（纯逻辑，不连网）。"""
from __future__ import annotations

import asyncio
import time
from decimal import Decimal as D

import msgpack
import pytest

from app.option_stream import (
    PRIORITY_CANDIDATE,
    PRIORITY_POSITION,
    PRIORITY_PREVIEW,
    PRIORITY_RECORDING,
    OptionQuoteStream,
    OptionSubscriptionBudgetError,
)

CODE = "QQQ260717C00697000"
CODE_2 = "QQQ260717C00698000"
CODE_3 = "SPY260717P00600000"


def _stream() -> OptionQuoteStream:
    return OptionQuoteStream(url="wss://test.invalid/v1beta1/indicative", staleness_s=5.0)


def _ready_stream_quote(s: OptionQuoteStream, code: str = CODE) -> None:
    s.replace_scope("test", {code}, PRIORITY_CANDIDATE)
    s._begin_connection()
    s.handle_messages([{"T": "subscription", "quotes": [code]}])


# ---------- handle_messages → quote ----------

def test_quote_frame_updates_cache():
    s = _stream()
    _ready_stream_quote(s)
    s.handle_messages([{"T": "q", "S": CODE, "bp": 1.05, "ap": 1.10}])
    assert s.quote(CODE) == (D("1.05"), D("1.10"))


def test_position_quote_wakes_watchdog_immediately():
    async def scenario():
        s = _stream()
        s._bind_loop()
        s.set_symbols({CODE})
        s._begin_connection()
        s.handle_messages([{"T": "subscription", "quotes": [CODE]}])
        waiter = asyncio.create_task(s.wait_activity(1.0))
        await asyncio.sleep(0)

        s.handle_messages([{"T": "q", "S": CODE, "bp": 1.05, "ap": 1.10}])

        assert await waiter is True

    asyncio.run(scenario())


def test_candidate_quote_does_not_wake_position_watchdog():
    async def scenario():
        s = _stream()
        s._bind_loop()
        s.replace_scope("candidates:QQQ", {CODE}, PRIORITY_CANDIDATE)
        s._begin_connection()
        s.handle_messages([{"T": "subscription", "quotes": [CODE]}])

        s.handle_messages([{"T": "q", "S": CODE, "bp": 1.05, "ap": 1.10}])

        assert await s.wait_activity(0.01) is False

    asyncio.run(scenario())


def test_quote_unknown_code_none():
    assert _stream().quote(CODE) is None


def test_quote_stale_none():
    s = _stream()
    _ready_stream_quote(s)
    s.handle_messages([{"T": "q", "S": CODE, "bp": 1.05, "ap": 1.10}])
    bid, ask, _ = s._cache[CODE]
    s._cache[CODE] = (bid, ask, time.monotonic() - 6.0)  # 人为放旧 6s > 5s
    assert s.quote(CODE) is None


def test_non_quote_frames_ignored():
    s = _stream()
    s.handle_messages([
        {"T": "success", "msg": "authenticated"},
        {"T": "subscription", "quotes": [CODE]},
        {"T": "q", "S": CODE, "bp": None, "ap": 1.10},  # 缺 bid → 丢弃
        {"T": "q", "bp": 1.0, "ap": 1.1},               # 缺 S → 丢弃
    ])
    assert s.quote(CODE) is None
    assert s.confirmed_symbols == {CODE}


def test_sos_ws_reader_requires_current_confirmed_ws_quote():
    s = _stream()
    s.replace_scope("sos-entry:test", {CODE}, PRIORITY_PREVIEW)
    s._begin_connection()
    s.handle_messages([{"T": "subscription", "quotes": [CODE]}])

    assert s.quote_with_age(
        CODE, max_age_s=1.0, require_confirmed=True, require_origin="ws"
    ) is None

    s.handle_messages([{"T": "q", "S": CODE, "bp": 1.05, "ap": 1.10}])
    quote = s.quote_with_age(
        CODE, max_age_s=1.0, require_confirmed=True, require_origin="ws"
    )
    assert quote is not None
    assert quote[:2] == (D("1.05"), D("1.10"))

    s._end_connection()
    assert s.quote_with_age(
        CODE, max_age_s=1.0, require_confirmed=True, require_origin="ws"
    ) is None


def test_sos_ws_reader_does_not_reuse_quote_after_membership_change():
    s = _stream()
    _ready_stream_quote(s)
    s.handle_messages([{"T": "q", "S": CODE, "bp": 1.05, "ap": 1.10}])

    s.handle_messages([{"T": "subscription", "quotes": []}])
    s.handle_messages([{"T": "subscription", "quotes": [CODE]}])

    assert s.quote_with_age(
        CODE, max_age_s=1.0, require_confirmed=True, require_origin="ws"
    ) is None


# ---------- sub_frames：订阅集差异 → 动作帧 ----------

def test_sub_frames_add_only():
    assert OptionQuoteStream.sub_frames({"A", "B"}, set()) == [
        {"action": "subscribe", "quotes": ["A", "B"]}]


def test_sub_frames_remove_only():
    assert OptionQuoteStream.sub_frames(set(), {"A"}) == [
        {"action": "unsubscribe", "quotes": ["A"]}]


def test_sub_frames_mixed():
    assert OptionQuoteStream.sub_frames({"B"}, {"A"}) == [
        {"action": "subscribe", "quotes": ["B"]},
        {"action": "unsubscribe", "quotes": ["A"]}]


def test_sub_frames_no_change():
    assert OptionQuoteStream.sub_frames({"A"}, {"A"}) == []


# ---------- set_symbols ----------

def test_set_symbols_copies():
    s = _stream()
    codes = {CODE}
    s.set_symbols(codes)
    codes.clear()  # 外部改动不得影响内部期望集
    assert s._want == {CODE}
    assert s.scope_symbols("positions") == {CODE}


# ---------- scope / budget / confirmed ----------

def test_scopes_are_deduplicated_and_released_independently():
    s = _stream()
    s.replace_scope("positions", {CODE}, PRIORITY_POSITION)
    s.replace_scope("candidates:QQQ", {CODE, CODE_2}, PRIORITY_CANDIDATE)

    assert s.desired_symbols == {CODE, CODE_2}
    s.release_scope("candidates:QQQ")
    assert s.desired_symbols == {CODE}


def test_budget_keeps_positions_and_drops_lower_priority_candidates():
    s = OptionQuoteStream(
        url="wss://test.invalid/v1beta1/indicative",
        staleness_s=5.0,
        symbol_budget=2,
    )
    s.replace_scope("positions", {CODE}, PRIORITY_POSITION)
    s.replace_scope("candidates:QQQ", {CODE_2, CODE_3}, PRIORITY_CANDIDATE)

    assert CODE in s.desired_symbols
    assert len(s.desired_symbols) == 2
    assert s.dropped_symbols == {CODE_2, CODE_3} - s.desired_symbols


def test_strict_recording_scope_rejects_instead_of_partial_subscription():
    s = OptionQuoteStream(
        url="wss://test.invalid/v1beta1/indicative",
        staleness_s=5.0,
        symbol_budget=1,
    )
    s.replace_scope("positions", {CODE}, PRIORITY_POSITION)

    with pytest.raises(OptionSubscriptionBudgetError):
        s.replace_scope("recording", {CODE_2}, PRIORITY_RECORDING, strict=True)

    assert s.scope_symbols("recording") == set()
    assert s.desired_symbols == {CODE}


def test_subscription_ack_replaces_confirmed_set():
    s = _stream()
    assert s.confirmed_symbols == set()

    s.handle_messages([{"T": "subscription", "quotes": [CODE, CODE_2]}])
    assert s.confirmed_symbols == {CODE, CODE_2}

    s.handle_messages([{"T": "subscription", "quotes": [CODE_2]}])
    assert s.confirmed_symbols == {CODE_2}
    assert s.subscription_snapshot()["metrics"]["subscription_acks"] == 2


def test_server_405_keeps_confirmed_set_and_is_observable():
    s = _stream()
    s.handle_messages([{"T": "subscription", "quotes": [CODE]}])
    s.handle_messages([{"T": "error", "code": 405, "msg": "symbol limit exceeded"}])

    assert s.confirmed_symbols == {CODE}
    assert s.subscription_snapshot()["metrics"]["server_errors"]["405"] == 1


def test_33_primary_candidates_plus_positions_remain_far_below_budget():
    s = OptionQuoteStream(url="wss://test.invalid", symbol_budget=120)
    for index in range(33):
        symbol = f"T{index:02d}260727C00500000"
        s.replace_scope(f"candidates:T{index:02d}", {symbol}, PRIORITY_CANDIDATE)
    positions = {f"POS{index:02d}260727P00500000" for index in range(20)}
    s.replace_scope("positions", positions, PRIORITY_POSITION)

    assert len(s.desired_symbols) == 53
    assert s.dropped_symbols == set()


def test_reconciler_is_woken_immediately_by_scope_change():
    class FakeWs:
        def __init__(self):
            self.frames = []

        async def send(self, payload):
            self.frames.append(msgpack.unpackb(payload))

    async def run():
        s = _stream()
        ws = FakeWs()
        s._bind_loop()
        task = asyncio.create_task(s._reconciler(ws))
        await asyncio.sleep(0)
        s.replace_scope("candidates:QQQ", {CODE}, PRIORITY_CANDIDATE)
        await asyncio.wait_for(_wait_for_frames(ws), timeout=0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return ws.frames

    async def _wait_for_frames(ws):
        while not ws.frames:
            await asyncio.sleep(0)

    assert asyncio.run(run()) == [{"action": "subscribe", "quotes": [CODE]}]
