"""⑤ 订单执行（Alpaca PAPER）—— 只接受带闸门通行证的请求。

README 铁律：
  - 用可成交限价单（marketable limit：买挂 ask，卖挂 bid，可追一档），禁止裸市价单。
  - 平仓原子：撤在途 → 确认 → 发平仓单 → 记 reason（本文件先做入场提交 + 撤单 + 查询，
    原子平仓/ watchdog / 对账为后续 阶段2 续建）。

纯逻辑（定价/订单构造）单测。
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from uuid import uuid4

from app import trade_recorder
from app.broker_pnl import PROGRAM_CLIENT_ORDER_PREFIX

TICK = Decimal("0.01")
POSITION_SETTLE_GRACE_S = 5.0


def normalize_limit_price(value: Decimal) -> Decimal:
    """把所有提交给券商的限价统一到系统的一分 tick。"""
    return Decimal(str(value)).quantize(TICK, rounding=ROUND_HALF_UP)


def reduce_qty(total: int, fraction: Decimal) -> int:
    """⑪ 分批减仓张数：round(total×fraction)，至少 1 张、不超过 total。

    小仓兜底：不足 1 手的比例向上并到 1 手（如 1 张的 1/3 → 卖 1 张 = 全平）。
    total=0 → 0。用于 1/3、减半(0.5) 等按比例减仓。
    """
    if total <= 0:
        return 0
    n = int((Decimal(total) * Decimal(fraction)).to_integral_value(rounding=ROUND_HALF_UP))
    return max(1, min(n, total))


def marketable_limit_price(side: str, bid: Decimal, ask: Decimal, aggressive_ticks: int = 0) -> Decimal:
    """可成交限价：买用 ask（+追价），卖用 bid（−追价）。aggressive_ticks 为追一档档数。"""
    s = side.lower()
    if s == "buy":
        return normalize_limit_price(ask + aggressive_ticks * TICK)
    if s == "sell":
        return normalize_limit_price(bid - aggressive_ticks * TICK)
    raise ValueError(f"未知 side: {side!r}（buy/sell）")


def price_for(side: str, price_type: str, bid: Decimal, ask: Decimal,
              limit_price: Decimal | None = None, aggressive_ticks: int = 1) -> Decimal:
    """⑩ 按价格类型算委托价（仍是限价单，不裸市价，守铁律）。

    价格类型（与买卖方向无关的固定语义）：
      - bid  买一：挂 bid（买入=被动排队，卖出=可成交）
      - ask  卖一：挂 ask（买入=可成交，卖出=被动排队）
      - mid  中间价：挂 bid/ask 中点，按一分 tick 四舍五入（被动，不保证成交）
      - market 市价：向对手方向追 aggressive_ticks 档（买 ask+、卖 bid−），确保吃单但仍是限价
      - limit 限价：用调用方给的 limit_price
    """
    s = side.lower()
    pt = price_type.lower()
    if pt == "bid":
        return normalize_limit_price(bid)
    if pt == "ask":
        return normalize_limit_price(ask)
    if pt == "mid":
        return normalize_limit_price((bid + ask) / 2)
    if pt == "market":
        if s == "buy":
            return normalize_limit_price(ask + aggressive_ticks * TICK)
        if s == "sell":
            return normalize_limit_price(bid - aggressive_ticks * TICK)
        raise ValueError(f"未知 side: {side!r}（buy/sell）")
    if pt == "limit":
        if limit_price is None:
            raise ValueError("price_type=limit 需提供 limit_price")
        return normalize_limit_price(limit_price)
    raise ValueError(f"未知价格类型: {price_type!r}（bid/ask/mid/market/limit）")


#: 每个方向上「吃单」的那一侧盘口（挂到这一侧 = 主动成交）。
_MARKETABLE_SIDE = {"buy": "ask", "sell": "bid"}
_PRICE_TYPES = frozenset({"bid", "ask", "mid", "market", "limit"})


def is_marketable(side: str, price_type: str) -> bool:
    """该价格类型对该方向是「可成交(吃单)」还是「被动(挂队列)」。

    买挂 ask / 卖挂 bid = 吃对手方挂单 → 可成交；反过来就是排进自己这侧的队列等人来吃，
    0DTE 上大概率挂到收盘也不成交（2026-07-25 QQQ 减仓事件即此）。
    market 两个方向都向对手追档 → 恒可成交；limit 是调用方显式指定价位，不替他二次猜测。
    """
    s = side.lower()
    pt = price_type.lower()
    if s not in _MARKETABLE_SIDE:
        raise ValueError(f"未知 side: {side!r}（buy/sell）")
    if pt not in _PRICE_TYPES:
        raise ValueError(f"未知价格类型: {price_type!r}（bid/ask/mid/market/limit）")
    if pt in ("market", "limit"):
        return True
    if pt == "mid":
        return False
    return pt == _MARKETABLE_SIDE[s]


def build_limit_order(
    symbol: str,
    side: str,
    qty: int,
    limit_price: Decimal,
    extended_hours: bool,
):
    """构造 Alpaca 限价单请求（DAY）。

    client_order_id 只放本程序归属前缀，不把开仓/减仓原因等业务信息发送给券商；
    完整动作和原因逐笔保存在我们自己的 ``ttt_order_events``。
    """
    from alpaca.trading.enums import OrderSide, PositionIntent, TimeInForce
    from alpaca.trading.requests import LimitOrderRequest
    order_side = OrderSide.BUY if side.lower() == "buy" else OrderSide.SELL
    normalized_limit = normalize_limit_price(limit_price)
    position_intent = (
        PositionIntent.BUY_TO_OPEN
        if order_side == OrderSide.BUY
        else PositionIntent.SELL_TO_CLOSE
    )
    return LimitOrderRequest(
        symbol=symbol.upper(), qty=qty, side=order_side, time_in_force=TimeInForce.DAY,
        limit_price=float(normalized_limit), extended_hours=extended_hours,
        client_order_id=f"{PROGRAM_CLIENT_ORDER_PREFIX}{uuid4()}",
        position_intent=position_intent,
    )


def _order_status(value) -> str:
    """Normalize SDK enums such as ``OrderStatus.FILLED`` to ``filled``."""
    return str(value or "").rsplit(".", 1)[-1].lower()


def _broker_order_fill(ec, order_id: str, submitted_qty: int) -> tuple[int, Decimal | None, str, dict]:
    """Read one order's broker-confirmed cumulative fill; never infer it from account qty."""
    try:
        order = ec.get_order(order_id) or {}
    except Exception:  # noqa: BLE001 — caller keeps polling/reconciling
        return 0, None, "", {}
    try:
        filled_qty = int(float(order.get("filled_qty") or 0))
    except (TypeError, ValueError):
        filled_qty = 0
    filled_qty = min(max(0, filled_qty), max(0, int(submitted_qty)))
    raw_price = order.get("filled_avg_price")
    try:
        fill_price = Decimal(str(raw_price)) if raw_price not in (None, "") else None
    except (TypeError, ValueError, ArithmeticError):
        fill_price = None
    return filled_qty, fill_price, _order_status(order.get("status")), order


