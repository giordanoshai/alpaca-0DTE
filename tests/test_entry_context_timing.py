"""下单点击路径的分步耗时埋点。

背景（2026-08-11 日志诊断，见 docs/reports/2026-08-11-下单卡顿与拦截诊断.md）：
`trade_intent_accepted` → `trade_intent_resolved` 在 `53f7d78` 之后由改动前的
max 2.4s 劣化到 max 14.7s，而券商侧 `submitted` → `filled` 只有 55–500ms ——
延迟全部发生在提交之前。但 `build_market_context` 的各子步骤一个埋点都没有，
14.7 秒那次窗口内日志完全静默，无从判断是哪一步。

本测试守的是埋点的**归属正确性**：哪一步慢，耗时就必须记在哪个字段上。
字段标错的埋点比没有埋点更坏 —— 会把排查引到错误的模块。
"""
from __future__ import annotations

import json
import time
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from app.services.market_context import build_market_context
from app.validator import AccountState, TickerSignal

_SLOW_S = 0.06
_SLOW_MS_FLOOR = 50      # 给调度抖动留余量
_FAST_MS_CEILING = 40


@pytest.fixture()
def captured(monkeypatch) -> list[dict]:
    """替换模块 logger，收集 info() 吐出的 JSON 事件。"""
    events: list[dict] = []

    def _info(payload):
        events.append(json.loads(payload))

    monkeypatch.setattr(
        "app.services.market_context.logger",
        SimpleNamespace(info=_info, warning=lambda *_a, **_k: None),
    )
    return events


def _wire(monkeypatch, *, slow_step: str):
    """装好各子步骤，其中 slow_step 那一步真的睡 _SLOW_S。"""
    signal = TickerSignal(price=D("100"),
        vwap=D("99"), atr=D("1"), vwap_sigma=D("0.5"),
    )
    account = AccountState(
        daily_pnl=D("0"), weekly_pnl=D("0"), consecutive_losses=0,
        cooldown_remaining_s=0, minutes_since_open=D("60"),
        buying_power=D("10000"),
    )

    def _maybe_sleep(step: str):
        if step == slow_step:
            time.sleep(_SLOW_S)

    def _signal(*_a, **_k):
        _maybe_sleep("signal")
        return signal

    def _contract(*_a, **_k):
        _maybe_sleep("contract")
        return None

    def _account(*_a, **_k):
        _maybe_sleep("account")
        return account

    monkeypatch.setattr("app.services.market_context.market_signal", _signal)
    monkeypatch.setattr("app.services.market_context.select_contract_full", _contract)
    monkeypatch.setattr("app.services.market_context.build_account_state", _account)

    src = SimpleNamespace(
        signal_online=lambda: True,
        clock=lambda: SimpleNamespace(minutes_since_open=D("60")),
    )
    return src


def _timing(events: list[dict]) -> dict:
    hits = [e for e in events if e.get("event") == "entry_context_timing"]
    assert len(hits) == 1, f"应恰好吐一条 entry_context_timing，实得 {len(hits)}"
    return hits[0]


@pytest.mark.parametrize(
    ("slow_step", "slow_field"),
    [
        ("signal", "signal_ms"),
        ("contract", "contract_ms"),
        ("account", "account_ms"),
    ],
)
def test_耗时归属到正确的子步骤(monkeypatch, captured, slow_step, slow_field):
    src = _wire(monkeypatch, slow_step=slow_step)

    build_market_context(src, "APLD", "long")

    timing = _timing(captured)
    assert timing[slow_field] >= _SLOW_MS_FLOOR, f"{slow_field} 未记到慢步骤的耗时"
    for field in ("signal_ms", "contract_ms", "account_ms"):
        if field != slow_field:
            assert timing[field] < _FAST_MS_CEILING, f"{field} 不该被计入 {slow_step} 的耗时"


def test_埋点带标的与方向且总耗时覆盖各步(monkeypatch, captured):
    src = _wire(monkeypatch, slow_step="contract")

    build_market_context(src, "APLD", "long")

    timing = _timing(captured)
    assert timing["ticker"] == "APLD"
    assert timing["direction"] == "long"
    parts = sum(timing[f] for f in ("signal_ms", "contract_ms", "account_ms"))
    assert timing["total_ms"] >= parts
