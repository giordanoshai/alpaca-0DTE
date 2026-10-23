"""watchdog 的券商订单更新解析与无持仓订单同步。"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from decimal import Decimal

from app import trade_recorder
from app.broker_order_state import is_order_not_found
from app.core.dailylogger import get_logger
from app.order_journal import JOURNAL
from app.position_registry import REGISTRY

logger = get_logger("tt.gate")

#: 券商侧「这单已经不在途」的终态（撤销/过期/拒绝/收市了结）。
_TERMINAL_CANCELLED = (
    "canceled",
    "cancelled",
    "expired",
    "rejected",
    "done_for_day",
)
_FILL_STATUSES = ("filled", "partially_filled")


def _filled_qty(order: dict | None) -> int:
    try:
        return int(float((order or {}).get("filled_qty") or 0))
    except (TypeError, ValueError):
        return 0


def _fill_price(order: dict | None) -> Decimal | None:
    value = (order or {}).get("filled_avg_price")
    return Decimal(str(value)) if value not in (None, "") else None


def _is_fill_update(order: dict | None) -> bool:
    if not order:
        return False
    status = (order.get("status") or "").lower()
    event = (order.get("event") or "").lower()
    return (
        status in _FILL_STATUSES
        or event in ("fill", "partial_fill")
        or (_filled_qty(order) > 0 and status in _TERMINAL_CANCELLED)
    )


def _sync_journal_only_orders(
    ec,
    updates: dict[str, dict],
    *,
    reconcile_rest: bool,
) -> None:
    """同步无独立 ManagedPosition 的订单，并按累计成交量幂等结算加仓。"""
    for order_id in JOURNAL.tracking_order_ids():
        if REGISTRY.by_order_id(order_id) or REGISTRY.by_close_order_id(order_id):
            continue
        order = updates.get(order_id)
        if order is None and reconcile_rest:
            try:
                order = ec.get_order(order_id)
            except Exception as exc:  # noqa: BLE001
                journal_event = JOURNAL.get(order_id)
                if (
                    is_order_not_found(exc)
                    and journal_event is not None
                    and journal_event.status == "pending"
                ):
                    side = journal_event.side if journal_event is not None else "buy"
                    JOURNAL.mark_cancelled(
                        order_id,
                        fail_reason="券商查无此订单，本地历史在途记录已清理",
                        cancel_origin="unknown",
                    )
                    trade_recorder.record_cancelled(
                        order_id,
                        **({} if side == "buy" else {"side": side}),
                    )
                    logger.info(json.dumps({
                        "event": "journal_only_order_not_found_resolved",
                        "order_id": order_id,
                        "side": side,
                    }, ensure_ascii=False))
                    continue
                # 超时、断网等暂态错误不能证明订单不存在；保留 pending 等待下次核对。
                logger.warning(json.dumps({
                    "event": "journal_only_order_query_error",
                    "order_id": order_id,
                    "error": str(exc),
                }, ensure_ascii=False))
                continue
        if not order:
            continue
        status = (order.get("status") or "").lower()
        journal_event = JOURNAL.get(order_id)
        side = journal_event.side if journal_event is not None else "buy"
        if _is_fill_update(order):
            price = _fill_price(order)
            qty = _filled_qty(order)
            if (
                journal_event is not None
                and journal_event.action == "add"
                and journal_event.parent_order_id
                and price is not None
            ):
                parent = REGISTRY.by_order_id(journal_event.parent_order_id)
                if parent is not None:
                    delta_qty, delta_px = JOURNAL.claim_fill_delta(
                        order_id,
                        qty,
                        price,
                    )
                    if delta_qty > 0 and delta_px is not None:
                        REGISTRY.add_qty(
                            journal_event.option_code,
                            delta_qty,
                            delta_px,
                        )
                        trade_recorder.record_add(
                            parent.order_id,
                            parent.qty,
                            parent.entry_mid,
                        )
            terminal_fill = status == "filled" or status in _TERMINAL_CANCELLED
            if terminal_fill:
                JOURNAL.mark_filled(
                    order_id,
                    price=price,
                    qty=qty if qty > 0 else None,
                )
            if side == "buy" and (
                journal_event is None or journal_event.action != "add"
            ):
                trade_recorder.record_filled(
                    order_id,
                    entry_mid=price,
                    qty=qty or None,
                )
            elif side != "buy":
                trade_recorder.record_filled(
                    order_id,
                    entry_mid=price,
                    qty=qty or None,
                    side=side,
                )
        elif status in _TERMINAL_CANCELLED:
            JOURNAL.mark_cancelled(order_id, broker_status=status)
            trade_recorder.record_cancelled(
                order_id,
                **({} if side == "buy" else {"side": side}),
            )


#: 让 pending→open 与 partial→advanced 这两段「读状态 → 算 → 写回」串行。
#:
#: 2026-08-25 起本函数有了**两个并发调用方**：watchdog 那一拍与
#: `POST /api/reconcile`，两者都跑在 `to_thread` 工作线程上。
#: `REGISTRY` 自己的 RLock 保不了这个 —— 它锁的是那个字典，不是这里跨多个字段的迁移。
#:
#: **老实说：这个锁挡的窗口目前小到测不出来。** 现有实现把所有状态写入都排在任何
#: 慢调用（`record_filled` / `record_position_sync` 落库）之前，两个分支的临界区
#: 因此都只剩几个属性赋值 + Decimal 运算；实测双线程同时进入也复现不出重复落地，
#: 写不出「去掉锁就变红」的测试。保留它是因为代价接近零（一天几次、无竞争），
#: 而**那个"先改内存态、最后才落库"的顺序一旦被人调整，窗口立刻变宽** ——
#: 到那时这把锁就是唯一还站着的东西。改这个函数的人请留意这条顺序。
_ENTRY_FILL_LOCK = threading.Lock()


def _apply_entry_fill(pos, order: dict | None, *, broker_qty: int | None = None) -> bool:
    """把原始开仓单的累计成交应用到本地（串行化入口，见 `_ENTRY_FILL_LOCK`）。"""
    with _ENTRY_FILL_LOCK:
        return _apply_entry_fill_locked(pos, order, broker_qty=broker_qty)


def _apply_entry_fill_locked(pos, order: dict | None, *,
                             broker_qty: int | None = None) -> bool:
    """把原始开仓单的累计成交应用到本地；旧快照不得覆盖后续加仓总量。

    **只能由 `_apply_entry_fill` 调用**（它负责持锁）。
    """
    fill_px = _fill_price(order)
    fill_qty = _filled_qty(order) or broker_qty
    # 成交成本未知时绝不把 provisional selection mid 当真实成本并启动止损。
    # 否则美元止损阈值会系统性后移；保留 pending，下一拍继续从 REST 补价。
    if not fill_qty or fill_px is None:
        return False

    if pos.status == "pending":
        pos.qty = fill_qty
        if fill_px is not None:
            pos.entry_mid = fill_px
        pos.entry_filled_qty = fill_qty
        pos.entry_fill_mid = fill_px
        pos.last_owned_buy_fill_at = datetime.now(timezone.utc)
        pos.status = "open"
        JOURNAL.mark_filled(pos.order_id, price=fill_px, qty=fill_qty)
        trade_recorder.record_filled(pos.order_id, entry_mid=fill_px, qty=fill_qty)
        logger.info(json.dumps({
            "event": "entry_fill_synced",
            "order_id": pos.order_id,
            "contract": pos.option_code,
            "filled_qty": fill_qty,
            "filled_avg_price": None if fill_px is None else str(fill_px),
            "source": (order or {}).get("source", "rest_reconcile"),
        }, ensure_ascii=False))
        return True

    # 兼容升级前/对账找回的 open 对象：第一次看见原始订单终态时只建立基线。
    # 当前 pos.qty 可能已经包含加仓，绝不能用首单 filled_qty 覆盖它。
    applied_qty = int(getattr(pos, "entry_filled_qty", 0) or 0)
    if applied_qty <= 0:
        pos.entry_filled_qty = fill_qty
        pos.entry_fill_mid = fill_px or pos.entry_mid
        return True

    # 数量未增加时也可能是“WS 首帧没成交价，REST 后来补齐真实价”。不能直接 no-op，
    # 否则 ttt_trades 永远保留下单前参考价。只在累计量与已应用原始入场量相等时替换
    # 原始入场组件；若 fill_qty 更小，说明恢复态含加仓，无法安全拆分，保持不动。
    if fill_qty <= applied_qty:
        old_fill_px = pos.entry_fill_mid
        if (
            fill_qty == applied_qty
            and fill_px is not None
            and old_fill_px is not None
            and fill_px != old_fill_px
        ):
            old_total = pos.entry_mid * Decimal(pos.qty)
            corrected_total = old_total + (
                (fill_px - old_fill_px) * Decimal(fill_qty)
            )
            pos.entry_mid = corrected_total / Decimal(pos.qty)
            pos.entry_fill_mid = fill_px
            JOURNAL.mark_filled(pos.order_id, price=fill_px, qty=fill_qty)
            trade_recorder.record_position_sync(pos.order_id, pos.qty, pos.entry_mid)
            logger.info(json.dumps({
                "event": "entry_fill_price_backfilled",
                "order_id": pos.order_id,
                "contract": pos.option_code,
                "filled_qty": fill_qty,
                "old_fill_price": str(old_fill_px),
                "filled_avg_price": str(fill_px),
                "position_avg_price": str(pos.entry_mid),
            }, ensure_ascii=False))
        return True

    # 原始开仓单从 partial_fill 继续成交时，只把新增部分加到当前总仓位。
    # 这样即使期间已有加仓，也不会把总数错误地重置为首单累计成交量。
    delta = fill_qty - applied_qty
    delta_px = fill_px or pos.entry_fill_mid or pos.entry_mid
    old_total = pos.entry_mid * Decimal(pos.qty)
    pos.qty += delta
    pos.entry_mid = (old_total + delta_px * Decimal(delta)) / Decimal(pos.qty)
    pos.entry_filled_qty = fill_qty
    pos.entry_fill_mid = fill_px or pos.entry_fill_mid or pos.entry_mid
    pos.last_owned_buy_fill_at = datetime.now(timezone.utc)
    trade_recorder.record_position_sync(pos.order_id, pos.qty, pos.entry_mid)
    logger.info(json.dumps({
        "event": "entry_partial_fill_advanced",
        "order_id": pos.order_id,
        "contract": pos.option_code,
        "filled_qty": fill_qty,
        "added_qty": delta,
        "position_qty": pos.qty,
    }, ensure_ascii=False))
    return True
