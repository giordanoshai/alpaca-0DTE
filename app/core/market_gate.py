"""行情数据窗口的单一权威判断 —— 非盘中不拉数据。

服务 24 小时常驻（2026-09-02 起撤掉了 cron 定时停机），网页随时能开；省下的
是**外部请求**：盯盘报价 3~5 秒一轮、GEX 墙 30 秒一轮、UW 潮汐 30 秒一轮，
非盘中全部挂起。

窗口 = 开盘前 5 分钟 ~ 收盘后 5 分钟。

判据分两层：

1. **券商时钟**（`AlpacaData.clock()`）。它认美股节假日与半日市，是纯时钟做
   不到的。`ClockInfo` 没有 `next_open` 时间字段，但休市时它的 `session_date`
   正好是**下一个交易日**，靠这个就能分辨：
     - `session_date == 今天` → 今天还没开盘，处于盘前
     - `session_date >  今天` → 今天的盘结束了，**或今天根本不是交易日**
2. **纯时钟兜底**：券商接口抛异常时用「周一~周五 09:25–16:05 ET」。
   降级方向是**放行** —— 宁可在节假日多拉一天数据，也不能因为券商接口抽风把
   整个盯盘停掉。丢的只是节假日感知，也就是退回本次改造之前的行为。

收盘后那 5 分钟靠进程内记住的「最后一次见到券商说开着」来判，而不是写死
16:00 —— 这样半日市（13:00 收盘）自动就对了，不用维护假日表。代价是进程若在
收盘后那 5 分钟内才启动，就没有缓冲；少拉几分钟而已，无害。
"""
from __future__ import annotations

import datetime as dt
import time
from dataclasses import dataclass
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")

PRE_OPEN_MINUTES = 5
POST_CLOSE_MINUTES = 5

# 兜底窗口（券商时钟不可用时用），与上面两个常量保持一致
_FALLBACK_OPEN = dt.time(9, 25)
_FALLBACK_CLOSE = dt.time(16, 5)

# 券商时钟的缓存时长。几个循环各自调用，30 秒内只会真打一次接口。
CLOCK_CACHE_TTL_S = 30.0


@dataclass
class _State:
    cached_open: bool | None = None
    cached_state: str = "closed"
    cached_at: float = 0.0
    last_open_at: dt.datetime | None = None


_STATE = _State()


def reset_cache(keep_last_open: bool = False) -> None:
    """清掉缓存。测试用；`keep_last_open` 保留「最后一次开着」以便测收盘缓冲。"""
    last = _STATE.last_open_at if keep_last_open else None
    _STATE.cached_open = None
    _STATE.cached_state = "closed"
    _STATE.cached_at = 0.0
    _STATE.last_open_at = last


def _fetch_clock():
    """取券商时钟。单独一个函数是为了测试能整体替换掉。"""
    from app.integrations.alpaca.data import AlpacaData

    global _ALPACA
    if _ALPACA is None:
        _ALPACA = AlpacaData(cache_ttl=CLOCK_CACHE_TTL_S)
    return _ALPACA.clock()


_ALPACA = None


def _fallback_open(now_et: dt.datetime) -> bool:
    """纯时钟兜底：周一~周五 09:25–16:05 ET。不认节假日。"""
    if now_et.weekday() >= 5:
        return False
    return _FALLBACK_OPEN <= now_et.time() < _FALLBACK_CLOSE


def _decide(now_et: dt.datetime) -> bool:
    try:
        clock = _fetch_clock()
    except Exception:  # noqa: BLE001 —— 券商接口抽风不能停掉盯盘，降级放行
        return _fallback_open(now_et)

    if clock.is_open:
        _STATE.last_open_at = now_et
        return True

    today = now_et.date()
    next_session = clock.session_date

    # 下一场就是今天 → 今天是交易日、只是还没开盘。是否已进入盘前预热窗口，
    # 交给纯时钟判断（券商时钟给不出「距开盘还有几分钟」）。
    if next_session == today:
        return _FALLBACK_OPEN <= now_et.time()

    # 下一场在今天之后 → 今天的盘已收，或今天不是交易日（周末/节假日）。
    # 只有「刚收盘不久」才继续放行。
    last_open = _STATE.last_open_at
    if last_open is not None:
        elapsed = (now_et - last_open).total_seconds()
        if 0 <= elapsed <= POST_CLOSE_MINUTES * 60:
            return True
    return False


def _now_et(now: dt.datetime | None) -> dt.datetime:
    if now is None:
        return dt.datetime.now(_ET)
    if now.tzinfo is None:
        return now.replace(tzinfo=_ET)
    return now.astimezone(_ET)


def data_window_open(now: dt.datetime | None = None) -> bool:
    """现在该不该拉行情。几个后台循环在自己的每轮开头调这一个。"""
    now_et = _now_et(now)
    age = time.monotonic() - _STATE.cached_at
    if _STATE.cached_open is not None and age < CLOCK_CACHE_TTL_S:
        return _STATE.cached_open

    is_open = _decide(now_et)
    _STATE.cached_open = is_open
    _STATE.cached_state = "open" if is_open else "closed"
    _STATE.cached_at = time.monotonic()
    return is_open


def session_state(now: dt.datetime | None = None) -> str:
    """给前端的状态串：``"open"`` / ``"closed"``。

    前端据此显示「休市」徽章 —— 非盘中卡片会冻在收盘时的值，不标出来的话
    看的人分不清自己盯的是实时价还是昨天的残影。
    """
    data_window_open(now)
    return _STATE.cached_state
