"""撤单、平仓与平仓终态结算。

从 routers/orders.py 收口迁出（Task 5）：路由只负责解析请求 + 前置 4xx 判断（_execution_enabled）
+ `await asyncio.to_thread(order_exec.<fn>, source, ...)` 透传；本模块内聚真正的业务体，
均为同步纯函数，`source`（当前数据源）与 `req`（router 的 Pydantic 模型）作参数，返回纯 dict。

对账/未登记仓扫描改用 AlpacaTrading 公开方法（broker_positions/closed_buy_orders），
消除对 `ec._tc` 的私有穿透。
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import HTTPException

from app.broker_order_state import (
    broker_status as _broker_status,
    is_order_not_found as _is_order_not_found,
)
from app.integrations.alpaca.trading import AlpacaTrading
from app.order_pricing import atomic_close
from app.broker_pnl import sibling_program_of
from app.order_journal import JOURNAL
from app.position_registry import ManagedPosition, REGISTRY
from app import trade_recorder

from app.core.dailylogger import get_logger

logger = get_logger("tt.gate")

_EXEC_SOURCES = {"live", "watcher"}
# close_submit 的「查 status→设 closing」须原子（close_submit 经 asyncio.to_thread 落线程池，
# 两个真并发的 /api/close 同标的若都读到 open 会各起一个 close_run → 券商侧双卖）。
_CLOSE_SUBMIT_LOCK = threading.Lock()

# execute_submit 的同标的+方向去重（防抖：连点买入在 execute_run 跑完前只认第一次，返回同一
# intent_id，不会各起一个 execute_run → 各提交一次真实买单）。原子性要求与 _CLOSE_SUBMIT_LOCK 同理。
def _execution_enabled(name: str) -> bool:
    """可走 Alpaca 真实（paper）执行的数据源：watcher 实盘、live 直连。"""
    return name in _EXEC_SOURCES


def _as_dec(v) -> Decimal | None:
    """atomic_close 回传的价位是字符串 → Decimal；缺失/不可解析 → None（订单流展示用，不得抛）。"""
    if v in (None, ""):
        return None
    try:
        return Decimal(str(v))
    except Exception:  # noqa: BLE001
        return None


_CANCELLED_STATUSES = frozenset(
    {"canceled", "cancelled", "expired", "rejected", "done_for_day", "stopped", "suspended"}
)


def _filled_qty(order: dict | None) -> int:
    try:
        return max(0, int(float((order or {}).get("filled_qty") or 0)))
    except (TypeError, ValueError):
        return 0


def _filled_price(order: dict | None) -> Decimal | None:
    return _as_dec((order or {}).get("filled_avg_price"))


def _clear_close_tracking(pos: ManagedPosition) -> None:
    pos.close_order_id = None
    pos.close_qty = 0
    pos.close_applied_qty = 0
    pos.close_reason = None
    pos.close_submitted_at = None
    pos.cancel_requested = False
    pos.close_timeout_policy = "quick"


def _order_audit_context(order_id: str, pos: ManagedPosition | None,
                         close_pos: ManagedPosition | None) -> dict:
    """把入场/平仓单整理成同一套审计字段，并用 parent_order_id 串起平仓单。"""
    event = JOURNAL.get(order_id)
    managed = close_pos or pos
    fields = {
        "order_id": order_id,
        "parent_order_id": close_pos.order_id if close_pos is not None else None,
        "ticker": managed.ticker if managed is not None else getattr(event, "ticker", None),
        "direction": managed.direction if managed is not None else None,
        "contract": managed.option_code if managed is not None else getattr(event, "option_code", None),
        "side": getattr(event, "side", None),
        "qty": getattr(event, "qty", None),
    }
    return {key: value for key, value in fields.items() if value is not None}


def _apply_entry_fill_to_position(
    pos: ManagedPosition,
    cumulative_qty: int,
    fill_px: Decimal | None,
) -> bool:
    """把原始入场单累计成交幂等应用到持仓，且不覆盖已经包含加仓的总数量。"""
    cumulative_qty = max(0, int(cumulative_qty))
    pos.cancel_requested = False
    if cumulative_qty <= 0 or fill_px is None:
        # Alpaca 偶尔先给 filled/partial_fill 数量、下一帧才补均价。成本未知时保持
        # pending，不能用选约参考价启动止损；watchdog 后续会用同一券商订单补齐。
        return False

    if pos.status == "pending":
        pos.qty = cumulative_qty
        pos.entry_mid = fill_px
        pos.entry_filled_qty = cumulative_qty
        pos.entry_fill_mid = fill_px
        pos.last_owned_buy_fill_at = datetime.now(timezone.utc)
        pos.status = "open"
        trade_recorder.record_filled(
            pos.order_id, entry_mid=fill_px, qty=cumulative_qty)
        return True

    applied_qty = int(getattr(pos, "entry_filled_qty", 0) or 0)
    if applied_qty <= 0:
        # 恢复态第一次看到原始入场单终态时只建立基线；pos.qty 可能已包含加仓。
        pos.entry_filled_qty = cumulative_qty
        pos.entry_fill_mid = fill_px
        trade_recorder.record_position_sync(pos.order_id, pos.qty, pos.entry_mid)
        return True

    if cumulative_qty <= applied_qty:
        old_fill_px = pos.entry_fill_mid
        if (
            cumulative_qty == applied_qty
            and old_fill_px is not None
            and fill_px != old_fill_px
            and pos.qty > 0
        ):
            corrected_total = (
                pos.entry_mid * Decimal(pos.qty)
                + (fill_px - old_fill_px) * Decimal(cumulative_qty)
            )
            pos.entry_mid = corrected_total / Decimal(pos.qty)
            pos.entry_fill_mid = fill_px
            trade_recorder.record_position_sync(pos.order_id, pos.qty, pos.entry_mid)
        return True

    delta = cumulative_qty - applied_qty
    old_total = pos.entry_mid * Decimal(pos.qty)
    pos.qty += delta
    pos.entry_mid = (
        old_total + fill_px * Decimal(delta)
    ) / Decimal(pos.qty)
    pos.entry_filled_qty = cumulative_qty
    pos.entry_fill_mid = fill_px
    pos.last_owned_buy_fill_at = datetime.now(timezone.utc)
    pos.status = "open"
    trade_recorder.record_position_sync(pos.order_id, pos.qty, pos.entry_mid)
    return True


def _settle_filled_order(order_id: str, order: dict, pos: ManagedPosition | None,
                         close_pos: ManagedPosition | None) -> dict:
    """Apply a fill discovered while a user is trying to cancel."""
    fill_px = _filled_price(order)
    if close_pos is not None:
        requested = close_pos.close_qty or _filled_qty(order)
        cumulative_filled = min(_filled_qty(order), requested)
        sold = min(
            max(0, cumulative_filled - close_pos.close_applied_qty),
            close_pos.qty,
        )
        JOURNAL.mark_filled(
            order_id,
            price=fill_px,
            qty=cumulative_filled if cumulative_filled > 0 else None,
        )
        if sold > 0:
            trade_recorder.record_closed(
                close_pos.order_id, fill_px, sold, close_pos.close_reason or "手动平仓")
            remaining = REGISTRY.reduce(close_pos.option_code, sold, fill_px)
            REGISTRY.set_status(close_pos.option_code, "closed" if remaining <= 0 else "open")
        elif cumulative_filled > 0:
            # 这部分累计成交已在 close_run/watchdog 的先前一拍应用，不能重复减仓。
            REGISTRY.set_status(
                close_pos.option_code,
                "closed" if close_pos.qty <= 0 else "open",
            )
        else:
            REGISTRY.set_status(close_pos.option_code, "open")
            close_pos.close_error = "券商返回已成交但缺少成交数量，请立即核对账户"
        _clear_close_tracking(close_pos)
    else:
        fill_qty = _filled_qty(order)
        JOURNAL.mark_filled(
            order_id, price=fill_px, qty=fill_qty if fill_qty > 0 else None)
        if pos is not None:
            _apply_entry_fill_to_position(pos, fill_qty, fill_px)
        else:
            trade_recorder.record_filled(
                order_id, entry_mid=fill_px, qty=fill_qty or None)
    trade_recorder.audit(
        "trade_cancel_raced_with_fill",
        **_order_audit_context(order_id, pos, close_pos),
        filled_qty=_filled_qty(order),
        fill_price=fill_px,
        status="filled",
    )
    return {"cancelled": False, "order_id": order_id, "status": "filled"}


def _settle_cancelled_order(order_id: str, order: dict | None,
                            pos: ManagedPosition | None,
                            close_pos: ManagedPosition | None,
                            *, reason: str,
                            cancel_origin: str | None = None,
                            cancel_cause: str | None = None) -> dict:
    """Apply a broker-confirmed cancel, including fills that happened first."""
    context = _order_audit_context(order_id, pos, close_pos)
    if close_pos is not None:
        cumulative_filled = min(_filled_qty(order), close_pos.close_qty)
        filled = min(
            max(0, cumulative_filled - close_pos.close_applied_qty),
            close_pos.qty,
        )
        fill_px = _filled_price(order)
        known = JOURNAL.get(order_id) is not None
        if cumulative_filled > 0:
            settled = JOURNAL.mark_filled(
                order_id,
                price=fill_px,
                qty=cumulative_filled if cumulative_filled > 0 else None,
            )
        else:
            settled = JOURNAL.mark_cancelled(
                order_id,
                fail_reason=reason,
                cancel_origin=cancel_origin,
                cancel_cause=cancel_cause,
                broker_status=_broker_status(order),
            )
        duplicate = known and not settled
        if duplicate:
            # 别人（watchdog 巡检等）已结算同一终态：再扣一次仓会凭空少仓，故账目一概不写。
            logger.info(json.dumps({
                "event": "cancel_settle_skipped", "order_id": order_id,
                "side": "sell", "loser": "exec_close", "winner": "other",
                "contract": close_pos.option_code,
                **JOURNAL.settle_forensics(order_id),
            }, ensure_ascii=False))
            # 但**状态位必须修**：下面的 _clear_close_tracking 是无条件执行的，
            # 这里若连状态位一起跳过，就留下 status='closing' + close_order_id=None
            # 的死态 —— 既没有在途单可撤，也永远回不到 open。前端
            # `app-sync.js` 的 backendClosing 于此恒真，`render.js` 把卖出 / 1/3 /
            # 半仓 / 全平 一并 pointer-events:none，刷新也回不来（closing 是从后端
            # 状态算的）。2026-09-03 NVDA C227.5 现场：用户撤掉目标限价单后按钮全灰，
            # 只能去 Alpaca App 手动市价清仓。
            #
            # 用 CAS 而不是无条件 set_status：赢家可能在本函数发那几百毫秒券商请求的
            # 空档里已经把仓位标成 closed（cancel() 顶部的 is_pending 是那之前查的），
            # 无条件写会把它复活成 open —— 那正是当初把整段账目一起跳过的初衷。
            REGISTRY.compare_and_set_status(
                close_pos.option_code,
                "closing",
                "closed" if close_pos.qty <= 0 else "open",
            )
        else:
            if cumulative_filled > 0:
                if filled > 0:
                    trade_recorder.record_closed(
                        close_pos.order_id,
                        fill_px,
                        filled,
                        close_pos.close_reason or "手动平仓",
                    )
                remaining = REGISTRY.reduce(close_pos.option_code, filled, fill_px)
                REGISTRY.set_status(
                    close_pos.option_code, "closed" if remaining <= 0 else "open")
            else:
                REGISTRY.set_status(close_pos.option_code, "open")
        # 审计独立于令牌胜负：谁先到谁写，只写一次（见 claim_terminal_audit）。
        if JOURNAL.claim_terminal_audit(order_id):
            trade_recorder.audit(
                "trade_cancel_confirmed", **context,
                filled_qty=cumulative_filled,
                newly_applied_qty=0 if duplicate else filled,
                status="cancelled", reason=reason)
        # 清理本地平仓跟踪是幂等的，无论谁记的账都必须执行，否则前端永久「撤单中」。
        _clear_close_tracking(close_pos)
        return {
            "cancelled": True,
            "order_id": order_id,
            "filled_qty": cumulative_filled,
            "newly_applied_qty": 0 if duplicate else filled,
            **({"already_settled": True} if duplicate else {}),
        }

    known = JOURNAL.get(order_id) is not None
    filled = _filled_qty(order)
    if filled > 0:
        fill_px = _filled_price(order)
        settled = JOURNAL.mark_filled(order_id, price=fill_px, qty=filled)
        duplicate = known and not settled
        if duplicate:
            logger.info(json.dumps({
                "event": "cancel_settle_skipped", "order_id": order_id,
                "side": "buy", "loser": "exec_close", "winner": "other",
                "status": "partially_filled",
                **JOURNAL.settle_forensics(order_id),
            }, ensure_ascii=False))
        else:
            if pos is not None:
                _apply_entry_fill_to_position(pos, filled, fill_px)
            else:
                trade_recorder.record_filled(order_id, entry_mid=fill_px, qty=filled)
        # 审计独立于令牌胜负：谁先到谁写，只写一次。
        if JOURNAL.claim_terminal_audit(order_id):
            trade_recorder.audit(
                "trade_cancel_confirmed", **context, filled_qty=filled,
                fill_price=fill_px, status="partially_filled", reason=reason)
        if pos is not None:
            pos.cancel_requested = False  # 幂等清理：不清则前端一直显示撤单中
        return {
            "cancelled": True,
            "order_id": order_id,
            "filled_qty": filled,
            "status": "partially_filled",
            **({"already_settled": True} if duplicate else {}),
        }

    settled = JOURNAL.mark_cancelled(
        order_id,
        fail_reason=reason,
        cancel_origin=cancel_origin,
        cancel_cause=cancel_cause,
        broker_status=_broker_status(order),
    )
    duplicate = known and not settled
    # 本地终态同步是幂等的，无条件执行；只有账目走令牌闸门。
    if pos is not None and pos.status == "pending":
        pos.status = "cancelled"
    if pos is not None:
        pos.cancel_requested = False
    if duplicate:
        logger.info(json.dumps({
            "event": "cancel_settle_skipped", "order_id": order_id,
            "side": "buy", "loser": "exec_close", "winner": "other",
            "status": "cancelled",
            **JOURNAL.settle_forensics(order_id),
        }, ensure_ascii=False))
        # 账目不写，但审计仍要保证有人写过（谁先到谁写，只写一次）。
        if JOURNAL.claim_terminal_audit(order_id):
            trade_recorder.audit(
                "trade_cancel_confirmed", **context, status="cancelled", reason=reason)
        return {"cancelled": True, "order_id": order_id, "already_settled": True}
    trade_recorder.record_cancelled(order_id)
    if JOURNAL.claim_terminal_audit(order_id):
        trade_recorder.audit(
            "trade_cancel_confirmed", **context, status="cancelled", reason=reason)
    return {"cancelled": True, "order_id": order_id}


def cancel(source, order_id: str) -> dict:
    """撤入场或平仓在途单，且只按券商确认的终态修改本地状态。

    ``REGISTRY`` 会在进程重启后丢失，所以订单日志中的 pending 事件也可
    作为撤单依据；平仓挂单则通过 ``close_order_id`` 定位。
    """
    if not _execution_enabled(source.name):
        raise HTTPException(status_code=400, detail="撤单需切到 watcher（或 live）数据源")
    event = JOURNAL.get(order_id)
    # 归属闸：兄弟系统（SOS/nDTE）的单在同一个账户里，撤掉的可能正是它的止盈保护。
    # SOS 已于 2026-08-15 移出本项目（备份 D:\SOS_BAK），这道闸反而更要留着——原先
    # SOS 侧还有自己的 UI 能管这些单，现在没有了，账户里残留的 `sos-` 挂单在本系统
    # 里彻底无人看管，误撤就是净损失。不能只靠「订单流里看不见」当保护。
    sibling = sibling_program_of(event.client_order_id if event is not None else None)
    if sibling is not None:
        raise HTTPException(
            status_code=403,
            detail=f"该订单属于 {sibling}(nDTE) 系统，请到 {sibling} 面板撤单")
    pos = REGISTRY.by_order_id(order_id)
    close_pos = REGISTRY.by_close_order_id(order_id)
    is_pending = (
        (event is not None and event.status == "pending")
        or (pos is not None and pos.status == "pending")
        or (close_pos is not None and close_pos.status == "closing")
    )
    if not is_pending:
        raise HTTPException(status_code=404, detail="无此在途单或订单已是终态，拒绝撤单")

    audit_context = _order_audit_context(order_id, pos, close_pos)
    trade_recorder.audit(
        "trade_cancel_requested", **audit_context, source=source.name)
    ec = AlpacaTrading()
    try:
        order = ec.get_order(order_id)
    except Exception as e:  # noqa: BLE001
        if _is_order_not_found(e):
            result = _settle_cancelled_order(
                order_id, None, pos, close_pos,
                reason="券商已无此订单，本地在途状态已清理",
                cancel_origin="unknown")
            result["already_terminal"] = True
            return result
        logger.warning(json.dumps(
            {"event": "cancel_query_error", "order_id": order_id, "error": str(e)},
            ensure_ascii=False))
        trade_recorder.audit(
            "trade_cancel_failed", **audit_context, source=source.name,
            stage="broker_query", reason=str(e), status="pending")
        raise HTTPException(status_code=502, detail=f"查询券商订单失败，尚未撤单: {e}") from e

    status = _broker_status(order)
    if status == "filled":
        return _settle_filled_order(order_id, order, pos, close_pos)
    if status in _CANCELLED_STATUSES:
        result = _settle_cancelled_order(
            order_id, order, pos, close_pos,
            reason=f"券商订单已终止（{status}）",
            cancel_origin="unknown")
        result["already_terminal"] = True
        return result

    from app.trade_update_stream import TRADE_UPDATE_STREAM

    ws_revision = TRADE_UPDATE_STREAM.order_revision(order_id)
    # 必须先写本地撤单意图，再调用券商。模拟盘/快速券商可能在 cancel() 返回前就通过
    # TradingStream 推送 canceled；若后写，watchdog 会把正常手动撤单误判成异常取消。
    JOURNAL.mark_cancel_requested(
        order_id,
        cancel_origin="user",
        cancel_cause="manual",
        reason="用户手动撤单",
    )
    if pos is not None:
        pos.cancel_requested = True
    if close_pos is not None:
        close_pos.cancel_requested = True
    try:
        ec.cancel(order_id)
    except Exception as e:  # noqa: BLE001
        JOURNAL.clear_cancel_requested(
            order_id,
            cancel_origin="user",
            cancel_cause="manual",
            reason="用户手动撤单",
        )
        if pos is not None and pos.status == "pending":
            pos.cancel_requested = False
        if (
            close_pos is not None
            and close_pos.status == "closing"
            and close_pos.close_order_id == order_id
        ):
            close_pos.cancel_requested = False
        logger.warning(json.dumps({"event": "cancel_error", "order_id": order_id, "error": str(e)}, ensure_ascii=False))
        trade_recorder.audit(
            "trade_cancel_failed", **audit_context, source=source.name,
            stage="broker_cancel", reason=str(e), status="pending")
        raise HTTPException(status_code=502, detail=f"券商撤单失败，订单仍在途中: {e}") from e

    # Alpaca 撤单是异步受理。TradingStream 连通时等待 WS 终态；断线时才按旧方式
    # 短时 REST 查询。最后仍未终态就明确返回 cancel_pending，绝不提前谎报 cancelled。
    latest = order
    for attempt in range(6):
        if TRADE_UPDATE_STREAM.connected:
            ws_order = TRADE_UPDATE_STREAM.wait_for_order(
                order_id,
                after_revision=ws_revision,
                timeout_s=0.2,
            )
            if ws_order is None:
                continue
            latest = ws_order
            ws_revision = int(ws_order.get("revision") or ws_revision)
        else:
            if attempt:
                time.sleep(0.2)
            try:
                latest = ec.get_order(order_id)
            except Exception as e:  # noqa: BLE001
                if _is_order_not_found(e):
                    result = _settle_cancelled_order(
                        order_id, None, pos, close_pos,
                        reason="用户手动撤单（券商已移除订单）",
                        cancel_origin="user",
                        cancel_cause="manual")
                    result["already_terminal"] = True
                    return result
                break
        status = _broker_status(latest)
        if status == "filled":
            return _settle_filled_order(order_id, latest, pos, close_pos)
        if status in _CANCELLED_STATUSES:
            result = _settle_cancelled_order(
                order_id, latest, pos, close_pos,
                reason="用户手动撤单",
                cancel_origin="user",
                cancel_cause="manual")
            logger.info(json.dumps(
                {"event": "cancel", "order_id": order_id,
                 "contract": (close_pos or pos).option_code if (close_pos or pos) else None},
                ensure_ascii=False))
            return result

    trade_recorder.audit(
        "trade_cancel_pending", **audit_context, source=source.name,
        broker_status=_broker_status(latest), status="cancel_pending")
    return {
        "cancelled": False, "accepted": True, "order_id": order_id,
        "status": "cancel_pending",
    }


#: 加仓时**不适用**的闸门规则（其余一律照拦）：
#:   contract —— ③合约选择。合约已经持有，闸门另选一张 ATM 来验没有意义。
#:   position/funds —— 闸门算的是"新选那张"的成本；加仓要买的是**持仓那张**，
#:                     口径不同，由本函数用其真实报价重算（见下方显式检查）。

def close_submit(source, req, route_started: float | None = None) -> dict:
    """⑤ 手动平仓受理（快、同步）：校验 → 清旧错误 → 设 closing → 秒返回；不跑 atomic_close。

    已在 closing（重复点击/在途）→ 幂等返回 spawn=False，不重复起后台 worker。
    真正的追单在 close_run（后台）里跑，成交/失败经 /api/positions 的 status/close_error 回显。
    """
    if not _execution_enabled(source.name):
        raise HTTPException(status_code=400, detail="平仓需切到 watcher（或 live）数据源")
    # 与 execute_submit 同款：量 asyncio.to_thread 的调度排队。平仓也是点击触发、
    # 也跑在工作线程里，事件链同样是从本函数里的 audit 才起步。同为 perf_counter，
    # 无跨钟问题；未传就不带该字段。
    timing = (
        {} if route_started is None
        else {"route_wait_ms": round((time.perf_counter() - route_started) * 1000, 1)}
    )
    pos = REGISTRY.get(req.option_code)
    if pos is None:
        raise HTTPException(status_code=404, detail="未在本系统登记的持仓，拒绝平仓")
    # 原子 check-and-set：并发同标的平仓只放行一个 close_run，其余幂等 spawn=False（防双卖）。
    with _CLOSE_SUBMIT_LOCK:
        if not REGISTRY.compare_and_set_status(
            req.option_code, "open", "closing"
        ):
            trade_recorder.audit(
                "trade_close_requested",
                order_id=pos.order_id,
                ticker=pos.ticker,
                direction=pos.direction,
                contract=pos.option_code,
                qty=min(req.qty, pos.qty) if req.qty is not None else pos.qty,
                reason=req.reason,
                price_type=req.price_type or "bid",
                source=source.name,
                duplicate=True,
                **timing,
            )
            return {"accepted": True, "status": "closing", "spawn": False}
        pos.close_error = None
        pos.close_timeout_policy = getattr(req, "timeout_policy", "quick")
        pos.close_submitted_at = None
        pos.cancel_requested = False
    trade_recorder.audit(
        "trade_close_requested",
        order_id=pos.order_id,
        ticker=pos.ticker,
        direction=pos.direction,
        contract=pos.option_code,
        qty=min(req.qty, pos.qty) if req.qty is not None else pos.qty,
        reason=req.reason,
        price_type=req.price_type or "bid",
        source=source.name,
        duplicate=False,
        **timing,
    )
    return {"accepted": True, "status": "closing", "spawn": True}


def cancel_close(source, option_code: str) -> dict:
    """撤在途平仓挂单；结算规则与订单面板的通用撤单完全一致。"""
    if not _execution_enabled(source.name):
        raise HTTPException(status_code=400, detail="撤单需切到 watcher（或 live）数据源")
    pos = REGISTRY.get(option_code)
    if pos is None or pos.status != "closing" or not pos.close_order_id:
        raise HTTPException(status_code=404, detail="无在途平仓挂单可撤")
    return cancel(source, pos.close_order_id)


def close_run(source, req) -> dict:
    """⑤ 手动平仓后台执行：跑 atomic_close（原样），成交→closed/减仓、失败/异常→回退 open + close_error。

    在后台线程跑（不占 HTTP）。atomic_close 逻辑不变（共享 watchdog 追单引擎）；
    任何异常都转成 close_error 回退，不得让后台任务静默崩溃。
    """
    pos = REGISTRY.get(req.option_code)
    if pos is None:
        return {"closed": False, "error": "持仓已不存在"}
    sell_qty = min(req.qty, pos.qty) if req.qty is not None else pos.qty  # ⑪ 部分平仓
    partial = sell_qty < pos.qty
    ptype = req.price_type or "bid"
    try:
        lp = Decimal(req.limit_price) if req.limit_price not in (None, "") else None
        res = atomic_close(AlpacaTrading(), source.alpaca, pos.option_code, sell_qty, req.reason,
                           price_type=ptype, limit_price=lp,
                           ticker=pos.ticker, parent_order_id=pos.order_id)
    except Exception as e:  # noqa: BLE001 — 后台失败不得崩溃：回退 open + 记 close_error
        REGISTRY.set_status(req.option_code, "open")
        _clear_close_tracking(pos)
        pos.close_error = f"平仓失败: {e}"
        logger.warning(json.dumps({"event": "close_run_error", "contract": req.option_code,
                                   "error": str(e)}, ensure_ascii=False))
        trade_recorder.audit(
            "trade_close_result",
            order_id=pos.order_id,
            ticker=pos.ticker,
            direction=pos.direction,
            contract=pos.option_code,
            side="sell",
            qty=sell_qty,
            reason=req.reason,
            price_type=ptype,
            status="error",
            error=str(e),
            source=source.name,
        )
        return {"closed": False, "error": str(e)}
    reported_filled = res.get("filled_qty")
    try:
        filled_qty = int(float(reported_filled)) if reported_filled is not None else 0
    except (TypeError, ValueError):
        filled_qty = 0
    if res.get("closed") and filled_qty <= 0:
        # “closed” 不是数量证据。真实执行器必须回传券商累计成交量；缺失时宁可停下
        # 等对账，也不能按请求量猜测并篡改本地仓位。
        res = {
            **res,
            "closed": False,
            "error": "平仓结果缺少 Alpaca 成交数量，已停止本地结算并等待对账",
            "reconcile_required": True,
        }
    filled_qty = min(max(0, filled_qty), sell_qty, pos.qty)

    position_qty_before = pos.qty
    remaining_position = pos.qty
    if filled_qty > 0:
        # 只认 Alpaca filled_avg_price 汇总出的 fill；盘口 mid 只是触发/展示参考，
        # 不是成交价，缺价时宁可留空也不能猜进已实现 PnL。
        exit_px = res.get("fill")
        exit_mid = Decimal(exit_px) if exit_px else None
        close_order = res.get("order") if isinstance(res.get("order"), dict) else {}
        sell_id = str(close_order.get("id") or "")
        # 正常 atomic_close 已逐手记事件；保留此兜底供替换执行器/恢复态使用，
        # 但绝不覆盖追价阶段已经记录的真实单笔成交。
        if sell_id and JOURNAL.get(sell_id) is None:
            JOURNAL.submitted(
                sell_id, pos.ticker, pos.option_code, "sell", filled_qty,
                reason=req.reason, price_type=ptype,
                limit_price=_as_dec(res.get("limit")),
                origin="user",
                action="reduce" if filled_qty < pos.qty else "close",
                parent_order_id=pos.order_id,
            )
            if not res.get("resting"):
                JOURNAL.mark_filled(
                    sell_id, price=_as_dec(res.get("fill")), qty=filled_qty)
        trade_recorder.record_closed(
            pos.order_id, exit_mid, filled_qty, req.reason)
        remaining_position = REGISTRY.reduce(
            req.option_code, filled_qty, exit_mid=exit_mid)

    if res.get("resting") and remaining_position > 0:
        # 平仓单已提交、券商侧排队未成交：保持 closing（平仓挂单中），绝不谎报成交。
        # atomic_close 可能已经确认了前几手/当前手的部分成交：上面先统一减本地仓，
        # 再只追踪当前仍在途订单的“累计量中已应用多少”，防止 watchdog 二次扣减。
        order = res.get("order") if isinstance(res.get("order"), dict) else {}
        sell_id = str(order.get("id") or "")
        if not sell_id:
            REGISTRY.set_status(req.option_code, "open")
            _clear_close_tracking(pos)
            pos.close_error = "平仓执行器返回挂单中但缺少券商订单号，请核对账户"
            res = {**res, "resting": False, "error": pos.close_error}
        else:
            try:
                current_order_qty = max(
                    0, int(float(res.get("current_order_qty") or sell_qty)))
            except (TypeError, ValueError):
                current_order_qty = sell_qty
            try:
                current_order_filled = max(
                    0, int(float(res.get("current_order_filled_qty") or 0)))
            except (TypeError, ValueError):
                current_order_filled = 0
            current_order_filled = min(current_order_filled, current_order_qty)
            REGISTRY.set_status(req.option_code, "closing")
            pos.close_order_id = sell_id
            pos.close_qty = current_order_qty
            pos.close_applied_qty = current_order_filled
            pos.close_reason = req.reason
            pos.close_submitted_at = datetime.now(timezone.utc)  # ⑥ 超时撤单的起算点
            pos.close_timeout_policy = getattr(req, "timeout_policy", "quick")
            if JOURNAL.get(sell_id) is None:
                JOURNAL.submitted(
                    sell_id, pos.ticker, pos.option_code, "sell", current_order_qty,
                    reason=req.reason, price_type=ptype,
                    limit_price=_as_dec(res.get("limit")),
                    origin="user",
                    action="reduce" if current_order_qty < position_qty_before else "close",
                    client_order_id=order.get("client_order_id"),
                    parent_order_id=pos.order_id,
                )
            if res.get("passive"):
                # 挂在自己这侧队列里等人来吃（如平仓挂卖一）→ 0DTE 上可能排到收盘也不成交。
                # 界面只显示"平仓中…"会让人误以为在追单，必须说清楚（2026-07-25 QQQ 减仓事件）。
                pos.close_error = (f"以{ptype}挂单=被动排队，券商侧可能不成交；"
                                   f"可撤单后改用买一(bid)重试")
            elif filled_qty > 0:
                pos.close_error = (
                    f"已成交 {filled_qty}/{sell_qty} 张，"
                    f"剩余 {remaining_position} 张中当前挂单继续等待券商成交"
                )
            else:
                pos.close_error = None
    elif filled_qty > 0:
        REGISTRY.set_status(
            req.option_code, "closed" if remaining_position <= 0 else "open")
        if filled_qty < sell_qty:
            pos.close_error = (
                f"追价单仅成交 {filled_qty}/{sell_qty} 张，"
                f"剩余 {sell_qty - filled_qty} 张未成交，可重试平仓"
            )
        else:
            pos.close_error = None
        _clear_close_tracking(pos)  # 已了结，清在途平仓挂单追踪
    else:
        REGISTRY.set_status(req.option_code, "open")
        _clear_close_tracking(pos)
        pos.close_error = res.get("error") or "穿价追单未成交"
    close_order = res.get("order") if isinstance(res.get("order"), dict) else {}
    trade_recorder.audit(
        "trade_close_result",
        order_id=pos.order_id,
        close_order_id=close_order.get("id"),
        ticker=pos.ticker,
        direction=pos.direction,
        contract=pos.option_code,
        side="sell",
        qty=sell_qty,
        reason=req.reason,
        price_type=ptype,
        limit_price=res.get("limit"),
        fill_price=res.get("fill"),
        status=("filled" if res.get("closed") else
                "resting" if res.get("resting") else "not_filled"),
        error=res.get("error"),
        timeout_policy=getattr(req, "timeout_policy", "quick"),
        source=source.name,
    )
    logger.info(json.dumps({"event": "close", "trade_id": pos.order_id,
                            "partial": partial, "sell_qty": sell_qty,
                            **{k: v for k, v in res.items() if k != "order"}}, ensure_ascii=False))
    return res