def atomic_close(ec, alpaca, option_code: str, qty: int, reason: str,
                 price_type: str = "bid", limit_price: Decimal | None = None,
                 chase: bool = True, attempts: int = 3, wait_s: float = 1.5,
                 poll_s: float = 0.3, *, ticker: str | None = None,
                 parent_order_id: str | None = None,
                 trigger_quote: tuple[Decimal, Decimal] | None = None,
                 trigger_context: dict | None = None,
                 confirmed_terminal_order_ids: set[str] | None = None,
                 confirmed_position_qty: int | None = None,
                 confirmed_position_at: datetime | None = None) -> dict:
    """原子平仓：撤在途 → 穿价限价卖 + 追单（未成交则撤单、按 spread 更深重挂，有界重试）。

    README 铁律：平仓用可成交限价单（marketable limit），禁裸市价；一个理由必填；止损由本程序执行。
    Layer 1 系统主止损（穿价+追单，修 #3 挂 bid 卡单）：
      watchdog 可把击穿止损线当刻的 ``trigger_quote`` 传入：首手立即挂该 bid，不再
      为定价重复发 REST，也不主动把限价压到 bid 以下；若未成，后续手才重取报价并
      按 `bid − (i/attempts)×spread` 加深。
      常规调用第 i 手（i=1..attempts）挂 `bid − (i/attempts)×spread`——薄票 spread 宽→多穿吃单，
      厚票 spread≈1 档→≈挂 bid（不改厚盘秒成行为）；每手轮询 wait_s 秒未成 → 撤单、重取报价、更深重挂。
      每手前按 position_qty 重算剩余张数（杜绝上一手部分成交后重卖）。全部 attempts 未成 → closed=False。
    单发回退：调用方给 limit_price、或 price_type≠bid（显式被动/指定价）、或 chase=False → 老的一次性挂单，不追。
    ⑪ 支持部分平仓（qty < 持仓）。成交后 fill=真实成交均价（⑧ 落库优先于 mid）。
    """
    from app.order_journal import JOURNAL

    # Alpaca 的 DELETE /orders/{id} 成功只表示“撤单请求已受理”，不表示订单已经
    # canceled。必须逐笔等到券商终态，期间任何一笔撤单失败/超时都禁止提交新的卖单，
    # 否则旧单和新单可能同时成交而超卖。
    initial_position_qty = ec.position_qty(option_code)
    confirmed_terminal_ids = {
        str(order_id)
        for order_id in (confirmed_terminal_order_ids or set())
        if order_id
    }
    broker_open_ids = [
        str(order_id) for order_id in ec.open_order_ids_for(option_code)
    ]
    prior_order_ids = [
        order_id
        for order_id in broker_open_ids
        if order_id not in confirmed_terminal_ids
    ]
    stale_terminal_ids = [
        order_id
        for order_id in broker_open_ids
        if order_id in confirmed_terminal_ids
    ]
    if stale_terminal_ids:
        # 同一流程刚通过 get_order / TradingStream 取得了终态；OPEN 列表可能短暂滞后。
        # 只忽略这些有逐笔终态证据的 ID，绝不放宽其它程序单/外部单的防超卖检查。
        trade_recorder.audit(
            "trade_close_stale_open_snapshot_ignored",
            trade_id=parent_order_id,
            contract=option_code,
            order_ids=stale_terminal_ids,
            reason=reason,
            status="terminal_confirmed",
        )
    cleanup_terminal = {
        "filled", "canceled", "cancelled", "expired", "rejected",
        "done_for_day", "replaced",
    }
    for oid in prior_order_ids:
        try:
            ec.cancel(oid)
            # 订单若已进入本地读模型，先记清“谁发起撤单”；最终 canceled 仍须等
            # TradingStream/REST 由 Alpaca 确认，不能在这里提前改终态。
            JOURNAL.mark_cancel_requested(
                oid,
                cancel_origin="system",
                reason="系统提交平仓前清理同合约旧挂单",
            )
            trade_recorder.audit(
                "trade_close_prior_order_cancelled",
                trade_id=parent_order_id,
                close_order_id=oid,
                contract=option_code,
                reason=reason,
                status="cancel_requested",
            )
        except Exception as exc:  # noqa: BLE001 — 失败时必须停止，不能 best-effort 后继续卖
            # cancel 与订单终态可能竞态：DELETE 报错不等于订单仍在途。逐笔查单若已经
            # 是 Alpaca 终态，可以继续进入下面的终态/成交核对；否则仍硬阻断。
            terminal_status = ""
            try:
                terminal_status = _order_status(
                    (ec.get_order(oid) or {}).get("status")
                )
            except Exception:  # noqa: BLE001 — 原撤单异常作为最终错误即可
                terminal_status = ""
            if terminal_status in cleanup_terminal:
                trade_recorder.audit(
                    "trade_close_prior_order_already_terminal",
                    trade_id=parent_order_id,
                    close_order_id=oid,
                    contract=option_code,
                    reason=reason,
                    broker_status=terminal_status,
                )
                continue
            trade_recorder.audit(
                "trade_close_prior_order_cancel_failed",
                trade_id=parent_order_id,
                close_order_id=oid,
                contract=option_code,
                reason=reason,
                status="unknown",
                error=str(exc),
            )
            return {
                "closed": False,
                "reason": reason,
                "contract": option_code,
                "error": f"旧挂单 {oid} 撤销失败，已禁止提交新平仓单以避免超卖",
                "cleanup_order_id": oid,
            }

    cleanup_had_fill = False
    # 全部撤单请求已经在上一个循环发出；终态等待必须共享一个 deadline。逐单重新
    # 计时会把风险退出最坏延迟放大为 N × wait_s。
    cleanup_deadline = time.monotonic() + max(0.0, wait_s)
    unresolved_cleanup = {
        oid: {"status": "", "order": {}}
        for oid in prior_order_ids
    }
    first_probe = True
    while unresolved_cleanup and (
        first_probe or time.monotonic() < cleanup_deadline
    ):
        for oid in list(unresolved_cleanup):
            try:
                cleanup_order = ec.get_order(oid) or {}
                status = _order_status(cleanup_order.get("status"))
            except Exception:  # noqa: BLE001 — 短暂查询失败继续在有界窗口内确认
                cleanup_order = {}
                status = ""
            unresolved_cleanup[oid] = {
                "status": status,
                "order": cleanup_order,
            }
            if status in cleanup_terminal:
                try:
                    cleanup_filled_qty = int(float(
                        cleanup_order.get("filled_qty") or 0))
                except (TypeError, ValueError):
                    cleanup_filled_qty = 0
                cleanup_had_fill = (
                    cleanup_had_fill
                    or status == "filled"
                    or cleanup_filled_qty > 0
                )
                confirmed_terminal_ids.add(oid)
                unresolved_cleanup.pop(oid, None)
        first_probe = False
        if not unresolved_cleanup or time.monotonic() >= cleanup_deadline:
            break
        remaining_s = max(0.0, cleanup_deadline - time.monotonic())
        time.sleep(min(max(0.0, poll_s), remaining_s))

    if unresolved_cleanup:
        unresolved_ids = list(unresolved_cleanup)
        for oid, snapshot in unresolved_cleanup.items():
            trade_recorder.audit(
                "trade_close_prior_order_cancel_unconfirmed",
                trade_id=parent_order_id,
                close_order_id=oid,
                contract=option_code,
                reason=reason,
                broker_status=snapshot["status"] or "unknown",
            )
        return {
            "closed": False,
            "reason": reason,
            "contract": option_code,
            "error": (
                "旧挂单 "
                + ",".join(unresolved_ids)
                + " 撤销尚未获 Alpaca 终态确认，已禁止重挂"
            ),
            "cleanup_order_id": unresolved_ids[0],
            "cleanup_order_ids": unresolved_ids,
            "cleanup_pending": True,
        }

    post_cleanup_open_ids = (
        [
            str(order_id)
            for order_id in ec.open_order_ids_for(option_code)
        ]
        if prior_order_ids else []
    )
    stale_post_cleanup_ids = [
        order_id
        for order_id in post_cleanup_open_ids
        if order_id in confirmed_terminal_ids
    ]
    if stale_post_cleanup_ids:
        trade_recorder.audit(
            "trade_close_stale_open_snapshot_ignored",
            trade_id=parent_order_id,
            contract=option_code,
            order_ids=stale_post_cleanup_ids,
            reason=reason,
            status="terminal_confirmed_after_cleanup",
        )
    remaining_prior_ids = [
        order_id
        for order_id in post_cleanup_open_ids
        if order_id not in confirmed_terminal_ids
    ]
    if remaining_prior_ids:
        return {
            "closed": False,
            "reason": reason,
            "contract": option_code,
            "error": (
                "清理后 Alpaca 仍报告同合约程序挂单；"
                "已禁止提交新平仓单以避免重复成交"
            ),
            "cleanup_order_ids": remaining_prior_ids,
            "cleanup_pending": True,
        }

    current_qty = (
        ec.position_qty(option_code)
        if prior_order_ids else initial_position_qty
    )
    if cleanup_had_fill:
        # 即使 positions REST 尚未反映刚发生的成交，也不能继续卖；订单终态本身已经
        # 足以证明清理窗口出现了仓位变化，先交给订单结算/对账链处理。
        return {
            "closed": False,
            "reason": reason,
            "contract": option_code,
            "error": (
                "旧挂单在清理期间发生了成交；已停止新平仓单，"
                "等待订单流结算后重试"
            ),
            "broker_position_qty": current_qty,
            "reconcile_required": True,
        }
    if prior_order_ids and current_qty != initial_position_qty:
        # 清理窗口内旧买/卖单发生了成交。此时本地尚未完成该旧单的归因和结算，
        # 先让 TradingStream/watchdog 对账，绝不把账户持仓变化猜成当前平仓成交。
        trade_recorder.audit(
            "trade_close_prior_order_position_changed",
            trade_id=parent_order_id,
            contract=option_code,
            reason=reason,
            before_qty=initial_position_qty,
            after_qty=current_qty,
            status="reconcile_required",
        )
        return {
            "closed": False,
            "reason": reason,
            "contract": option_code,
            "error": (
                "清理旧挂单期间券商持仓数量发生变化；"
                "已停止新平仓单，等待订单流对账后重试"
            ),
            "broker_position_qty": current_qty,
            "reconcile_required": True,
        }
    confirmed_floor = 0
    confirmed_floor_age_s: float | None = None
    if confirmed_position_qty is not None and confirmed_position_at is not None:
        confirmed_at = confirmed_position_at
        if confirmed_at.tzinfo is None:
            confirmed_at = confirmed_at.replace(tzinfo=timezone.utc)
        fill_age_s = (
            datetime.now(timezone.utc) - confirmed_at.astimezone(timezone.utc)
        ).total_seconds()
        if 0.0 <= fill_age_s < POSITION_SETTLE_GRACE_S:
            confirmed_floor = max(0, int(confirmed_position_qty))
            confirmed_floor_age_s = fill_age_s
    if confirmed_floor > current_qty:
        trade_recorder.audit(
            "trade_close_position_snapshot_floor_used",
            trade_id=parent_order_id,
            contract=option_code,
            reason=reason,
            broker_position_qty=current_qty,
            confirmed_position_qty=confirmed_floor,
            owned_fill_age_ms=round((confirmed_floor_age_s or 0.0) * 1000),
            status="position_snapshot_stale",
        )
    effective_position_qty = max(max(0, int(current_qty)), confirmed_floor)
    qty = min(max(0, int(qty)), effective_position_qty)
    if qty <= 0:
        return {
            "closed": False,
            "reason": reason,
            "contract": option_code,
            "error": "Alpaca 当前无可平仓数量，已禁止提交卖单并等待持仓对账",
            "broker_position_qty": current_qty,
            "reconcile_required": True,
        }
    actor = "system" if str(reason).startswith("watchdog:") else "manual"
    action = "reduce" if qty < effective_position_qty else "close"
    event_origin = "system" if actor == "system" else "user"
    event_action = "auto_close" if actor == "system" else action
    event_ticker = ticker or option_code.split("2", 1)[0]

    # 单发回退：显式限价 / 非 bid 价格类型 / 关闭追单 —— 尊重调用方意图（不追价、被动排队）。
    # 铁律：submit ≠ 成交。被动卖一(ask)挂单可能长时间不成交；submit 后必须查券商持仓确认
    # 成交，才可报 closed。窗口内未确认成交 → resting（挂单在券商排队，非 closed、非 error），
    # 由 watchdog 异步确认最终成交/失败（修「谎报已成交」严重资金 BUG）。
    if limit_price is not None or price_type != "bid" or not chase:
        q = alpaca.option_quote(option_code, fresh=True)  # 定价必须用真实最新盘口，不吃缓存
        if q is None:
            trade_recorder.audit(
                "trade_close_order_status",
                trade_id=parent_order_id,
                contract=option_code,
                qty=qty,
                price_type=price_type,
                status="not_submitted",
                reason=reason,
                error="无报价，无法平仓",
            )
            return {"closed": False, "reason": reason, "error": "无报价，无法平仓"}
        bid, ask = q
        limit = price_for("sell", price_type, bid, ask, limit_price=limit_price)
        order = ec.submit(build_limit_order(
            option_code, "sell", qty, limit, extended_hours=False))
        JOURNAL.submitted(
            str(order["id"]), event_ticker, option_code, "sell", qty,
            reason=reason, price_type=price_type, limit_price=limit,
            origin=event_origin, action=event_action,
            client_order_id=order.get("client_order_id"),
            parent_order_id=parent_order_id,
            **(trigger_context or {}),
        )
        trade_recorder.audit(
            "trade_close_order_submitted",
            trade_id=parent_order_id,
            close_order_id=order.get("id"),
            contract=option_code,
            side="sell",
            qty=qty,
            limit_price=limit,
            price_type=price_type,
            attempt=1,
            max_attempts=1,
            reason=reason,
            status="submitted",
        )
        fill_px = None
        filled_qty = 0
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            time.sleep(poll_s)
            filled_qty, fill_px, status, _ = _broker_order_fill(
                ec, str(order["id"]), qty)
            if filled_qty >= qty or status in {
                "canceled", "cancelled", "expired", "rejected", "done_for_day"
            }:
                break
        mid = str((bid + ask) / 2)
        if filled_qty >= qty:
            JOURNAL.mark_filled(
                str(order["id"]), price=fill_px, qty=filled_qty)
            trade_recorder.audit(
                "trade_close_order_status",
                trade_id=parent_order_id,
                close_order_id=order.get("id"),
                contract=option_code,
                side="sell",
                qty=qty,
                limit_price=limit,
                fill_price=fill_px,
                status="filled",
                reason=reason,
            )
            return {"closed": True, "reason": reason, "contract": option_code, "limit": str(limit),
                    "mid": mid, "fill": None if fill_px is None else str(fill_px),
                    "filled_qty": filled_qty, "order": order, "qty": qty}
        # passive：这单挂在自己这侧队列里等人来吃（如卖出挂 ask），排到收盘也可能不成交。
        # 上层据此如实告知用户，不许让他误以为已经卖掉了（2026-07-25 QQQ 减仓事件）。
        trade_recorder.audit(
            "trade_close_order_status",
            trade_id=parent_order_id,
            close_order_id=order.get("id"),
            contract=option_code,
            side="sell",
            qty=qty,
            limit_price=limit,
            status="resting",
            reason=reason,
        )
        return {"closed": False, "resting": True, "reason": reason, "contract": option_code,
                "limit": str(limit), "mid": mid, "order": order, "qty": qty,
                "filled_qty": filled_qty,
                "current_order_qty": qty,
                "current_order_filled_qty": filled_qty,
                "passive": not is_marketable("sell", price_type)}

    # 穿价 + 追单
    first_mid = None
    order = None
    used = 0
    total_filled = 0
    observed: dict[str, tuple[int, Decimal | None]] = {}
    submitted_orders: list[dict] = []

    def observe(order_id: str, submitted_qty: int) -> tuple[int, Decimal | None, str]:
        """记录每笔订单的累计量/累计均价；允许均价在同数量的后续帧补齐。"""
        nonlocal total_filled
        filled, avg, status, _ = _broker_order_fill(ec, order_id, submitted_qty)
        old_qty, old_avg = observed.get(order_id, (0, None))
        if filled >= old_qty:
            observed[order_id] = (filled, avg if avg is not None else old_avg)
            total_filled = sum(cumulative for cumulative, _ in observed.values())
        return filled, avg, status

    def weighted_fill_price() -> Decimal | None:
        """所有已成交张数都有券商均价时才返回跨订单加权价；绝不把未知价按 0 摊薄。"""
        if total_filled <= 0:
            return None
        priced = [(filled, avg) for filled, avg in observed.values() if filled > 0]
        if any(avg is None for _, avg in priced):
            return None
        notional = sum(
            (avg or Decimal("0")) * Decimal(filled)
            for filled, avg in priced
        )
        return notional / Decimal(total_filled)

    for i in range(attempts):
        used = i + 1
        remaining = qty - total_filled
        if remaining <= 0:
            break
        use_trigger_quote = i == 0 and trigger_quote is not None
        q = (
            trigger_quote
            if use_trigger_quote
            else alpaca.option_quote(option_code, fresh=True)
        )
        if q is None:
            trade_recorder.audit(
                "trade_close_order_status",
                trade_id=parent_order_id,
                contract=option_code,
                side="sell",
                qty=remaining,
                attempt=used,
                max_attempts=attempts,
                status="quote_unavailable",
                reason=reason,
            )
            break
        bid, ask = q
        if first_mid is None:
            first_mid = (bid + ask) / 2
        spread = max(ask - bid, TICK)
        cross = (
            Decimal("0")
            if use_trigger_quote
            else (
                spread * Decimal(i + 1) / Decimal(attempts)
            ).quantize(TICK, rounding=ROUND_HALF_UP)
        )
        limit = max(bid - cross, TICK).quantize(TICK)
        order = ec.submit(build_limit_order(
            option_code, "sell", remaining, limit, extended_hours=False))
        submitted_orders.append(order)
        JOURNAL.submitted(
            str(order["id"]), event_ticker, option_code, "sell", remaining,
            reason=reason, price_type="bid_chase", limit_price=limit,
            origin=event_origin, action=event_action,
            client_order_id=order.get("client_order_id"),
            parent_order_id=parent_order_id,
            **((trigger_context or {}) if i == 0 else {}),
        )
        trade_recorder.audit(
            "trade_close_order_submitted",
            trade_id=parent_order_id,
            close_order_id=order.get("id"),
            contract=option_code,
            side="sell",
            qty=remaining,
            limit_price=limit,
            price_type="bid_chase",
            attempt=used,
            max_attempts=attempts,
            quote_bid=bid,
            quote_ask=ask,
            quote_source="trigger" if use_trigger_quote else "fresh",
            reason=reason,
            status="submitted",
        )
        deadline = time.monotonic() + wait_s
        order_filled = 0
        order_fill_px = None
        status = ""
        while time.monotonic() < deadline:
            time.sleep(poll_s)
            order_filled, order_fill_px, status = observe(
                str(order["id"]), remaining)
            if total_filled >= qty or status in {
                "canceled", "cancelled", "expired", "rejected", "done_for_day"
            }:
                break
        if total_filled >= qty:
            weighted_fill = weighted_fill_price()
            JOURNAL.mark_filled(
                str(order["id"]), price=order_fill_px, qty=order_filled)
            trade_recorder.audit(
                "trade_close_order_status",
                trade_id=parent_order_id,
                close_order_id=order.get("id"),
                contract=option_code,
                side="sell",
                qty=qty,
                limit_price=limit,
                fill_price=None if weighted_fill is None else str(weighted_fill),
                attempt=used,
                status="filled",
                reason=reason,
            )
            return {"closed": True, "reason": reason, "contract": option_code,
                    "limit": str(limit), "mid": str(first_mid),
                    "fill": None if weighted_fill is None else str(weighted_fill),
                    "filled_qty": total_filled, "attempts": used,
                    "order": order, "orders": submitted_orders, "qty": qty}
        cancel_failed = False
        try:
            ec.cancel(order["id"])
            JOURNAL.mark_cancel_requested(
                str(order["id"]),
                cancel_origin="system",
                reason="系统追价换单，撤销上一档平仓挂单",
            )
            trade_recorder.audit(
                "trade_close_order_repriced",
                trade_id=parent_order_id,
                close_order_id=order.get("id"),
                contract=option_code,
                side="sell",
                qty=remaining,
                limit_price=limit,
                attempt=used,
                status="cancel_requested",
                reason=reason,
            )
        except Exception:  # noqa: BLE001
            cancel_failed = True
            trade_recorder.audit(
                "trade_close_order_reprice_failed",
                trade_id=parent_order_id,
                close_order_id=order.get("id"),
                contract=option_code,
                side="sell",
                qty=remaining,
                attempt=used,
                status="unknown",
                reason=reason,
            )
        # 撤单与成交可能竞态。必须等旧单进入终态后才允许重挂下一手；否则旧单仍活跃
        # 时新单也可能成交，造成超卖。只认本订单 filled_qty，不拿账户持仓变化冒充成交。
        cancel_terminal = False
        cancel_deadline = time.monotonic() + wait_s
        while not cancel_failed and time.monotonic() < cancel_deadline:
            order_filled, order_fill_px, status = observe(
                str(order["id"]), remaining)
            if status in {
                "filled", "canceled", "cancelled", "expired", "rejected", "done_for_day"
            }:
                cancel_terminal = True
                break
            time.sleep(poll_s)
        if order_filled > 0:
            JOURNAL.mark_filled(
                str(order["id"]), price=order_fill_px, qty=order_filled)
        if cancel_failed or not cancel_terminal:
            weighted_fill = weighted_fill_price()
            return {
                "closed": total_filled >= qty,
                "resting": total_filled < qty,
                "cancel_pending": not cancel_failed,
                "reason": reason,
                "contract": option_code,
                "error": (
                    "追价旧单撤销尚未确认，禁止重挂以避免超卖"
                    if not cancel_failed else
                    "追价旧单撤销失败，禁止重挂以避免超卖"
                ),
                "attempts": used,
                "filled_qty": total_filled,
                "current_order_qty": remaining,
                "current_order_filled_qty": order_filled,
                "fill": None if weighted_fill is None else str(weighted_fill),
                "mid": str(first_mid) if first_mid is not None else None,
                "limit": str(limit),
                "order": order,
                "orders": submitted_orders,
                "remaining": max(0, qty - total_filled),
            }

    weighted_fill = weighted_fill_price()
    if total_filled >= qty:
        trade_recorder.audit(
            "trade_close_order_status",
            trade_id=parent_order_id,
            close_order_id=order.get("id") if isinstance(order, dict) else None,
            contract=option_code,
            side="sell",
            qty=qty,
            fill_price=None if weighted_fill is None else str(weighted_fill),
            attempt=used,
            status="filled_after_cancel",
            reason=reason,
        )
        return {"closed": True, "reason": reason, "contract": option_code,
                "mid": str(first_mid) if first_mid is not None else None,
                "fill": None if weighted_fill is None else str(weighted_fill),
                "filled_qty": total_filled, "attempts": used,
                "order": order, "orders": submitted_orders, "qty": qty}
    remaining = max(0, qty - total_filled)
    trade_recorder.audit(
        "trade_close_order_status",
        trade_id=parent_order_id,
        close_order_id=order.get("id") if isinstance(order, dict) else None,
        contract=option_code,
        side="sell",
        qty=qty,
        attempt=used,
        status="not_filled",
        reason=reason,
        filled_qty=total_filled,
        fill_price=None if weighted_fill is None else str(weighted_fill),
        remaining=remaining,
    )
    return {"closed": False, "reason": reason, "contract": option_code,
            "error": f"穿价追单 {used} 手未成交", "attempts": used,
            "filled_qty": total_filled,
            "fill": None if weighted_fill is None else str(weighted_fill),
            "mid": str(first_mid) if first_mid is not None else None,
            "order": order, "orders": submitted_orders, "remaining": remaining}
