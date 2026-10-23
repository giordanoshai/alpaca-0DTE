# -*- coding: utf-8 -*-
"""带仓重启必须自动恢复持仓，否则 watchdog 对着空 REGISTRY 巡检 —— 止损/超时/追踪全部失明。

2026-08-14 23:49 重启实测：券商实持 3 张 QQQ + 1 张 RKLB，30 秒一次的 REST 对账
**连报五次** `active_positions: 0`，失明 2 分 35 秒，直到人工 POST /api/reconcile；
当晚 QQQ 的 15 分钟超时因此晚触发 3 分 40 秒。

根因：启动路径调的是 `reconcile_pending_journal()`（只对账在途未决订单），而真正能
`REGISTRY.record()` 恢复持仓的 `reconcile()` **仅**挂在 `POST /api/reconcile` 上。
详见 docs/reports/2026-08-14-未修缺陷清单.md §1。

本次只自动化**调用时机**，不动 `reconcile()` 的任何判据（数据库无记录的外部仓仍拒绝接管、
券商聚合量多出的部分仍按外部仓忽略）。
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app import exec_reconcile


def _pin_source(monkeypatch, name: str):
    monkeypatch.setattr(
        "app.services.data_source_registry.current_source",
        lambda: SimpleNamespace(name=name),
    )


def test_startup_recovery_runs_reconcile_on_executable_source(monkeypatch):
    """watcher 源：必须真的跑一次持仓恢复，并把结果原样带回。"""
    seen = []
    _pin_source(monkeypatch, "watcher")
    monkeypatch.setattr(
        exec_reconcile, "reconcile",
        lambda source: seen.append(source.name) or {
            "recovered": [{"option_code": "QQQ260814P00729000", "qty": 3}],
            "ignored": [],
            "still_broker_positions": 1,
        },
    )

    result = exec_reconcile.recover_positions_on_startup()

    assert seen == ["watcher"], "可执行数据源下必须恰好跑一次持仓恢复"
    assert result["recovered"][0]["option_code"] == "QQQ260814P00729000"


def test_startup_recovery_skips_when_source_cannot_execute(monkeypatch):
    """mock 源不碰券商：跳过而不是抛错，否则 mock 回放根本起不来。"""
    _pin_source(monkeypatch, "mock")
    monkeypatch.setattr(
        exec_reconcile, "reconcile",
        lambda source: pytest.fail("不可执行数据源下不得调用 reconcile"),
    )

    result = exec_reconcile.recover_positions_on_startup()

    assert result["skipped"] is True
    assert result["source"] == "mock"


def test_startup_recovery_never_blocks_startup(monkeypatch):
    """券商不可达时只告警不抛：恢复失败很危险，但起不来更危险（与相邻启动步同口径）。"""
    _pin_source(monkeypatch, "watcher")

    def _boom(source):
        raise RuntimeError("broker unreachable")

    monkeypatch.setattr(exec_reconcile, "reconcile", _boom)

    result = exec_reconcile.recover_positions_on_startup()

    assert "broker unreachable" in result["error"]


def test_lifespan_actually_wires_startup_recovery():
    """守住启动路径真的挂上了这次恢复 —— 否则单测全绿、线上照样失明 2 分 35 秒。"""
    main_py = Path(__file__).resolve().parents[1] / "app" / "main.py"
    text = main_py.read_text(encoding="utf-8")
    assert "recover_positions_on_startup" in text, (
        "app/main.py 的 lifespan 必须在 watchdog 启动前调用启动恢复"
    )
