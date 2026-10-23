"""ManagedPosition.close_error 默认 None，且 /api/positions 每仓回显该字段。"""
from datetime import datetime, timezone
from decimal import Decimal as D

import app.exec_reconcile as order_exec
from app.order_state import REGISTRY, ManagedPosition


def _mk(code="QQQ260716C00500000", status="open"):
    return ManagedPosition(option_code=code, ticker="QQQ", direction="long", qty=1,
                           entry_mid=D("1.00"), entry_time=datetime.now(timezone.utc),
                           order_id="e1", status=status)


def test_managed_position_close_error_defaults_none():
    assert _mk().close_error is None


def test_list_positions_exposes_close_error(monkeypatch):
    from types import SimpleNamespace
    REGISTRY._pos.clear()
    p = _mk()
    p.close_error = "穿价追单未成交"
    p.risk_exit_state = "canceling_existing"
    p.risk_exit_reason = "$止损 亏$100 ≥ $80"
    p.cancel_requested = True
    p.realized_pnl = D("42.50")
    REGISTRY.record(p)
    src = SimpleNamespace(name="real", alpaca=None)  # 非可执行档→跳过 unmanaged 扫描
    out = order_exec.list_positions(src)
    row = next(r for r in out["positions"] if r["option_code"] == p.option_code)
    assert row["order_id"] == p.order_id
    assert row["close_error"] == "穿价追单未成交"
    assert row["realized_pnl"] == "42.50"
    assert row["stop_mode"] == "percent"
    assert row["risk_exit_state"] == "canceling_existing"
    assert row["risk_exit_reason"] == "$止损 亏$100 ≥ $80"
    assert row["cancel_requested"] is True
    REGISTRY._pos.clear()
