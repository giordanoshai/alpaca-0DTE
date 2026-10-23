"""⑦ 风控状态机（账户级）—— 从已平仓交易聚合出风控输入。

纯函数：给定已平仓交易(exit_time, pnl) + 当前时刻 → 日亏/周亏/连亏/冷却剩余。
供 WATCHER 闸门组装 AccountState 使用。
（blackout 的 minutes_since_open 依赖实时时钟，不在这里，由调用方给。）
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from math import ceil


@dataclass(frozen=True)
class ClosedTrade:
    exit_time: datetime
    pnl: Decimal


@dataclass(frozen=True)
class RiskState:
    daily_pnl: Decimal
    weekly_pnl: Decimal
    consecutive_losses: int
    cooldown_remaining_s: int


def compute_risk(trades: list[ClosedTrade], now: datetime, cooldown_secs: int) -> RiskState:
    ordered = sorted(trades, key=lambda t: t.exit_time)
    week_ago = now - timedelta(days=7)

    daily = sum((t.pnl for t in ordered if t.exit_time.date() == now.date()), Decimal(0))
    weekly = sum((t.pnl for t in ordered if t.exit_time > week_ago), Decimal(0))

    # 连亏是一个有生命周期的状态，而不是永久回看历史末尾：
    #   2 连亏 -> 从触发单起冷却；冷却到期 -> 连亏归零，下一笔亏损从 1 重新计数。
    # 逐笔回放可在服务重启后仅凭成交历史重建同一状态，也能正确处理冷却期间仍有
    # 预先持有仓位平仓的情况（新亏损会从其平仓时间重新延长冷却）。
    consecutive = 0
    cooldown_until: datetime | None = None
    cooldown_delta = timedelta(seconds=max(0, cooldown_secs))
    for t in ordered:
        if cooldown_until is not None and t.exit_time >= cooldown_until:
            consecutive = 0
            cooldown_until = None

        if t.pnl < 0:
            consecutive += 1
            if consecutive >= 2:
                cooldown_until = t.exit_time + cooldown_delta
        else:
            consecutive = 0
            cooldown_until = None

    cooldown = 0
    if cooldown_until is not None:
        remaining = (cooldown_until - now).total_seconds()
        if remaining > 0:
            cooldown = ceil(remaining)
        else:
            # 冷却周期已经完整结束：状态一并清零，避免下一笔亏损被历史连亏立即重触发。
            consecutive = 0

    return RiskState(daily_pnl=daily, weekly_pnl=weekly,
                     consecutive_losses=consecutive, cooldown_remaining_s=cooldown)
