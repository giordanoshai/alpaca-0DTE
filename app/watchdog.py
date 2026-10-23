"""持仓巡检编排：券商对账、止盈单终态、风险退出与后台循环。

金额/%/最长持仓风险判断与锁存执行拆在 ``watchdog_exit.py``；本模块保证任何
在途卖单进入券商终态前都不会再提交第二张卖单。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from decimal import Decimal

from app import trade_recorder
from app.config import ALPACA_TRADE_DISCONNECTED_RECONCILE_INTERVAL_S
from app.config import ALPACA_TRADE_RECONCILE_INTERVAL_S
from app.integrations.alpaca.trading import AlpacaTrading
from app.option_stream import OPTION_STREAM
from app.order_pricing import POSITION_SETTLE_GRACE_S, atomic_close
from app.exec_entry import _execution_enabled
from app.order_journal import JOURNAL
from app.position_registry import REGISTRY
from app.services.data_source_registry import current_source
from app.services.market_context import underlying_price
from app.trade_update_stream import TRADE_UPDATE_STREAM
from app.watchdog_exit import (
    aged_out,
    clear_risk_exit,
    close_order_aged_out,
    execute_latched_risk_exit,
    latch_risk_exit,
    mark_stale_risk_cancel,
    order_timeout_s,
    price_risk_trigger,
    request_close_cancel,
    time_risk_trigger,
    watchdog_check,
)
from app.watchdog_order_sync import (
    _TERMINAL_CANCELLED,
    _apply_entry_fill,
    _fill_price,
    _filled_qty,
    _is_fill_update,
    _sync_journal_only_orders,
)

from app.core.dailylogger import get_logger

logger = get_logger("tt.gate")

# Alpaca trade_updates 会先确认期权成交，账户级 positions REST 随后才聚合出新张数。
# 在这个最终一致性窗口内，较小的 REST 数量是旧快照，不是外部减仓。
_BROKER_POSITION_SETTLE_GRACE_S = POSITION_SETTLE_GRACE_S

# 一拍超过这个秒数就告警。正常一拍是毫秒级（券商 REST 实测 200~500ms）。
# 2026-08-25 那次卡死之所以只能靠 8 分 48 秒后的 RemoteDisconnected 事后倒推，
# 就是因为 `trade_rest_reconciled` 是在一拍**跑完之后**才打的 —— 一拍没跑完
# 就永远不打，日志上看起来像"什么都没发生"。
WATCHDOG_TICK_SLOW_S = 10.0


def _finalize_resting_close(pos, order: dict) -> None:
    """平仓挂单经券商确认成交 → 结算：全平置 closed / 部分平减仓回 open；订单流卖单 mark_filled、⑧ 落库。

    与 close_run 成交分支同口径，只是成交确认从「submit 即认」改成「watchdog 读券商订单状态确认」。
    """
    code = pos.option_code
    cumulative_filled = min(_filled_qty(order), pos.close_qty)
    if (order.get("status") or "").lower() == "filled" and cumulative_filled <= 0:
        cumulative_filled = pos.close_qty
    sold = min(
        max(0, cumulative_filled - pos.close_applied_qty),
        pos.qty,
    )
    reason = pos.close_reason or "手动平仓"
    exit_px = order.get("filled_avg_price")
    exit_mid = Decimal(str(exit_px)) if exit_px else None
    close_order_id = pos.close_order_id
    JOURNAL.mark_filled(
        pos.close_order_id,
        price=exit_mid,
        qty=cumulative_filled,
    )
    if sold > 0:
        trade_recorder.record_closed(pos.order_id, exit_mid, sold, reason)
    remaining = REGISTRY.reduce(code, sold, exit_mid)
    risk_exit = pos.risk_exit_state is not None
    if remaining <= 0:
        pos.status = "closed"
    elif risk_exit:
        pos.status = "closing"
    else:
        pos.status = "open"
    trade_recorder.audit(
        "trade_close_filled",
        order_id=pos.order_id,
        close_order_id=close_order_id,
        ticker=pos.ticker,
        direction=pos.direction,
        contract=code,
        side="sell",
        sold_qty=cumulative_filled,
        newly_applied_qty=sold,
        fill_price=exit_mid,
        remaining_qty=remaining,
        status=pos.status,
        reason=reason,
    )
    _clear_resting_close_tracking(pos, clear_risk=remaining <= 0 or not risk_exit)
    if risk_exit and remaining > 0:
        pos.risk_exit_state = "closing_remaining"
    logger.info(json.dumps({"event": "resting_close_filled", "trade_id": pos.order_id,
                            "contract": code,
                            "remaining": remaining, "sold": sold}, ensure_ascii=False))


def _clear_resting_close_tracking(pos, *, clear_risk: bool = True) -> None:
    """清空平仓挂单跟踪字段，并恢复下一笔卖单的默认短超时策略。"""
    pos.close_order_id = None
    pos.close_qty = 0
    pos.close_applied_qty = 0
    pos.close_reason = None
    pos.close_submitted_at = None
    pos.cancel_requested = False
    pos.close_timeout_policy = "quick"
    if clear_risk:
        clear_risk_exit(pos)


def _watchdog_tick(
    source,
    *,
    reconcile_rest: bool = True,
    order_updates: dict[str, dict] | None = None,
) -> None:
    """一拍：WS 订单事件 + 定时 REST 对账 + 挂单超时 + open 仓止损/止盈。

    ``reconcile_rest=False`` 时跳过本拍的周期性订单/持仓 REST 对账，优先消费
    TradingStream 缓存；超时撤单、止损平仓等必要动作仍会按各自动作链调用 Trading REST。
    旧调用默认仍做完整 REST 对账，便于诊断与测试。
    """
    ec = AlpacaTrading()
    broker = ec.broker_position_qtys() if reconcile_rest else None
    updates = order_updates or {}
    _sync_journal_only_orders(ec, updates, reconcile_rest=reconcile_rest)
    alpaca = source.alpaca
    timeout_s = order_timeout_s()
    for pos in REGISTRY.active():
        code = pos.option_code
        if pos.status == "pending":
            update = updates.get(pos.order_id)
            if _is_fill_update(update):
                order = update
                if _fill_price(order) is None:
                    try:
                        order = ec.get_order(pos.order_id)
                    except Exception as e:  # noqa: BLE001 — 下拍继续补真实成交成本
                        logger.warning(json.dumps({
                            "event": "fill_price_query_error",
                            "order_id": pos.order_id,
                            "error": str(e),
                        }, ensure_ascii=False))
                if _apply_entry_fill(pos, order):
                    continue
            if update and (update.get("status") or "").lower() in _TERMINAL_CANCELLED:
                status = (update.get("status") or "").lower()
                known = JOURNAL.get(pos.order_id) is not None
                settled = JOURNAL.mark_cancelled(
                    pos.order_id,
                    broker_status=status,
                )
                # 本地终态同步无条件执行（幂等）；只有账目走令牌闸门。
                REGISTRY.set_status(code, "cancelled")
                if known and not settled:
                    logger.info(json.dumps({
                        "event": "cancel_settle_skipped", "order_id": pos.order_id,
                        "side": "buy", "loser": "watchdog_stream",
                        "winner": "other", "contract": code,
                        **JOURNAL.settle_forensics(pos.order_id),
                    }, ensure_ascii=False))
                else:
                    trade_recorder.record_cancelled(pos.order_id)
                # 审计独立于令牌胜负：谁先到谁写，只写一次（见 claim_terminal_audit）。
                if JOURNAL.claim_terminal_audit(pos.order_id):
                    trade_recorder.audit(
                        "trade_cancel_confirmed", order_id=pos.order_id, ticker=pos.ticker,
                        direction=pos.direction, contract=code, side="buy", qty=pos.qty,
                        trigger="trading_stream", broker_status=status, status="cancelled",
                        source=getattr(source, "name", None))
                continue
            if broker is not None and code in broker:
                # 回填券商真实成交价/成交量（审查 3.2）：entry_mid 原本是③选约时的 mid，
                # 而实际是以 ask（或 ask+1档）成交的 → entry_val 偏低、止损两条线都算在
                # 虚成本上。成交价只有券商知道，必须问它。查不到就沿用旧值，绝不写坏分母。
                order = update
                try:
                    if order is None or _fill_price(order) is None:
                        order = ec.get_order(pos.order_id)
                except Exception as e:  # noqa: BLE001 — 保持 pending，下拍继续补真实成本
                    logger.warning(json.dumps({"event": "fill_backfill_error",
                                               "order_id": pos.order_id, "error": str(e)},
                                              ensure_ascii=False))
                _apply_entry_fill(pos, order, broker_qty=broker.get(code))
                continue
            # ⑥ 超时撤单：已发过撤单请求 → 只等券商终态，不重复发、不臆断结果
            if pos.cancel_requested:
                o = update
                if o is None and not reconcile_rest:
                    continue
                try:
                    if o is None:
                        o = ec.get_order(pos.order_id)
                except Exception as e:  # noqa: BLE001 — 查单失败本拍跳过，下拍再试
                    logger.warning(json.dumps({"event": "timeout_cancel_query_error",
                                               "order_id": pos.order_id, "error": str(e)},
                                              ensure_ascii=False))
                    trade_recorder.audit(
                        "trade_cancel_failed", order_id=pos.order_id, ticker=pos.ticker,
                        direction=pos.direction, contract=code, side="buy", qty=pos.qty,
                        trigger="watchdog_timeout", stage="broker_query", reason=str(e),
                        status="cancel_pending", source=getattr(source, "name", None))
                    continue
                if (o.get("status") or "").lower() in _TERMINAL_CANCELLED:
                    known = JOURNAL.get(pos.order_id) is not None
                    settled = JOURNAL.mark_cancelled(  # ⑦ 订单流：超时撤单，说清为什么没成交
                        pos.order_id, broker_status=(o.get("status") or "").lower())
                    # 本地终态同步无条件执行（幂等）；只有账目走令牌闸门。
                    REGISTRY.set_status(code, "cancelled")
                    if known and not settled:
                        logger.info(json.dumps({
                            "event": "cancel_settle_skipped", "order_id": pos.order_id,
                            "side": "buy", "loser": "watchdog_timeout",
                            "winner": "other", "contract": code,
                            **JOURNAL.settle_forensics(pos.order_id),
                        }, ensure_ascii=False))
                    else:
                        trade_recorder.record_cancelled(pos.order_id)  # ⑧
                    # 审计独立于令牌胜负：谁先到谁写，只写一次。
                    if JOURNAL.claim_terminal_audit(pos.order_id):
                        trade_recorder.audit(
                            "trade_cancel_confirmed", order_id=pos.order_id, ticker=pos.ticker,
                            direction=pos.direction, contract=code, side="buy", qty=pos.qty,
                            trigger="watchdog_timeout", status="cancelled",
                            reason=f"挂单超时 {timeout_s:.0f} 秒未成交",
                            source=getattr(source, "name", None))
                    logger.info(json.dumps({"event": "timeout_cancel_settled", "contract": code,
                                            "order_id": pos.order_id}, ensure_ascii=False))
                continue
            # 挂满阈值仍未成交 → 撤（DAY 单不撤会一路排到收盘）
            if aged_out(pos.entry_time, timeout_s):
                try:
                    ec.cancel(pos.order_id)
                    pos.cancel_requested = True
                    JOURNAL.mark_cancel_requested(
                        pos.order_id,
                        cancel_origin="system",
                        cancel_cause="timeout",
                        reason=f"挂单超时 {timeout_s:.0f} 秒未成交，系统自动撤单",
                    )
                    trade_recorder.audit(
                        "trade_cancel_requested", order_id=pos.order_id, ticker=pos.ticker,
                        direction=pos.direction, contract=code, side="buy", qty=pos.qty,
                        trigger="watchdog_timeout", timeout_s=timeout_s,
                        source=getattr(source, "name", None))
                    logger.info(json.dumps({"event": "timeout_cancel_buy",
                                            "trade_id": pos.order_id, "contract": code,
                                            "order_id": pos.order_id, "timeout_s": timeout_s},
                                           ensure_ascii=False))
                except Exception as e:  # noqa: BLE001 — 撤单失败下拍再试（不置 flag）
                    logger.warning(json.dumps({"event": "timeout_cancel_error",
                                               "order_id": pos.order_id, "error": str(e)},
                                              ensure_ascii=False))
                    trade_recorder.audit(
                        "trade_cancel_failed", order_id=pos.order_id, ticker=pos.ticker,
                        direction=pos.direction, contract=code, side="buy", qty=pos.qty,
                        trigger="watchdog_timeout", stage="broker_cancel", reason=str(e),
                        status="pending", source=getattr(source, "name", None))
            continue
        # 部分成交后本地已进入 open；同一买单后续 fill 帧仍需更新最终张数/均价。
        entry_update = updates.get(pos.order_id)
        if pos.status == "open" and _is_fill_update(entry_update):
            _apply_entry_fill(pos, entry_update)

        if pos.close_order_id:
            # 平仓挂单在券商排队：按券商订单状态确认最终成交/撤销（不依赖 code not in broker——
            # 部分平仓时剩余仓仍在券商）。铁律：券商确认成交前，绝不谎报已成交。
            o = updates.get(pos.close_order_id)
            if o is None and reconcile_rest:
                try:
                    o = ec.get_order(pos.close_order_id)
                except Exception as e:  # noqa: BLE001 — 查单失败本拍跳过，下拍再试
                    logger.warning(json.dumps({"event": "resting_close_query_error",
                                               "order_id": pos.close_order_id, "error": str(e)}, ensure_ascii=False))
            if o is None:
                # 无新 WS 帧且未到 REST 对账拍：已锁存风险时继续确保撤单请求已发；
                # 普通 quick 卖单仍只按短超时撤销。
                if pos.risk_exit_state is not None:
                    mark_stale_risk_cancel(
                        pos,
                        wait_s=timeout_s,
                        source_name=getattr(source, "name", None),
                    )
                    request_close_cancel(
                        pos,
                        ec,
                        cancel_cause="risk_preempt",
                        reason="风险退出触发，先撤现有止盈/卖单",
                        trigger="watchdog_risk_preempt",
                        source_name=getattr(source, "name", None),
                    )
                elif close_order_aged_out(pos, timeout_s):
                    request_close_cancel(
                        pos,
                        ec,
                        cancel_cause="timeout",
                        reason=f"平仓挂单超时 {timeout_s:.0f} 秒未成交，系统自动撤单",
                        trigger="watchdog_timeout",
                        timeout_s=timeout_s,
                        source_name=getattr(source, "name", None),
                    )
                continue
            st = (o.get("status") or "").lower()
            filled_q = _filled_qty(o)
            if st == "filled" or filled_q >= pos.close_qty:
                terminal_close_order_id = pos.close_order_id
                _finalize_resting_close(pos, o)
                if pos.risk_exit_state is not None and pos.qty > 0:
                    execute_latched_risk_exit(
                        pos,
                        ec,
                        alpaca,
                        source_name=getattr(source, "name", None),
                        close_executor=atomic_close,
                        confirmed_terminal_order_ids={terminal_close_order_id},
                    )
            elif st in _TERMINAL_CANCELLED:
                close_order_id = pos.close_order_id
                close_qty = pos.close_qty
                close_reason = pos.close_reason
                close_event = JOURNAL.get(close_order_id)
                manual_cancel = (
                    st in {"canceled", "cancelled"}
                    and close_event is not None
                    and (
                        close_event.cancel_cause == "manual"
                        or close_event.cancel_origin == "user"
                    )
                )
                newly_filled = min(
                    max(0, filled_q - pos.close_applied_qty),
                    pos.qty,
                )
                risk_exit = pos.risk_exit_state is not None
                known = close_event is not None
                if filled_q > 0:
                    exit_px = o.get("filled_avg_price")
                    exit_mid = Decimal(str(exit_px)) if exit_px else None
                    settled = JOURNAL.mark_filled(
                        pos.close_order_id,
                        price=exit_mid,
                        qty=filled_q,
                    )
                else:
                    exit_mid = None
                    settled = JOURNAL.mark_cancelled(  # ⑦ 订单流：平仓卖单券商侧撤/拒
                        pos.close_order_id, broker_status=st)
                # 结算令牌只闸「账目」（扣仓 + 落库 + 审计）这类不可重复的副作用。
                # 下面的本地跟踪清理是幂等的，必须无条件执行——2026-08-01 MOCK 实测：
                # 落败就 continue 会让 close_order_id 永远留着，watchdog 每拍重新
                # 撞见同一终态订单，刷屏 skip 日志，前端永久卡在「撤单中」。
                if known and not settled:
                    logger.info(json.dumps({
                        "event": "cancel_settle_skipped", "order_id": close_order_id,
                        "side": "sell", "loser": "watchdog_close",
                        "winner": "other", "contract": code,
                        "filled_qty": filled_q,
                        **JOURNAL.settle_forensics(close_order_id),
                    }, ensure_ascii=False))
                else:
                    if filled_q > 0 and newly_filled > 0:
                        # 撤单终态前已部分成交：已卖张数必须扣减并落库，否则持仓高估 →
                        # 后续全量平仓超卖报错（订单流按已成交记，成交是事实）
                        trade_recorder.record_closed(
                            pos.order_id,
                            exit_mid,
                            newly_filled,
                            pos.close_reason or "手动平仓",
                        )
                        REGISTRY.reduce(code, newly_filled, exit_mid)
                # 审计独立于令牌胜负：抢先置终态的可能是不写审计的纯状态同步路径，
                # 靠令牌决定审计会整笔丢失（2026-08-01 MOCK: 8 笔撤单丢 6 笔）。
                if JOURNAL.claim_terminal_audit(close_order_id):
                    trade_recorder.audit(
                        "trade_cancel_confirmed", order_id=close_order_id,
                        parent_order_id=pos.order_id, ticker=pos.ticker,
                        direction=pos.direction, contract=code, side="sell", qty=close_qty,
                        filled_qty=filled_q, newly_applied_qty=newly_filled,
                        fill_price=o.get("filled_avg_price"),
                        trigger=(
                            "watchdog_risk_preempt" if risk_exit
                            else "manual_cancel" if manual_cancel
                            else "watchdog_reconcile"
                        ),
                        broker_status=st, status="cancelled",
                        reason=close_reason or "平仓单券商终止",
                        source=getattr(source, "name", None))
                remaining = pos.qty
                _clear_resting_close_tracking(pos, clear_risk=not risk_exit)
                if remaining <= 0:
                    pos.status = "closed"
                    clear_risk_exit(pos)
                elif risk_exit:
                    pos.status = "closing"
                    pos.risk_exit_state = "closing_remaining"
                    pos.close_error = None
                    execute_latched_risk_exit(
                        pos,
                        ec,
                        alpaca,
                        source_name=getattr(source, "name", None),
                        close_executor=atomic_close,
                        confirmed_terminal_order_ids={close_order_id},
                    )
                else:
                    pos.status = "open"
                    if manual_cancel:
                        # 用户主动撤单且券商确认 canceled 是正常完成，不应再弹
                        # “平仓未成交”的红色错误；撤单确认及可能的部分成交已由订单流展示。
                        pos.close_error = None
                    else:
                        pos.close_error = (
                            f"平仓单券商侧撤销前已成交 {filled_q}/{close_qty} 张"
                            f"（新增结算 {newly_filled} 张），余仓可重试平仓"
                            if filled_q > 0
                            else f"平仓单券商侧未成交（{st}），已回退持仓，可重试平仓"
                        )
                logger.info(json.dumps({"event": "resting_close_cancelled", "contract": code,
                                        "broker_status": st, "filled_qty": filled_q}, ensure_ascii=False))
            elif pos.risk_exit_state is None:
                # 长期目标单只豁免短挂单超时；金额/%/最长持仓仍须每拍判定。
                close, reason = time_risk_trigger(pos, checker=watchdog_check)
                trigger_quote = None
                trigger_value = None
                if not close:
                    close, reason, trigger_quote, trigger_value = price_risk_trigger(
                        pos,
                        alpaca,
                        option_stream=OPTION_STREAM,
                        checker=watchdog_check,
                    )
                if close:
                    latch_risk_exit(
                        pos,
                        reason,
                        trigger_quote=trigger_quote,
                        trigger_value=trigger_value,
                        source_name=getattr(source, "name", None),
                    )
                    request_close_cancel(
                        pos,
                        ec,
                        cancel_cause="risk_preempt",
                        reason="风险退出触发，先撤现有止盈/卖单",
                        trigger="watchdog_risk_preempt",
                        source_name=getattr(source, "name", None),
                    )
                elif close_order_aged_out(pos, timeout_s):
                    request_close_cancel(
                        pos,
                        ec,
                        cancel_cause="timeout",
                        reason=f"平仓挂单超时 {timeout_s:.0f} 秒未成交，系统自动撤单",
                        trigger="watchdog_timeout",
                        timeout_s=timeout_s,
                        source_name=getattr(source, "name", None),
                    )
            else:
                mark_stale_risk_cancel(
                    pos,
                    wait_s=timeout_s,
                    source_name=getattr(source, "name", None),
                )
                request_close_cancel(
                    pos,
                    ec,
                    cancel_cause="risk_preempt",
                    reason="风险退出触发，先撤现有止盈/卖单",
                    trigger="watchdog_risk_preempt",
                    source_name=getattr(source, "name", None),
                )
            # 其余（new/accepted/partially_filled 未达 close_qty 且未超时）→ 保持 closing
            continue
        if pos.risk_exit_state is not None:
            # 旧卖单已确认终态或上一轮风险追价未完成：锁存原因继续退出，绝不因行情反弹放弃。
            execute_latched_risk_exit(
                pos,
                ec,
                alpaca,
                source_name=getattr(source, "name", None),
                close_executor=atomic_close,
            )
            continue
        if broker is not None and code not in broker:  # REST 对账确认券商侧已无此仓
            # closing 且没有 close_order_id 表示 close_run 正在同步提交/追单。
            # 这段窗口由 close_run 自己登记真实 sell order；watchdog 若抢先
            # 合成 :ext，会让同一次平仓在订单流里显示两笔成交。
            if pos.status == "closing":
                continue
            reason = "external_closed"
            pos.status = "external_closed"
            trade_recorder.record_closed(  # ⑧ 外部/收尾平仓落库（无成交价→exit_mid None）
                pos.order_id, None, pos.qty, reason, external=True)
            # ⑦ 订单流：#9 让"系统强制平仓/外部平仓"在订单流面板可见（此前完全没有卖单记录，
            # 用户只看到仓位从持仓列表里消失、不知道是谁平的、为什么）。无真实券商卖单号，
            # 用登记单号加后缀区分（不会与真实 order_id 冲突）。
            ext_id = pos.order_id + ":ext"
            JOURNAL.submitted(
                ext_id, pos.ticker, pos.option_code, "sell", pos.qty, reason=reason,
                origin="external", action="close", parent_order_id=pos.order_id)
            JOURNAL.mark_filled(ext_id)
            logger.info(json.dumps({"event": "reconcile", "contract": code, "status": pos.status}, ensure_ascii=False))
            continue
        if pos.status == "open":
            # 张数以 Alpaca 为准（审查 7.4③）：本地是推演账本，券商才是真源。不一致就覆盖，
            # 否则 atomic_close 的 target=券商持仓−卖出量 会算错，"全平"少卖留仓或多卖超卖。
            # 放在止损判定之前 —— 本拍的 entry_val/current_val 要用已校正的张数算。
            # （pending / 平仓挂单在途 两种情形在上面就 continue 了，不会走到这里被误判。）
            if broker is not None:
                broker_qty = broker.get(code)
                if broker_qty is not None and broker_qty < pos.qty:
                    local_qty = pos.qty
                    recent_fill_at = pos.last_owned_buy_fill_at
                    fill_age_s = (
                        (datetime.now(timezone.utc) - recent_fill_at).total_seconds()
                        if recent_fill_at is not None
                        else None
                    )
                    if (
                        fill_age_s is not None
                        and fill_age_s < _BROKER_POSITION_SETTLE_GRACE_S
                    ):
                        pos.sync_note = (
                            f"本系统刚确认买入 {local_qty} 张，Alpaca 持仓快照仍为 "
                            f"{broker_qty} 张；等待券商持仓快照追上"
                        )
                        logger.info(json.dumps({
                            "event": "position_qty_snapshot_stale",
                            "contract": code,
                            "local_qty": local_qty,
                            "broker_qty": broker_qty,
                            "owned_fill_age_ms": round(fill_age_s * 1000),
                        }, ensure_ascii=False))
                    else:
                        logger.warning(json.dumps({
                            "event": "position_qty_desync",
                            "contract": code,
                            "local_qty": local_qty,
                            "broker_qty": broker_qty,
                        }, ensure_ascii=False))
                        pos.sync_note = (
                            f"张数已按券商校正：本地 {local_qty} → Alpaca {broker_qty}"
                        )
                        pos.qty = broker_qty
                        trade_recorder.record_position_sync(
                            pos.order_id,
                            broker_qty,
                        )
                elif broker_qty is not None and broker_qty > pos.qty:
                    # Alpaca 按账户聚合同合约数量；多出的部分可能来自用户的其他程序。
                    # 本程序只管理自己的生命周期，不得把额外数量并入 REGISTRY/watchdog。
                    pos.sync_note = (
                        f"Alpaca 同合约总仓 {broker_qty}，本程序管理 {pos.qty}；"
                        f"额外 {broker_qty - pos.qty} 张未接管"
                    )
                elif pos.sync_note is not None and broker_qty == pos.qty:
                    pos.sync_note = None      # 已恢复一致，清掉过期告警
            # 时间线独立于行情；未到时间线时再检查 bid 价格止损。
            # （触墙止盈已随 GEX 于 2026-08-11 下线，退出只剩时间线与价格两条。）
            close, reason = time_risk_trigger(pos, checker=watchdog_check)
            trigger_quote = None
            trigger_value = None
            if not close:
                close, reason, trigger_quote, trigger_value = price_risk_trigger(
                    pos,
                    alpaca,
                    option_stream=OPTION_STREAM,
                    checker=watchdog_check,
                )
            if close:
                # 手动平仓与自动止损可能在同一毫秒争抢；只有成功从 open→closing 的
                # 一方拥有执行权，另一方必须退出，杜绝双卖。
                if not REGISTRY.compare_and_set_status(code, "open", "closing"):
                    continue
                latch_risk_exit(
                    pos,
                    reason,
                    trigger_quote=trigger_quote,
                    trigger_value=trigger_value,
                    source_name=getattr(source, "name", None),
                )
                execute_latched_risk_exit(
                    pos,
                    ec,
                    alpaca,
                    source_name=getattr(source, "name", None),
                    close_executor=atomic_close,
                )
                logger.info(json.dumps({"event": "watchdog_close", "contract": code, "reason": reason}, ensure_ascii=False))


def _declare_option_subscriptions(source) -> None:
    """每拍向期权流声明订阅集 = 活跃仓合约（非可执行档 → 空集不建连）。

    2026-08-15 起只剩 Alpaca OPTION_STREAM 一条：原先并存的 LES_OPTION_STREAM 随 LES
    数据源整体下线一并删除（它自 DATA_SOURCE 切回 yahoo 后就再没被喂过数据，
    quote() 恒 None，08-14 全天零条 option_price_compare 可证）。
    """
    active = REGISTRY.active()
    enabled = _execution_enabled(source.name)
    codes = {p.option_code for p in active} if enabled else set()
    OPTION_STREAM.set_symbols(codes)


async def _run_watchdog_tick(source, *, reconcile_rest: bool,
                             order_updates: dict[str, dict]) -> None:
    """跑一拍并计时；超过 ``WATCHDOG_TICK_SLOW_S`` 留一条带耗时的告警。

    一拍是串行的：这个线程卡多久，止损/超时平仓/触墙检查/成交回填就停多久。
    抛异常的那一拍同样要留痕 —— 被对端掐断（RemoteDisconnected）正是现场的样子，
    而"卡了多久"只有这里知道。
    """
    started = time.monotonic()
    try:
        await asyncio.to_thread(
            _watchdog_tick,
            source,
            reconcile_rest=reconcile_rest,
            order_updates=order_updates,
        )
    finally:
        elapsed = time.monotonic() - started
        if elapsed >= WATCHDOG_TICK_SLOW_S:
            logger.warning(json.dumps({
                "event": "watchdog_tick_slow",
                "elapsed_s": round(elapsed, 1),
                "threshold_s": WATCHDOG_TICK_SLOW_S,
                "reconcile_rest": reconcile_rest,
                # 卡住期间止损/超时/触墙全停，受影响的就是这些仓
                "active_positions": len(REGISTRY.active()),
            }, ensure_ascii=False))


async def watchdog_loop() -> None:
    """watchdog：TradingStream 事件驱动，REST 仅定时/断线/重连对账。

    活跃持仓每一帧期权报价与成交/部分成交/撤单/拒单事件都会提前唤醒；一秒定时拍仅作
    WS 无更新时的兜底。TradingStream 连通时每
    ``ALPACA_TRADE_RECONCILE_INTERVAL_S`` 秒用 REST 校验持仓真源；断线时降级为每秒
    REST，连接代次变化后立即再对账一次，补齐断线窗口内可能漏掉的事件。
    """
    next_rest_reconcile_at = 0.0
    seen_connection_generation = -1
    seen_connected: bool | None = None
    while True:
        try:
            trade_wait = asyncio.create_task(
                TRADE_UPDATE_STREAM.wait_activity(1.0))
            option_wait = asyncio.create_task(
                OPTION_STREAM.wait_activity(1.0))
            _done, pending = await asyncio.wait(
                {trade_wait, option_wait},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            # 先清标志再取快照：清理后到达的新帧会再次 set，且本次快照也能读到它。
            TRADE_UPDATE_STREAM.clear_activity()
            OPTION_STREAM.clear_activity()
            source = current_source()
            active = REGISTRY.active()
            journal_tracking_ids = JOURNAL.tracking_order_ids()
            enabled = _execution_enabled(source.name)
            _declare_option_subscriptions(source)
            if not enabled or (not active and not journal_tracking_ids):
                continue

            now = time.monotonic()
            connected = TRADE_UPDATE_STREAM.connected
            generation = TRADE_UPDATE_STREAM.connection_generation
            connection_changed = (
                generation != seen_connection_generation
                or connected != seen_connected
            )
            reconcile_rest = connection_changed or now >= next_rest_reconcile_at
            order_ids = {
                order_id
                for pos in active
                for order_id in (pos.order_id, pos.close_order_id)
                if order_id
            } | journal_tracking_ids
            updates = TRADE_UPDATE_STREAM.orders_snapshot(order_ids)
            await _run_watchdog_tick(
                source,
                reconcile_rest=reconcile_rest,
                order_updates=updates,
            )
            if reconcile_rest:
                interval = (
                    ALPACA_TRADE_RECONCILE_INTERVAL_S
                    if connected
                    else ALPACA_TRADE_DISCONNECTED_RECONCILE_INTERVAL_S
                )
                next_rest_reconcile_at = time.monotonic() + max(0.5, interval)
                seen_connection_generation = generation
                seen_connected = connected
                logger.info(json.dumps({
                    "event": "trade_rest_reconciled",
                    "trading_stream_connected": connected,
                    "next_reconcile_s": max(0.5, interval),
                    "active_positions": len(active),
                }, ensure_ascii=False))
        except asyncio.CancelledError:
            break
        except Exception as e:  # noqa: BLE001
            logger.warning(json.dumps({"event": "watchdog_error", "error": str(e)}, ensure_ascii=False))
