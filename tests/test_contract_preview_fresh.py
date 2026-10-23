# -*- coding: utf-8 -*-
"""合约预览的合约选择可短缓存，但盘口必须取最新报价。"""
from decimal import Decimal as D
from types import SimpleNamespace

import app.exec_entry as order_exec
from app.integrations.alpaca.data import SelectedContract


def test_contract_preview_overrides_cached_selection_quote_with_fresh_quote(
    monkeypatch,
):
    calls = []
    selected = SelectedContract(
        "QQQ260726C00700000",
        D("1.00"),
        D("2.00"),
        delta=D("0.48"),
        delta_source="alpaca",
    )
    source = SimpleNamespace(
        name="watcher",
        option_quote=lambda code, fresh=False: (
            calls.append((code, fresh)) or (D("1.40"), D("1.60"))
        ),
    )
    monkeypatch.setattr(
        order_exec,
        "entry_plan",
        lambda *_args, **_kwargs: selected,
    )
    order_exec._PREVIEW_CACHE.clear()

    result = order_exec.contract_preview(source, "QQQ", "long")

    assert calls == [("QQQ260726C00700000", True)]
    assert result["bid"] == "1.40"
    assert result["ask"] == "1.60"
    assert result["mid"] == "1.50"
    assert result["spread_pct"] == str(D("0.20") / D("1.50"))
