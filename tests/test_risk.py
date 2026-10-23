"""⑦ 风控聚合 compute_risk 测试。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

from app.risk import ClosedTrade, compute_risk

UTC = timezone.utc
NOW = datetime(2026, 7, 13, 20, 0, 0, tzinfo=UTC)


def ct(dt, pnl):
    return ClosedTrade(exit_time=dt, pnl=D(str(pnl)))


def test_empty_is_all_zero():
    r = compute_risk([], NOW, 600)
    assert r.daily_pnl == D("0")
    assert r.weekly_pnl == D("0")
    assert r.consecutive_losses == 0
    assert r.cooldown_remaining_s == 0


def test_daily_pnl_only_today():
    trades = [
        ct(datetime(2026, 7, 13, 10, 0, tzinfo=UTC), -50),
        ct(datetime(2026, 7, 13, 15, 0, tzinfo=UTC), -30),
        ct(datetime(2026, 7, 12, 10, 0, tzinfo=UTC), -100),  # 昨天，不计入日亏
    ]
    assert compute_risk(trades, NOW, 600).daily_pnl == D("-80")


def test_weekly_pnl_trailing_7_days():
    trades = [
        ct(datetime(2026, 7, 13, 10, 0, tzinfo=UTC), -50),
        ct(datetime(2026, 7, 8, 10, 0, tzinfo=UTC), -30),    # 5 天前，计入
        ct(datetime(2026, 7, 5, 10, 0, tzinfo=UTC), -100),   # 8 天前，不计入
    ]
    assert compute_risk(trades, NOW, 600).weekly_pnl == D("-80")


def test_consecutive_losses_trailing():
    trades = [
        ct(datetime(2026, 7, 13, 19, 55, tzinfo=UTC), 5),
        ct(datetime(2026, 7, 13, 19, 57, tzinfo=UTC), -10),
        ct(datetime(2026, 7, 13, 19, 58, tzinfo=UTC), -20),
        ct(datetime(2026, 7, 13, 19, 59, tzinfo=UTC), -30),
    ]
    assert compute_risk(trades, NOW, 600).consecutive_losses == 3


def test_consecutive_losses_reset_by_recent_win():
    trades = [
        ct(datetime(2026, 7, 13, 11, 0, tzinfo=UTC), -10),
        ct(datetime(2026, 7, 13, 12, 0, tzinfo=UTC), -20),
        ct(datetime(2026, 7, 13, 13, 0, tzinfo=UTC), 5),   # 最近是盈利 → 连亏归零
    ]
    assert compute_risk(trades, NOW, 600).consecutive_losses == 0


def test_cooldown_active_after_two_losses():
    # 最近两笔连亏，末笔 30 秒前 → 冷却剩 570
    trades = [
        ct(datetime(2026, 7, 13, 19, 58, 0, tzinfo=UTC), -10),
        ct(datetime(2026, 7, 13, 19, 59, 30, tzinfo=UTC), -20),
    ]
    r = compute_risk(trades, NOW, 600)
    assert r.consecutive_losses == 2
    assert r.cooldown_remaining_s == 570


def test_cooldown_expired():
    trades = [
        ct(datetime(2026, 7, 13, 19, 40, 0, tzinfo=UTC), -10),
        ct(datetime(2026, 7, 13, 19, 45, 0, tzinfo=UTC), -20),  # 15 分钟前 > 600s
    ]
    r = compute_risk(trades, NOW, 600)
    assert r.cooldown_remaining_s == 0
    assert r.consecutive_losses == 0


def test_loss_after_expired_cooldown_starts_new_streak_at_one():
    trades = [
        ct(datetime(2026, 7, 13, 19, 30, tzinfo=UTC), -10),
        ct(datetime(2026, 7, 13, 19, 40, tzinfo=UTC), -20),
        # 上一轮冷却在 19:50 结束；此单必须作为新周期第 1 笔亏损。
        ct(datetime(2026, 7, 13, 19, 59, tzinfo=UTC), -30),
    ]
    r = compute_risk(trades, NOW, 600)
    assert r.consecutive_losses == 1
    assert r.cooldown_remaining_s == 0


def test_two_losses_after_expired_cooldown_trigger_a_new_cooldown():
    trades = [
        ct(datetime(2026, 7, 13, 19, 20, tzinfo=UTC), -10),
        ct(datetime(2026, 7, 13, 19, 30, tzinfo=UTC), -20),
        ct(datetime(2026, 7, 13, 19, 58, tzinfo=UTC), -30),
        ct(datetime(2026, 7, 13, 19, 59, 30, tzinfo=UTC), -40),
    ]
    r = compute_risk(trades, NOW, 600)
    assert r.consecutive_losses == 2
    assert r.cooldown_remaining_s == 570


def test_cooldown_needs_two_consecutive_losses():
    # 只有 1 笔连亏（前一笔是盈利）→ 不进冷却
    trades = [
        ct(datetime(2026, 7, 13, 19, 58, 0, tzinfo=UTC), 5),
        ct(datetime(2026, 7, 13, 19, 59, 45, tzinfo=UTC), -20),
    ]
    assert compute_risk(trades, NOW, 600).cooldown_remaining_s == 0
