"""账户、交易次数与风控状态装配。"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from app import db
from app.risk import ClosedTrade, compute_risk
from app.templates import get_store
from app.validator import AccountState

_ET = ZoneInfo("America/New_York")
_CLOSED_TRADES_TTL_S = 30.0
_closed_trades_cache: tuple[float, list[ClosedTrade]] | None = None


def invalidate_closed_trades_cache() -> None:
    global _closed_trades_cache
    _closed_trades_cache = None


def closed_trades() -> list[ClosedTrade]:
    """读取本系统已平仓交易；平仓主动失效，30 秒 TTL 兜底。"""
    global _closed_trades_cache
    hit = _closed_trades_cache
    if hit is not None and (time.monotonic() - hit[0]) < _CLOSED_TRADES_TTL_S:
        return hit[1]
    with db.connect() as conn:
        cur = conn.cursor()
        cur.execute("""
            select exit_time, total_pnl from ttt_trades
            where status='closed' and exit_time is not null
              and coalesce(source, '') <> 'mock'
            order by exit_time
        """)
        trades = [
            ClosedTrade(exit_time=exit_time, pnl=pnl)
            for exit_time, pnl in cur.fetchall()
        ]
    _closed_trades_cache = (time.monotonic(), trades)
    return trades


def count_trades_since(start: datetime) -> int:
    with db.connect() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            select count(*) from ttt_trades
            where entry_time >= %s
              and status in ('open', 'closed', 'external_closed')
              and coalesce(source, '') <> 'mock'
            """,
            (start,),
        )
        row = cur.fetchone()
        return int(row[0] if row else 0)


def trade_count_today(now: datetime | None = None) -> int:
    as_of = now or datetime.now(timezone.utc)
    now_et = as_of.astimezone(_ET)
    day_start = now_et.replace(hour=0, minute=0, second=0, microsecond=0)
    return count_trades_since(day_start.astimezone(timezone.utc))


def build_account_state(source, minutes_since_open: Decimal) -> AccountState:
    now = datetime.now(timezone.utc)
    risk_state = compute_risk(
        closed_trades(),
        now,
        get_store().active().cooldown_seconds,
    )
    broker = source.program_pnl(now)
    return AccountState(
        daily_pnl=broker.daily.program_gross_pnl,
        weekly_pnl=broker.weekly.program_gross_pnl,
        consecutive_losses=risk_state.consecutive_losses,
        cooldown_remaining_s=risk_state.cooldown_remaining_s,
        minutes_since_open=minutes_since_open,
        buying_power=source.buying_power(),
        trades_today=trade_count_today(now),
    )
