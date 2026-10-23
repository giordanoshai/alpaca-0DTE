"""协调器判否时必须指名是哪一条判据不满足。

2026-08-03 BMNR 现场：卡片写「当前不可入场」、没有合约，用户照样点了买入并
500ms 成交。「当前不可入场」是 `gate.get("reason")` 为空时的兜底文案，
意味着**下单闸门根本没拦**，是协调器自己的判据没过 —— 但没告诉用户是哪一条。
四个判据折叠成一个 bool，这类矛盾就无从排查。

本文件原名 `test_option_candidates_primary_direction.py`，锁的是「选约方向必须取
primary_direction 而非冲量 direction」。2026-08-08 趋势分降级第二期后选约方向
不再来自趋势分（CALL/PUT 两边都预取），那批断言整体作废，双边行为由
`test_option_candidates_dual_side.py` 接管；只有「每条判据自报家门」这部分
与方向无关，保留在此。
"""
from __future__ import annotations

import asyncio
from decimal import Decimal as D

import pytest

from tests.test_option_candidates_dual_side import C1, _frame, _setup


def _drive(monkeypatch, frame):
    """推过 1 秒稳定闸门后返回 long 侧快照与选约调用。"""
    async def run():
        coordinator, source, _stream = _setup(monkeypatch, {"long": [C1], "short": []})
        await coordinator.reconcile_snapshot(frame, now=10.0)
        await coordinator.reconcile_snapshot(frame, now=11.1)
        await coordinator.wait_idle()
        preview = coordinator.snapshot()["previews"]["QQQ"]
        return preview["sides"]["long"], source.calls

    return asyncio.run(run())


def test_gate_block_reason_still_wins(monkeypatch):
    """下单闸门给了理由时，照原样透出，不被协调器自己的措辞盖掉。"""
    frame = _frame()
    frame["ticks"][0]["gates"]["long"] = {
        "blocked": True, "rule": "chase", "reason": "追高 2.5σ",
    }

    side, calls = _drive(monkeypatch, frame)

    assert [d for _t, d, _p in calls] == ["short"]     # 只有被拦的 long 侧不选约
    assert side["gate"]["reason"] == "追高 2.5σ"
    assert side["gate"]["rule"] == "chase"


@pytest.mark.parametrize("mutate,expected", [
    (lambda f: f["pool"][0].update(tradable=False), "数据质量不足"),
    (lambda f: f["ticks"][0].update(price="0"), "无有效现价"),
])
def test_each_blocking_condition_names_itself(monkeypatch, mutate, expected):
    """协调器自己判否时，必须指名是哪一条不满足。"""
    frame = _frame()
    mutate(frame)

    side, calls = _drive(monkeypatch, frame)

    assert calls == []
    assert side["gate"]["reason"] == expected


def test_missing_gates_reports_gate_not_ready(monkeypatch):
    """闸门取数失败（tick 无 gates 键）不是「不可入场」，是闸门没就绪。"""
    frame = _frame()
    frame["ticks"][0].pop("gates", None)

    side, calls = _drive(monkeypatch, frame)

    assert calls == []
    assert side["gate"]["reason"] == "闸门未就绪"


def test_allowed_side_reports_no_reason(monkeypatch):
    """放行时 gate 理由必须是空串——非空即代表有拦截，前端据此判是否变灰。"""
    side, _calls = _drive(monkeypatch, _frame())

    assert side["gate_allowed"] is True
    assert side["gate"]["reason"] == ""
    assert side["selected"] == C1.option_code
