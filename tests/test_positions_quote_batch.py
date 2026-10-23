# -*- coding: utf-8 -*-
"""持仓列表取价：Option WS 优先，未命中的合并成一发 REST。

`list_positions` 由 `TradingStateHub._positions_loop` **0.5 秒一拍**驱动（trading_state.py:89）。
原实现对每个持仓单独 `alpaca.option_quote()`，N 个持仓没命中 WS 就是每拍 N 发 REST ——
打的正是那个已经在限流的行情 API（2026-08-11 三次 alpaca_rate_limited 全来自行情侧）。
而这条循环一旦卡住，前端持仓面板就跟着停更（成交后不刷新的直接成因）。

WS 优先的语义保持不变，只把「逐个 REST 兜底」换成「一发批量兜底」。
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal as D
from types import SimpleNamespace

import app.exec_reconcile as exec_reconcile
from app.option_stream import OPTION_STREAM

_A = "QQQ260716C00500000"
_B = "QQQ260716P00490000"
_C = "SPY260716C00600000"


def _pos(code):
    return SimpleNamespace(
        option_code=code, ticker=code[:3], direction="long", order_id=f"o-{code}",
        qty=1, entry_mid=D("1.00"), status="open", realized_pnl=D("0"),
        close_error=None, sync_note=None, close_order_id=None, close_qty=0,
        cancel_requested=False, close_timeout_policy=None,
        risk_exit_state=None, risk_exit_reason=None,
        entry_time=datetime.now(timezone.utc),
        stop_pct=D("0.3"), dollar_line=D("100"), stop_mode="percent", max_hold_s=1800.0,
    )


def _wire(monkeypatch, *, ws_hits: dict, batch_result: dict):
    batch_calls: list[list[str]] = []
    single_calls: list[str] = []

    monkeypatch.setattr(exec_reconcile.REGISTRY, "active",
                        lambda: [_pos(_A), _pos(_B), _pos(_C)])
    monkeypatch.setattr(OPTION_STREAM, "quote", lambda code: ws_hits.get(code))

    def _option_quotes(codes, fresh=False):
        batch_calls.append(list(codes))
        return {c: batch_result[c] for c in codes if c in batch_result}

    def _option_quote(code, fresh=False):
        single_calls.append(code)
        return batch_result.get(code)

    src = SimpleNamespace(alpaca=SimpleNamespace(
        option_quotes=_option_quotes, option_quote=_option_quote))
    return src, batch_calls, single_calls


def test_ws未命中的合约合并成一发批量请求(monkeypatch):
    src, batch_calls, single_calls = _wire(
        monkeypatch,
        ws_hits={_A: (D("1.0"), D("1.05"))},
        batch_result={_B: (D("2.0"), D("2.06")), _C: (D("3.0"), D("3.09"))},
    )

    out = exec_reconcile.list_positions(src, include_unmanaged=False)

    assert len(batch_calls) == 1, f"应只发一发批量，实得 {len(batch_calls)}"
    assert sorted(batch_calls[0]) == sorted([_B, _C]), "WS 已命中的不该进批量"
    assert single_calls == [], "不该再逐个单发"

    by_code = {row["option_code"]: row for row in out["positions"]}
    assert by_code[_A]["quote"]["bid"] == "1.0"      # WS
    assert by_code[_C]["quote"]["ask"] == "3.09"     # 批量 REST


def test_ws全部命中时不发任何REST(monkeypatch):
    src, batch_calls, single_calls = _wire(
        monkeypatch,
        ws_hits={_A: (D("1.0"), D("1.05")), _B: (D("2.0"), D("2.06")),
                 _C: (D("3.0"), D("3.09"))},
        batch_result={},
    )

    exec_reconcile.list_positions(src, include_unmanaged=False)

    assert batch_calls == [] and single_calls == []


def test_批量取价失败不得挡住整个持仓列表(monkeypatch):
    """单个合约报价失败不能挡住列表——批量整体失败同理，退化为 quote=None。"""
    src, _, _ = _wire(monkeypatch, ws_hits={}, batch_result={})

    def _boom(_codes, fresh=False):
        raise RuntimeError("行情 API 挂了")

    src.alpaca.option_quotes = _boom

    out = exec_reconcile.list_positions(src, include_unmanaged=False)

    assert len(out["positions"]) == 3
    assert all(row["quote"] is None for row in out["positions"])


def test_没有alpaca的数据源照常返回持仓(monkeypatch):
    src, _, _ = _wire(monkeypatch, ws_hits={}, batch_result={})
    src.alpaca = None

    out = exec_reconcile.list_positions(src, include_unmanaged=False)

    assert len(out["positions"]) == 3
    assert all(row["quote"] is None for row in out["positions"])
