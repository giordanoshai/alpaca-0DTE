"""⑥ watchdog 止损判定测试。"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal as D
from types import SimpleNamespace

from app.watchdog import watchdog_check

STOP = D("0.20")
DOLLAR = D("80")
MAXHOLD = 900.0  # 15 分钟


def chk(entry, current, elapsed, mode="percent"):
    return watchdog_check(
        entry, current, elapsed,
        stop_pct=STOP, dollar_line=DOLLAR, max_hold_s=MAXHOLD,
        stop_mode=mode,
    )


def test_dollar_stop_triggers():
    close, reason = chk(D("400"), D("320"), 60, "amount")  # 亏 $80
    assert close is True and "$止损" in reason


def test_dollar_stop_boundary_exact():
    close, _ = chk(D("400"), D("320.0"), 60, "amount")  # 恰好 -80
    assert close is True


def test_pct_stop_triggers():
    close, reason = chk(D("300"), D("240"), 60)  # 亏 $60(-20%)，未到 $80 线
    assert close is True and "%止损" in reason


def test_timeout_triggers():
    close, reason = chk(D("400"), D("400"), 900)  # 无亏但超时
    assert close is True and "超时" in reason


def test_no_close_small_loss_short_time():
    close, reason = chk(D("400"), D("390"), 60)  # 亏 $10(-2.5%)，未超时
    assert close is False and reason == ""


def test_amount_mode_ignores_percent_line():
    close, reason = chk(D("300"), D("240"), 60, "amount")  # -20%，但只亏 $60
    assert close is False and reason == ""


def test_percent_mode_ignores_amount_line():
    close, reason = chk(D("800"), D("720"), 60)  # 亏 $80，但只有 -10%
    assert close is False and reason == ""


def test_default_mode_is_percent():
    close, reason = watchdog_check(
        D("300"), D("240"), 60,
        stop_pct=STOP, dollar_line=DOLLAR, max_hold_s=MAXHOLD,
    )
    assert close is True and "%止损" in reason


def test_watchdog_declares_option_symbols(monkeypatch):
    """③ `_declare_option_subscriptions` 把活跃仓合约集喂给 Alpaca OPTION_STREAM。

    2026-08-15 LES 整体下线前，本条同时钉 LES_OPTION_STREAM（当时是「只读并存」的第二条流）。
    该流已随 `app/les_option_stream.py` 一并删除，故只剩 Alpaca 这一条 —— 断言口径不变：
    活跃仓合约集必须原样送达，否则止损/止盈拿不到盘口。
    """
    from app import watchdog
    from app.option_stream import OPTION_STREAM
    from app.order_state import REGISTRY, ManagedPosition

    seen = {}
    monkeypatch.setattr(OPTION_STREAM, "set_symbols", lambda s: seen.update(codes=s))

    REGISTRY._pos.clear()
    p = ManagedPosition(option_code="SPY260724C00470000", ticker="SPY", direction="long",
                        qty=1, entry_mid=D("1.00"), entry_time=datetime.now(timezone.utc),
                        order_id="e1", status="open")
    REGISTRY.record(p)
    source = SimpleNamespace(name="watcher")

    try:
        watchdog._declare_option_subscriptions(source)
        assert seen["codes"] == {"SPY260724C00470000"}
    finally:
        REGISTRY._pos.clear()
