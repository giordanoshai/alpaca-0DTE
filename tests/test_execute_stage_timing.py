"""execute 事件必须带出提交前各段网络调用的耗时。

背景（2026-08-11 诊断，docs/reports/2026-08-11-下单卡顿与拦截诊断.md）：
点击到券商提交最长 14.7s，而券商 submitted→filled 仅 55–500ms。
`build_market_context` 内部已由 `entry_context_timing` 拆开，但 `execute()`
在闸门之后、`trade_order_submitted` 之前还有三段独立的网络调用没有埋点：

    _quote_at_submit  → option_quote(fresh=True)     提交价的来源
    position_qty      → 合约持仓归属核对（防与 SOS 撞仓）
    submit            → 真正的券商下单

缺了这三段，就算知道闸门耗时也补不齐「受理→提交」这条时间轴。

与 test_entry_context_timing 同理，本测试守的是**归属正确性**：
哪一段慢，耗时必须记在哪个字段上，标错的埋点会把排查引向错误的模块。
"""
from __future__ import annotations

import json
import time
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app.exec_entry as exec_entry
import app.main as m
import app.routers.orders as orders
from app.integrations.alpaca.data import SelectedContract
from app.order_gate import MarketContext
from app.services.contract_selection import contract_for_validation
from app.validator import ValidationResult

client = TestClient(m.app)

_CODE = "QQQ260716C00500000"
_SLOW_S = 0.06
_SLOW_MS_FLOOR = 50
_FAST_MS_CEILING = 40

_STAGES = {
    "gate": "gate_ms",
    "quote": "quote_ms",
    "owner_check": "owner_check_ms",
    "submit": "submit_ms",
}


@pytest.fixture()
def captured(monkeypatch) -> list[dict]:
    events: list[dict] = []

    def _info(payload):
        try:
            events.append(json.loads(payload))
        except (TypeError, ValueError):
            pass

    monkeypatch.setattr(
        exec_entry, "logger",
        SimpleNamespace(info=_info, warning=lambda *_a, **_k: None),
    )
    return events


def _wire(monkeypatch, *, slow_stage: str):
    def _maybe_sleep(stage: str):
        if stage == slow_stage:
            time.sleep(_SLOW_S)

    class _Exec:
        submitted: list = []

        def position_qty(self, _symbol):
            _maybe_sleep("owner_check")
            return 0

        def submit(self, req):
            _maybe_sleep("submit")
            _Exec.submitted.append(req)
            return {"id": "fake-order-stage-timing", "status": "accepted"}

    _Exec.submitted = []

    sel = SelectedContract(_CODE, Decimal("1.00"), Decimal("1.02"),
                           delta=Decimal("0.47"), delta_source="uw")
    ctx = MarketContext(signal=None, account=None,
                        contract=contract_for_validation(sel), selected=sel)

    def _quote(_code, *, fresh=False):
        _maybe_sleep("quote")
        return Decimal("1.00"), Decimal("1.02")

    def _gate(*_a, **_k):
        _maybe_sleep("gate")
        return ValidationResult(allowed=True, reason="", rule="", qty=1), ctx

    src = SimpleNamespace(
        name="watcher",
        alpaca=SimpleNamespace(option_quote=_quote),
        market_context=lambda *_: ctx,
    )
    monkeypatch.setattr(orders, "current_source", lambda: src)
    monkeypatch.setattr(exec_entry, "evaluate_with_context", _gate)
    monkeypatch.setattr(exec_entry, "AlpacaTrading", _Exec)


def _execute_event(events: list[dict]) -> dict:
    hits = [e for e in events if e.get("event") == "execute"]
    assert len(hits) == 1, f"应恰好吐一条 execute 事件，实得 {len(hits)}"
    return hits[0]


@pytest.mark.parametrize(("slow_stage", "slow_field"), sorted(_STAGES.items()))
def test_耗时归属到正确的提交前阶段(monkeypatch, captured, slow_stage, slow_field):
    _wire(monkeypatch, slow_stage=slow_stage)

    r = client.post("/api/execute",
                    json={"ticker": "QQQ", "direction": "long", "qty": 1})
    assert r.status_code == 200, r.text

    timing = _execute_event(captured)
    assert timing[slow_field] >= _SLOW_MS_FLOOR, f"{slow_field} 未记到慢阶段的耗时"
    for field in _STAGES.values():
        if field != slow_field:
            assert timing[field] < _FAST_MS_CEILING, \
                f"{field} 不该被计入 {slow_stage} 的耗时"
