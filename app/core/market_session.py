"""订单展示用 ET 自然日窗口。"""
from __future__ import annotations

from datetime import datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")
_RTH_OPEN = dtime(9, 30)
_RTH_CLOSE = dtime(16, 0)


def order_session_window(now: datetime | None = None) -> dict:
    """当前 ET 自然日边界，并附带真实常规盘开闭状态。

    订单时间戳仍以 UTC 保存和传输；``session_start/session_end`` 是当前 ET
    ``00:00`` 与次日 ``00:00`` 换算后的 UTC 边界，自动覆盖夏令时切换。
    """
    ref = now or datetime.now(timezone.utc)
    ref = ref.replace(tzinfo=timezone.utc) if ref.tzinfo is None else ref
    ref_utc = ref.astimezone(timezone.utc)
    now_et = ref_utc.astimezone(_ET)
    session_date = now_et.date()
    next_date = session_date + timedelta(days=1)
    start_et = datetime.combine(session_date, dtime.min, tzinfo=_ET)
    end_et = datetime.combine(next_date, dtime.min, tzinfo=_ET)
    start_utc = start_et.astimezone(timezone.utc)
    end_utc = end_et.astimezone(timezone.utc)
    rth_start = datetime.combine(session_date, _RTH_OPEN, tzinfo=_ET).astimezone(
        timezone.utc
    )
    rth_end = datetime.combine(session_date, _RTH_CLOSE, tzinfo=_ET).astimezone(
        timezone.utc
    )
    return {
        "session_date": session_date.isoformat(),
        "session_start": start_utc,
        "session_end": end_utc,
        "query_until": min(ref_utc, end_utc),
        "is_weekday": session_date.weekday() < 5,
        "is_open": (
            session_date.weekday() < 5
            and rth_start <= ref_utc < rth_end
        ),
        "timezone": "America/New_York",
    }
