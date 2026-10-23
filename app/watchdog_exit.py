"""watchdog 风险退出状态机的触发快照与平仓执行。

本模块只处理已经锁存的金额/%/最长持仓退出。旧卖单的券商终态核对仍由
``watchdog.py`` 编排；只有确认不存在在途卖单后才会调用这里提交新单。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal

from app import trade_recorder
from app.core.dailylogger import get_logger
from app.order_pricing import atomic_close
from app.order_journal import JOURNAL
from app.position_registry import REGISTRY

logger = get_logger("tt.gate")


def watchdog_check(
    entry_val: Decimal,
    current_val: Decimal,
    elapsed_s: float,
    *,
    stop_pct: Decimal,
    dollar_line: Decimal,
    max_hold_s: float,
    stop_mode: str = "percent",
) -> tuple[bool, str]:
    """时间退出始终开启；价格止损严格按金额/比例二选一。"""
    unreal = current_val - entry_val
    if elapsed_s >= max_hold_s:
        return True, f"超时 {max_hold_s / 60:.0f}分"
    if stop_mode == "amount":
        if unreal <= -dollar_line:
            return True, f"$止损 亏${abs(unreal):.0f} ≥ ${dollar_line:.0f}"
        return False, ""
    pnl_pct = (unreal / entry_val) if entry_val > 0 else Decimal(0)
    if pnl_pct <= -stop_pct:
        return True, f"%止损 {pnl_pct * 100:.0f}% ≤ -{stop_pct * 100:.0f}%"
    return False, ""


def order_timeout_s() -> float:
    """当前模板的挂单超时；0 或读取失败表示停用自动撤单。"""
    try:
        from app.templates import get_store
        return float(get_store().active().order_timeout_s)
    except Exception as exc:  # noqa: BLE001 — 配置故障不得打断风险巡检
        logger.warning(json.dumps({
            "event": "order_timeout_config_error",
            "error": str(exc),
        }, ensure_ascii=False))
        return 0.0


def aged_out(since, timeout_s: float) -> bool:
    if timeout_s <= 0 or since is None:
        return False
    return (datetime.now(timezone.utc) - since).total_seconds() >= timeout_s


def close_order_aged_out(pos, timeout_s: float) -> bool:
    """长期目标限价单只豁免短挂单超时。"""
    return (
        getattr(pos, "close_timeout_policy", "quick") != "persistent"
        and aged_out(pos.close_submitted_at, timeout_s)
    )


def time_risk_trigger(pos, *, checker=watchdog_check) -> tuple[bool, str]:
    elapsed = (datetime.now(timezone.utc) - pos.entry_time).total_seconds()
    return checker(
        pos.entry_val,
        pos.entry_val,
        elapsed,
        stop_pct=pos.stop_pct,
        dollar_line=pos.dollar_line,
        max_hold_s=pos.max_hold_s,
        stop_mode=pos.stop_mode,
    )


def price_risk_trigger(
    pos,
    alpaca,
    *,
    option_stream,
    checker=watchdog_check,
) -> tuple[bool, str, tuple[Decimal, Decimal] | None, Decimal | None]:
    """用可变现 bid 判金额/% 止损；无报价时只跳过价格线。"""
    quote = (
        option_stream.quote(pos.option_code)
        or alpaca.option_quote(pos.option_code)
    )
    if not quote:
        return False, "", None, None
    bid, ask = quote
    current_val = bid * Decimal(100) * Decimal(pos.qty)
    elapsed = (datetime.now(timezone.utc) - pos.entry_time).total_seconds()
    close, reason = checker(
        pos.entry_val,
        current_val,
        elapsed,
        stop_pct=pos.stop_pct,
        dollar_line=pos.dollar_line,
        max_hold_s=pos.max_hold_s,
        stop_mode=pos.stop_mode,
    )
    return close, reason, (bid, ask), current_val


def request_close_cancel(
    pos,
    ec,
    *,
    cancel_cause: str,
    reason: str,
    trigger: str,
    source_name: str | None,
    timeout_s: float | None = None,
) -> bool:
    """请求撤当前卖单；成功后只等券商终态，不重复发送。"""
    if not pos.close_order_id or pos.cancel_requested:
        return False
    try:
        ec.cancel(pos.close_order_id)
        pos.cancel_requested = True
        JOURNAL.mark_cancel_requested(
            pos.close_order_id,
            cancel_origin="system",
            cancel_cause=cancel_cause,
            reason=reason,
        )
        trade_recorder.audit(
            "trade_cancel_requested",
            order_id=pos.close_order_id,
            parent_order_id=pos.order_id,
            ticker=pos.ticker,
            direction=pos.direction,
            contract=pos.option_code,
            side="sell",
            qty=pos.close_qty,
            trigger=trigger,
            timeout_s=timeout_s,
            source=source_name,
        )
        return True
    except Exception as exc:  # noqa: BLE001 — 不置已请求标志，下拍安全重试
        if cancel_cause == "risk_preempt":
            pos.close_error = f"止损已触发，但旧止盈单撤销失败，保持保护态重试：{exc}"
        logger.warning(json.dumps({
            "event": "close_cancel_error",
            "order_id": pos.close_order_id,
            "cancel_cause": cancel_cause,
            "error": str(exc),
        }, ensure_ascii=False))
        trade_recorder.audit(
            "trade_cancel_failed",
            order_id=pos.close_order_id,
            parent_order_id=pos.order_id,
            ticker=pos.ticker,
            direction=pos.direction,
            contract=pos.option_code,
            side="sell",
            qty=pos.close_qty,
            trigger=trigger,
            stage="broker_cancel",
            reason=str(exc),
            status="resting",
            source=source_name,
        )
        return False


def latch_risk_exit(
    pos,
    reason: str,
    *,
    trigger_quote: tuple[Decimal, Decimal] | None,
    trigger_value: Decimal | None,
    source_name: str | None,
) -> None:
    """首次触发时冻结证据；后续行情反弹也不能取消这次风险退出。"""
    if pos.risk_exit_state is not None:
        return
    triggered_at = datetime.now(timezone.utc)
    trigger_bid = trigger_quote[0] if trigger_quote is not None else None
    trigger_ask = trigger_quote[1] if trigger_quote is not None else None
    trigger_loss = (
        None if trigger_value is None else pos.entry_val - trigger_value
    )
    stop_threshold = (
        pos.dollar_line if pos.stop_mode == "amount" else pos.stop_pct
    )
    context = {
        "triggered_at": triggered_at,
        "trigger_bid": trigger_bid,
        "trigger_ask": trigger_ask,
        "trigger_value": trigger_value,
        "trigger_loss": trigger_loss,
        "trigger_qty": pos.qty,
        "stop_mode": pos.stop_mode,
        "stop_threshold": stop_threshold,
        "entry_value": pos.entry_val,
    }
    pos.risk_exit_state = (
        "canceling_existing" if pos.close_order_id else "closing_remaining"
    )
    pos.risk_exit_reason = reason
    pos.risk_exit_context = context
    pos.close_error = None
    trade_recorder.audit(
        "trade_watchdog_triggered",
        order_id=pos.order_id,
        ticker=pos.ticker,
        direction=pos.direction,
        contract=pos.option_code,
        side="sell",
        qty=pos.qty,
        entry_price=pos.entry_mid,
        reason=reason,
        source=source_name,
        **context,
    )


def clear_risk_exit(pos) -> None:
    pos.risk_exit_state = None
    pos.risk_exit_reason = None
    pos.risk_exit_context = None


def mark_stale_risk_cancel(
    pos,
    *,
    wait_s: float,
    source_name: str | None,
) -> bool:
    """旧卖单撤销长期无终态时留下可见紧急错误，但绝不放宽防双卖。"""
    if (
        pos.risk_exit_state != "canceling_existing"
        or not pos.cancel_requested
    ):
        return False
    triggered_at = (pos.risk_exit_context or {}).get("triggered_at")
    if not isinstance(triggered_at, datetime):
        return False
    elapsed_s = (datetime.now(timezone.utc) - triggered_at).total_seconds()
    threshold_s = max(10.0, float(wait_s or 0))
    if elapsed_s < threshold_s:
        return False
    _audit_block_once(
        pos,
        (
            f"止损已触发，但旧卖单撤销 {elapsed_s:.0f} 秒仍未获券商终态；"
            "保持保护态并禁止重挂"
        ),
        source_name=source_name,
    )
    return True


def _open_sell_conflicts(
    ec,
    option_code: str,
    *,
    confirmed_terminal_order_ids: set[str] | None = None,
) -> tuple[list[str], list[str]]:
    """返回（本程序仍在途卖单，外部卖单）；测试桩无该能力时保持兼容。"""
    getter = getattr(ec, "open_sell_orders_for", None)
    if not callable(getter):
        return [], []
    owned: list[str] = []
    external: list[str] = []
    confirmed_terminal_ids = {
        str(order_id)
        for order_id in (confirmed_terminal_order_ids or set())
        if order_id
    }
    for order in getter(option_code) or []:
        order_id = str(order.get("id") or "")
        if not order_id:
            continue
        if order_id in confirmed_terminal_ids:
            continue
        (owned if order.get("owned") else external).append(order_id)
    return owned, external


def _audit_block_once(pos, message: str, *, source_name: str | None) -> None:
    if pos.close_error == message:
        return
    pos.close_error = message
    trade_recorder.audit(
        "trade_watchdog_result",
        order_id=pos.order_id,
        ticker=pos.ticker,
        direction=pos.direction,
        contract=pos.option_code,
        side="sell",
        qty=pos.qty,
        status="blocked",
        reason=pos.risk_exit_reason,
        error=message,
        source=source_name,
    )


def _broker_reports_flat(result: dict) -> bool:
    """执行器那次**实时** REST 读数明确报 0 张才算数；缺字段一律不算。

    只认 `order_pricing.close_position` 里「qty <= 0 → 禁止提交卖单」那条路
    （它同时带 `reconcile_required` 与 `broker_position_qty`）。宁可多重试一拍，
    也不能靠猜把一个还在的仓判成已平 —— 那会让风险退出提前放手。
    """
    if not result.get("reconcile_required"):
        return False
    raw = result.get("broker_position_qty")
    if raw is None or isinstance(raw, bool):
        return False
    try:
        return int(float(raw)) <= 0
    except (TypeError, ValueError):
        return False


def _settle_broker_flat(pos, *, reason: str, source_name: str | None) -> None:
    """券商侧已无此仓：收尾并解除保护态。**不伪造成交价。**

    那几张确实卖掉了，但成交价在异常里丢了。落库 `exit_mid=None` 让 PnL 缺口
    显式可见，绝不拿盘口 mid 猜进已实现盈亏。收尾方式与 `watchdog.py:596` 的
    `external_closed` 分支一致（同样只置状态 + 落库，不合成 JOURNAL 卖单，
    避免与真实卖单在订单流里重复计一笔）。
    """
    lost_qty = pos.qty
    trade_recorder.audit(
        "trade_position_desync_closed",
        trade_id=pos.order_id,
        contract=pos.option_code,
        ticker=pos.ticker,
        direction=pos.direction,
        local_qty=lost_qty,
        broker_position_qty=0,
        reason=reason,
        status="closed_by_reconcile",
        source=source_name,
    )
    trade_recorder.record_closed(
        pos.order_id, None, lost_qty, "watchdog:" + reason, external=True,
    )
    pos.status = "external_closed"
    pos.close_error = (
        f"券商侧已无此仓（本地 {lost_qty} 张），按券商校正并结束风险退出；"
        "成交价已丢失，未计入已实现盈亏"
    )
    clear_risk_exit(pos)


def execute_latched_risk_exit(
    pos,
    ec,
    alpaca,
    *,
    source_name: str | None,
    close_executor=None,
    confirmed_terminal_order_ids: set[str] | None = None,
) -> bool:
    """提交并结算锁存风险退出；返回本拍是否实际调用了执行器。"""
    if pos.risk_exit_state is None:
        return False
    if pos.qty <= 0:
        pos.status = "closed"
        clear_risk_exit(pos)
        return False

    try:
        _, external = _open_sell_conflicts(
            ec,
            pos.option_code,
            confirmed_terminal_order_ids=confirmed_terminal_order_ids,
        )
    except Exception as exc:  # noqa: BLE001 — 核对失败时宁可不挂新卖单
        _audit_block_once(
            pos,
            f"止损前核对在途卖单失败，保持保护态且禁止重挂：{exc}",
            source_name=source_name,
        )
        pos.status = "closing"
        pos.risk_exit_state = "closing_remaining"
        return False
    if external:
        _audit_block_once(
            pos,
            "检测到非本程序在途卖单，已阻止自动止损以避免超卖："
            + ",".join(external),
            source_name=source_name,
        )
        pos.status = "closing"
        pos.risk_exit_state = "closing_remaining"
        return False
    # 本地可能因重启/恢复丢失 close_order_id，但 Alpaca 仍有 ttt-* 目标卖单。
    # 这些本程序订单交给 atomic_close 逐笔撤销并等待终态；在这里硬阻断会让风险退出
    # 永久卡住。外部卖单仍在上面硬阻断，不能由本程序擅自撤销。

    reason = pos.risk_exit_reason or "风险退出"
    context = dict(pos.risk_exit_context or {})
    journal_context = {
        key: value for key, value in context.items() if key != "trigger_qty"
    }
    bid = context.get("trigger_bid")
    ask = context.get("trigger_ask")
    trigger_quote = (bid, ask) if bid is not None and ask is not None else None
    requested_qty = pos.qty
    pos.status = "closing"
    pos.risk_exit_state = "closing_remaining"
    pos.close_error = None

    try:
        executor = close_executor or atomic_close
        result = executor(
            ec,
            alpaca,
            pos.option_code,
            requested_qty,
            "watchdog:" + reason,
            ticker=pos.ticker,
            parent_order_id=pos.order_id,
            trigger_quote=trigger_quote,
            trigger_context=journal_context,
            confirmed_terminal_order_ids=confirmed_terminal_order_ids,
            confirmed_position_qty=pos.qty,
            confirmed_position_at=pos.last_owned_buy_fill_at,
            wait_s=0.6,
            poll_s=0.1,
        )
    except Exception as exc:  # noqa: BLE001 — 锁存触发，下拍继续，不能丢保护态
        _audit_block_once(
            pos,
            f"止损执行异常，保持保护态等待重试：{exc}",
            source_name=source_name,
        )
        return True

    try:
        filled_qty = int(float(result.get("filled_qty") or 0))
    except (TypeError, ValueError):
        filled_qty = 0
    if result.get("closed") and filled_qty <= 0:
        result = {
            **result,
            "closed": False,
            "error": "止损结果缺少 Alpaca 成交数量，已停止本地结算并等待对账",
            "reconcile_required": True,
        }
    filled_qty = min(max(0, filled_qty), requested_qty)

    # 券商侧已无此仓 → 收尾走人，继续重试只会永远撞同一堵墙。
    #
    # 2026-08-14 实盘（INTC260814P00104000）：撤单竞态里的 1 张部分成交被 429 抛断、
    # 没记账，本地永远多 1 张；此后每拍都拿到「Alpaca 当前无可平仓数量」，
    # 打出 46 条 `trade_watchdog_result` 直到重启。
    #
    # 为什么修在这里而不是放开 `watchdog.py:580` 那条 `continue`：
    #   - 依据是执行器那次**实时** REST 读数，比 `_watchdog_tick` 手上的持仓快照新；
    #   - 执行器刚刚拒绝提交卖单，此刻不可能有本程序的在途平仓单，不会与
    #     `close_run` 抢（`watchdog.py:594` 那条守卫正是为避让它而存在）；
    #   - `order_pricing` 已先用 `confirmed_position_qty` 兜过成交结算窗口
    #     （`POSITION_SETTLE_GRACE_S`），能拿到 0 就不是「刚成交、快照没追上」。
    if filled_qty <= 0 and _broker_reports_flat(result):
        _settle_broker_flat(pos, reason=reason, source_name=source_name)
        return True

    # 盘口 mid 不是成交价；缺 filled_avg_price 时保留未知，不能猜进已实现 PnL。
    exit_px = result.get("fill")
    exit_mid = Decimal(str(exit_px)) if exit_px not in (None, "") else None
    close_order = result.get("order") if isinstance(result.get("order"), dict) else {}
    sell_id = str(close_order.get("id") or "")

    if filled_qty > 0:
        if sell_id and JOURNAL.get(sell_id) is None:
            JOURNAL.submitted(
                sell_id,
                pos.ticker,
                pos.option_code,
                "sell",
                filled_qty,
                reason="watchdog:" + reason,
                origin="system",
                action="auto_close",
                parent_order_id=pos.order_id,
                **journal_context,
            )
            JOURNAL.mark_filled(sell_id, price=exit_mid, qty=filled_qty)
        trade_recorder.record_closed(
            pos.order_id,
            exit_mid,
            filled_qty,
            "watchdog:" + reason,
        )
        remaining = REGISTRY.reduce(pos.option_code, filled_qty, exit_mid)
    else:
        remaining = pos.qty

    if result.get("resting") and sell_id and remaining > 0:
        try:
            current_qty = int(float(
                result.get("current_order_qty")
                or close_order.get("qty")
                or remaining
            ))
        except (TypeError, ValueError):
            current_qty = remaining
        try:
            current_filled = int(float(
                result.get("current_order_filled_qty") or 0
            ))
        except (TypeError, ValueError):
            current_filled = 0
        pos.close_order_id = sell_id
        pos.close_qty = max(1, current_qty)
        pos.close_applied_qty = min(max(0, current_filled), pos.close_qty)
        pos.close_reason = "watchdog:" + reason
        pos.close_submitted_at = datetime.now(timezone.utc)
        pos.close_timeout_policy = "quick"
        pos.cancel_requested = bool(result.get("cancel_pending"))
        pos.status = "closing"
        pos.close_error = result.get("error") or "止损卖单仍在券商途中，等待终态"
    elif remaining <= 0:
        pos.status = "closed"
        pos.close_error = None
        clear_risk_exit(pos)
    else:
        pos.status = "closing"
        pos.close_error = (
            result.get("error")
            or f"止损追价仅成交 {filled_qty}/{requested_qty} 张；剩余仓位继续退出"
        )

    status = (
        "filled" if remaining <= 0
        else "resting" if result.get("resting")
        else "not_filled"
    )
    # 去重：风险退出按拍重试，一个卡住的状态会把同一份内容复读成每秒一条
    # （2026-08-14 INTC 46 条）。隔壁 `_audit_block_once` 早有去重，这条一直没有。
    # 指纹里带 `filled_qty` 与 `sell_id`：**任何真实进展都会改指纹**，部分成交
    # 绝不会被吞掉；被吞的只有一字不差的复读。
    audit_key = "|".join(str(x) for x in (
        status, filled_qty, requested_qty, sell_id, result.get("error"),
        result.get("limit"), result.get("fill"),
    ))
    if pos.watchdog_audit_key == audit_key:
        pos.watchdog_audit_repeats += 1     # 只计数，不落盘
        return True
    suppressed = pos.watchdog_audit_repeats
    pos.watchdog_audit_key = audit_key
    pos.watchdog_audit_repeats = 0

    payload = {
        "order_id": pos.order_id,
        "close_order_id": sell_id or None,
        "ticker": pos.ticker,
        "direction": pos.direction,
        "contract": pos.option_code,
        "side": "sell",
        "qty": requested_qty,
        "filled_qty": filled_qty,
        "limit_price": result.get("limit"),
        "fill_price": result.get("fill"),
        "status": status,
        "reason": reason,
        "error": result.get("error"),
        "source": source_name,
    }
    # 静默去重会让人以为「只失败了一次」—— 吞掉多少必须说出来。
    if suppressed:
        payload["suppressed"] = suppressed
    trade_recorder.audit("trade_watchdog_result", **payload)
    return True
