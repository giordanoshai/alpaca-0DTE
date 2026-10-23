# -*- coding: utf-8 -*-
"""select_contract_full 必须读当前生效模板的 min_delta/max_spread_pct，而不是硬编码常量
（2026-07-23 簇A：此前设置页调这两个阈值对选约完全不生效，只影响 validator 规则9 的拒单判断）。
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from types import SimpleNamespace

from app.datasource.live import LiveDataSource
from app.risk import ClosedTrade
from app.services import account_state, contract_selection

CANDS = [
    (0.5, "QQQ260716C00500000", D("1.00"), D("1.02"), D("0.50")),
    (1.5, "QQQ260716C00499000", D("1.50"), D("1.53"), D("0.61")),
]


def test_select_contract_full_uses_active_template_min_delta(monkeypatch):
    class FakeTemplate:
        min_delta = D("0.60")
        max_spread_pct = D("0.03")
        max_position_size = D("500")
        position_tolerance = D("0.20")

    class FakeStore:
        def active(self):
            return FakeTemplate()

    monkeypatch.setattr(contract_selection, "get_store", lambda: FakeStore())

    src = LiveDataSource()
    monkeypatch.setattr(src.alpaca, "contract_candidates", lambda *a, **k: CANDS)

    sel = contract_selection.select_contract_full(
        src,
        "QQQ",
        "long",
        D("500"),
    )
    # min_delta=0.60 来自模板，不是硬编码常量 0.40 —— 只有 0.61 那档达标
    assert sel.option_code == "QQQ260716C00499000"
    assert sel.delta == D("0.61")


def test_account_risk_uses_active_template_cooldown_seconds(monkeypatch):
    now = datetime(2026, 7, 28, 14, 0, tzinfo=timezone.utc)
    trades = [
        ClosedTrade(now - timedelta(seconds=90), D("-10")),
        ClosedTrade(now - timedelta(seconds=30), D("-20")),
    ]

    class FakeDateTime:
        @staticmethod
        def now(tz=None):
            return now

    class FakeStore:
        def active(self):
            return SimpleNamespace(cooldown_seconds=120)

    monkeypatch.setattr(account_state, "datetime", FakeDateTime)
    monkeypatch.setattr(account_state, "get_store", lambda: FakeStore())
    monkeypatch.setattr(account_state, "closed_trades", lambda: trades)
    monkeypatch.setattr(account_state, "trade_count_today", lambda _now=None: 2)

    src = LiveDataSource()
    monkeypatch.setattr(
        src,
        "program_pnl",
        lambda _now=None: SimpleNamespace(
            daily=SimpleNamespace(program_gross_pnl=D("-30")),
            weekly=SimpleNamespace(program_gross_pnl=D("-30")),
        ),
    )
    monkeypatch.setattr(src, "buying_power", lambda: D("1000"))

    account = account_state.build_account_state(src, D("60"))

    assert account.consecutive_losses == 2
    assert account.cooldown_remaining_s == 90
