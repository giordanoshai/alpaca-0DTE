"""订单时间戳的时钟纪律 —— 修「成交耗时显示 0ms」。

2026-08-03 实测：`ttt_order_events` 里 26/33 笔成交单的 `submitted_at` 与 `updated_at`
**到微秒完全相同**，而日志里真实耗时是 130–310ms，前端 `elapsedText` 因此显示 0ms。

根因是 `updated_ts` 混了两个时钟：
  - `submitted()` / `mark_filled()` 盖的是**本地**墙钟（与 `ts` 同源）；
  - `_upsert_broker_orders_locked()` 用**券商**墙钟无条件覆盖，且允许倒退；
  - 而 `mark_filled()` 一旦发现状态已被券商帧翻成 filled 就整体空转
    （`_transition` 只在 status=="pending" 时触发），永远不再 bump。

本文件锁死的纪律：**`ts` 与 `updated_ts` 永远来自同一个时钟**，因此两者之差
恒等于「我们观察到状态变化所花的真实时间」。券商时钟只用于判帧新旧，存在
`broker_updated_ts`，不落库、不参与耗时计算。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

import app.order_journal as order_journal
from app.order_journal import OrderJournal

_LOCAL_EPOCH = datetime(2026, 8, 3, 21, 42, 53, tzinfo=timezone.utc)


class _Clock:
    """可控的本地墙钟。

    Windows 上 `datetime.now()` 的粒度约 15.6ms，实测提交与摄入常落在同一个 tick 里，
    真实断言会偶发相等。这里注入时钟，让「耗时是否为正」变成确定性判定。
    """

    def __init__(self) -> None:
        self.now = _LOCAL_EPOCH

    def tick(self, ms: int) -> datetime:
        self.now += timedelta(milliseconds=ms)
        return self.now

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock(monkeypatch) -> _Clock:
    c = _Clock()
    monkeypatch.setattr(order_journal, "_utcnow", c)
    return c


def _j() -> OrderJournal:
    return OrderJournal()


def _broker(order_id: str, *, status: str, submitted_at: datetime,
            updated_at: datetime | None = None, filled_qty: int = 0,
            filled_avg_price: str | None = None) -> dict:
    return {
        "id": order_id,
        "symbol": "GOOGL260803C00372500",
        "side": "buy",
        "status": status,
        "qty": 2,
        "filled_qty": filled_qty,
        "filled_avg_price": filled_avg_price,
        "limit_price": "2.44",
        "client_order_id": "ttt-abc",
        "submitted_at": submitted_at,
        "updated_at": updated_at,
    }


def test_broker_frame_never_pushes_updated_ts_backwards(clock):
    """券商钟比本地钟慢时，`updated_ts` 不得被拉回到 `ts` 之前或与之相等。

    这是 0ms 的直接成因：GOOGL a650f410 本地 13:42:53.043 提交、53.214 成交，
    券商帧带着更早的时间戳把 `updated_ts` 覆盖回 53.043，两列于是完全相等。
    """
    j = _j()
    ev = j.submitted("oid", "GOOGL", "GOOGL260803C00372500", "buy", 2, Decimal("2.395"))
    submitted_ts = ev.ts

    stale = submitted_ts - timedelta(seconds=5)   # 券商钟比我们慢 5 秒
    clock.tick(171)                                # 真实成交耗时
    j.upsert_broker_orders([_broker(
        "oid", status="filled", submitted_at=stale, updated_at=stale,
        filled_qty=2, filled_avg_price="2.42",
    )], persist=False)

    got = j.get("oid")
    assert got.status == "filled"
    assert got.price == Decimal("2.42")
    assert got.ts == submitted_ts                    # 提交时刻不被券商钟改写
    assert got.updated_ts - got.ts == timedelta(milliseconds=171)


def test_elapsed_is_positive_when_broker_frame_wins_the_race(clock):
    """券商帧抢在 `mark_filled` 之前落地时，耗时仍须为正。

    此前这条必死：券商帧把状态翻成 filled 并填好成交价后，`mark_filled` 的
    `_transition` 不再触发、price/qty 也已相等，`changed` 恒为 False，
    于是既不 bump `updated_ts` 也不落库。
    """
    j = _j()
    ev = j.submitted("oid", "GOOGL", "GOOGL260803C00372500", "buy", 2, Decimal("2.395"))

    broker_ts = ev.ts - timedelta(milliseconds=40)
    clock.tick(313)
    j.upsert_broker_orders([_broker(
        "oid", status="filled", submitted_at=broker_ts, updated_at=broker_ts,
        filled_qty=2, filled_avg_price="2.42",
    )], persist=False)
    # 本地成交同步随后到达 —— 此时已无字段可改，是个纯 no-op
    clock.tick(58)
    j.mark_filled("oid", price=Decimal("2.42"), qty=2)

    got = j.get("oid")
    # 以**先观察到**的那一刻为准；no-op 不得再把耗时往后推。
    assert got.updated_ts - got.ts == timedelta(milliseconds=313)


def test_no_op_broker_frame_does_not_inflate_elapsed(clock):
    """重复的同内容券商帧不得反复推进 `updated_ts`，否则耗时会随对账拍数虚增。"""
    j = _j()
    j.submitted("oid", "GOOGL", "GOOGL260803C00372500", "buy", 2, Decimal("2.395"))
    broker_ts = datetime(2026, 8, 3, 13, 42, 53, tzinfo=timezone.utc)
    frame = _broker("oid", status="filled", submitted_at=broker_ts,
                    updated_at=broker_ts, filled_qty=2, filled_avg_price="2.42")

    clock.tick(171)
    j.upsert_broker_orders([frame], persist=False)
    first = j.get("oid").updated_ts

    clock.tick(5000)                    # 下一拍 REST 对账又送来同一帧
    j.upsert_broker_orders([frame], persist=False)
    assert j.get("oid").updated_ts == first


def test_broker_clock_kept_separately_for_staleness_check():
    """券商钟存在 `broker_updated_ts`，不与本地钟混用，也不进 to_dict()。"""
    j = _j()
    j.submitted("oid", "GOOGL", "GOOGL260803C00372500", "buy", 2, Decimal("2.395"))
    broker_ts = datetime(2026, 8, 3, 13, 42, 53, tzinfo=timezone.utc)
    j.upsert_broker_orders([_broker(
        "oid", status="filled", submitted_at=broker_ts, updated_at=broker_ts,
        filled_qty=2, filled_avg_price="2.42",
    )], persist=False)

    got = j.get("oid")
    assert got.broker_updated_ts == broker_ts
    assert "broker_updated_ts" not in got.to_dict()


def test_stale_broker_frame_cannot_revert_a_terminal_state():
    """旧 REST 快照不得把已终态的订单打回 pending —— 这条既有保护不能被本次改动破坏。"""
    j = _j()
    j.submitted("oid", "GOOGL", "GOOGL260803C00372500", "buy", 2, Decimal("2.395"))
    fresh = datetime(2026, 8, 3, 13, 42, 53, tzinfo=timezone.utc)
    j.upsert_broker_orders([_broker(
        "oid", status="filled", submitted_at=fresh, updated_at=fresh,
        filled_qty=2, filled_avg_price="2.42",
    )], persist=False)

    older = fresh - timedelta(seconds=30)
    j.upsert_broker_orders([_broker(
        "oid", status="new", submitted_at=older, updated_at=older,
    )], persist=False)

    assert j.get("oid").status == "filled"


def test_unknown_order_from_broker_keeps_broker_clock_on_both_columns():
    """从未在本地登记过的订单（重启找回/外部单）两列都用券商钟，差值仍有意义。"""
    j = _j()
    submitted = datetime(2026, 8, 3, 13, 42, 53, tzinfo=timezone.utc)
    filled = submitted + timedelta(milliseconds=171)
    j.upsert_broker_orders([_broker(
        "oid", status="filled", submitted_at=submitted, updated_at=filled,
        filled_qty=2, filled_avg_price="2.42",
    )], persist=False)

    got = j.get("oid")
    assert got.ts == submitted
    assert got.updated_ts == filled
