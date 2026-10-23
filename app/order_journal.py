"""订单事件日志与券商订单读模型。 

watchdog / 平仓 / 对账都只作用于此登记，**绝不触碰账户里非本系统的既有持仓**。
状态机：pending(下单未成交) → open(已成交持仓) → closing → closed / external_closed。
（阶段0 内存态；⑧ 落 Supabase trades 为后续。）
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
import re
import threading

from app.broker_pnl import PROGRAM_CLIENT_ORDER_PREFIX, sibling_program_of
from app.order_event import OrderEvent
from app.core.market_session import order_session_window

_HUNDRED = Decimal("100")
_OPTION_SYMBOL_RE = re.compile(r"^([A-Z]+)\d{6}[CP]\d{8}$")
_BROKER_PENDING = {
    "accepted", "accepted_for_bidding", "calculated", "held", "new",
    "partially_filled", "pending_cancel", "pending_new", "pending_replace",
    "stopped",
}
_BROKER_CANCELLED = {
    "canceled", "cancelled", "done_for_day", "expired", "rejected", "replaced",
    "suspended",
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class OrderJournal:
    """⑦ 订单事件日志（内存态、当日）：下单/成交/撤单三态，喂前端订单流面板。

    阶段0 内存态、进程重启即清（每日重启即当日）。落 Supabase 为后续。
    """
    def __init__(self, *, persist: bool = False) -> None:
        self._events: dict[str, OrderEvent] = {}
        self._persist_enabled = persist
        self._apply_lock = threading.RLock()
        self._revision = 0

    @property
    def revision(self) -> int:
        with self._apply_lock:
            return self._revision

    def _bump_revision_locked(self) -> None:
        self._revision += 1

    def _persist_event(self, ev: OrderEvent | None) -> None:
        """全局真实订单日志写穿数据库；测试/临时 Journal 默认不触库。

        broker-only 外部订单的 ``counts_toward_risk`` 为 False，不写入本程序订单表。
        新版带 ttt client id 的本程序订单即使由 REST/WS 找回，也会标 True 后补写。
        """
        with self._apply_lock:
            if not self._persist_enabled or ev is None or not ev.counts_toward_risk:
                return
            payload = ev.to_dict()
        from app import trade_recorder
        trade_recorder.record_order_event(payload)

    def persist(self, order_id: str) -> None:
        """显式持久化一笔；供 TradingStream 放到线程池，避免阻塞 WS 事件循环。"""
        with self._apply_lock:
            ev = self._events.get(order_id)
        self._persist_event(ev)

    def submitted(self, order_id: str, ticker: str, option_code: str, side: str,
                  qty: int, price: Decimal | None = None,
                  ts: datetime | None = None, reason: str | None = None,
                  price_type: str | None = None, limit_price: Decimal | None = None,
                  origin: str | None = None, action: str | None = None,
                  client_order_id: str | None = None,
                  parent_order_id: str | None = None,
                  triggered_at: datetime | None = None,
                  trigger_bid: Decimal | None = None,
                  trigger_ask: Decimal | None = None,
                  trigger_value: Decimal | None = None,
                  trigger_loss: Decimal | None = None,
                  stop_mode: str | None = None,
                  stop_threshold: Decimal | None = None,
                  entry_value: Decimal | None = None) -> OrderEvent:
        t = ts or _utcnow()
        inferred_origin, inferred_action = _infer_local_semantics(side, reason)
        ev = OrderEvent(order_id=order_id, ticker=ticker.upper(), option_code=option_code,
                        side=side, qty=qty, status="pending", price=price, reason=reason,
                        price_type=price_type, limit_price=limit_price, ts=t, updated_ts=t,
                        origin=origin or inferred_origin, action=action or inferred_action,
                        client_order_id=client_order_id,
                        parent_order_id=parent_order_id,
                        triggered_at=triggered_at,
                        trigger_bid=trigger_bid,
                        trigger_ask=trigger_ask,
                        trigger_value=trigger_value,
                        trigger_loss=trigger_loss,
                        stop_mode=stop_mode,
                        stop_threshold=stop_threshold,
                        entry_value=entry_value)
        with self._apply_lock:
            before = self._events.get(order_id)
            self._events[order_id] = ev
            if before is None or before.to_dict() != ev.to_dict():
                self._bump_revision_locked()
        self._persist_event(ev)
        return ev

    def get(self, order_id: str) -> OrderEvent | None:
        """Return one event without exposing the mutable journal mapping."""
        with self._apply_lock:
            return self._events.get(order_id)

    def claim_terminal_audit(self, order_id: str) -> bool:
        """认领终态审计权：同一订单的 `trade_cancel_confirmed` 只允许写一次。

        终态迁移令牌（`mark_cancelled` 返回值）解决的是「谁记账」，但抢先把订单
        推进终态的可能是**只同步状态、不写审计**的路径——2026-08-01 MOCK 实测
        8 笔撤单有 6 笔因此整条审计丢失。所以审计的幂等锚点必须独立于状态迁移：
        谁先到谁写，且只写一次，与令牌胜负无关。
        """
        with self._apply_lock:
            ev = self._events.get(order_id)
            if ev is None:
                return True  # 不在 JOURNAL 里，无从去重，照写不误
            if ev.terminal_audited:
                return False
            ev.terminal_audited = True
            return True

    def settle_forensics(self, order_id: str) -> dict:
        """结算令牌落败时的取证快照：谁把这单推进了终态。

        `fail_reason` 的形态足以区分写入者——带「Alpaca 已确认取消」后缀说明调用方
        传了 broker_status 却没传 fail_reason，带「查无此订单」则来自 404 清理路径。
        """
        with self._apply_lock:
            ev = self._events.get(order_id)
            if ev is None:
                return {"journal": "absent"}
            return {
                "journal_status": ev.status,
                "journal_broker_status": ev.broker_status,
                "journal_cancel_origin": ev.cancel_origin,
                "journal_cancel_cause": ev.cancel_cause,
                "journal_fail_reason": ev.fail_reason,
            }

    def _transition(self, order_id: str, status: str) -> bool:
        ev = self._events.get(order_id)
        if ev is not None and ev.status == "pending":
            ev.status = status
            return True
        return False

    def mark_filled(
        self,
        order_id: str,
        price: Decimal | None = None,
        qty: int | None = None,
    ) -> bool:
        """成交。price 给出时一并把委托估价换成**券商真实成交均价**（面板显示真值）。

        返回**终态结算令牌**：仅当本次调用真正完成 pending→filled 迁移时为 True。
        同一订单的终态可能被前台 cancel()、watchdog 巡检同时观察到，只有拿到
        令牌的一方负责扣仓与审计，否则会重复落库、重复扣减持仓。
        """
        changed = False
        settled = False
        with self._apply_lock:
            ev = self._events.get(order_id)
            if ev is not None and ev.status in {"pending", "filled"}:
                if price is not None and ev.price != price:
                    ev.price = price
                    changed = True
                if qty is not None and qty > 0 and ev.qty != qty:
                    ev.qty = qty
                    changed = True
                settled = self._transition(order_id, "filled")
                changed = settled or changed
                if changed:
                    ev.updated_ts = _utcnow()
                    self._bump_revision_locked()
        if changed:
            self._persist_event(ev)
        return settled

    def mark_cancel_requested(
        self,
        order_id: str,
        *,
        cancel_origin: str,
        cancel_cause: str | None = None,
        reason: str | None = None,
    ) -> None:
        """记录撤单请求的发起者，但在 Alpaca 确认终态前仍保持 pending。"""
        changed = False
        with self._apply_lock:
            ev = self._events.get(order_id)
            if ev is None or ev.status != "pending":
                return
            if ev.cancel_origin != cancel_origin:
                ev.cancel_origin = cancel_origin
                changed = True
            if cancel_cause is not None and ev.cancel_cause != cancel_cause:
                ev.cancel_cause = cancel_cause
                changed = True
            if reason and ev.fail_reason != reason:
                ev.fail_reason = reason
                changed = True
            if changed:
                ev.updated_ts = _utcnow()
                self._bump_revision_locked()
        if changed:
            self._persist_event(ev)

    def clear_cancel_requested(
        self,
        order_id: str,
        *,
        cancel_origin: str,
        cancel_cause: str | None = None,
        reason: str | None = None,
    ) -> None:
        """券商撤单调用同步失败时，回滚尚未进入终态的本地撤单意图。

        若 TradingStream 已抢先写入终态，则状态不再是 pending，本方法不会抹掉已经
        被券商确认的撤单来源。
        """
        changed = False
        with self._apply_lock:
            ev = self._events.get(order_id)
            if ev is None or ev.status != "pending":
                return
            if ev.cancel_origin == cancel_origin:
                ev.cancel_origin = None
                changed = True
            if cancel_cause is not None and ev.cancel_cause == cancel_cause:
                ev.cancel_cause = None
                changed = True
            if reason is not None and ev.fail_reason == reason:
                ev.fail_reason = None
                changed = True
            if changed:
                ev.updated_ts = _utcnow()
                self._bump_revision_locked()
        if changed:
            self._persist_event(ev)

    def mark_cancelled(
        self,
        order_id: str,
        fail_reason: str | None = None,
        *,
        cancel_origin: str | None = None,
        cancel_cause: str | None = None,
        broker_status: str | None = None,
    ) -> bool:
        """撤单/失败。fail_reason 说清**为什么没成交**（超时自动撤 / 手动撤 / 券商拒）。

        返回**终态结算令牌**，语义同 :meth:`mark_filled`。
        """
        changed = False
        settled = False
        with self._apply_lock:
            ev = self._events.get(order_id)
            if ev is not None and ev.status == "pending":
                before = ev.to_dict()
                if cancel_origin is not None:
                    ev.cancel_origin = cancel_origin
                if cancel_cause is not None:
                    ev.cancel_cause = cancel_cause
                if broker_status:
                    ev.broker_status = broker_status
                if fail_reason is not None:
                    ev.fail_reason = fail_reason
                if broker_status:
                    ev.fail_reason = _terminal_reason(ev, broker_status)
                settled = self._transition(order_id, "cancelled")
                changed = ev.to_dict() != before
                if changed:
                    ev.updated_ts = _utcnow()
                    self._bump_revision_locked()
        if changed:
            self._persist_event(ev)
        return settled

    def today(self, now: datetime | None = None) -> dict:
        """当日订单，按 status 分三组、每组按末次迁移时间倒序（最新在前）。"""
        ref = (now or _utcnow()).date()
        return self._grouped(lambda ev: ev.ts.date() == ref)

    def current_session(
        self,
        now: datetime | None = None,
        *,
        compact: bool = True,
    ) -> dict:
        """当前 ET 自然日终态订单 + 全部跨日未终态订单。

        终态以提交时间或最后更新时间落在 ET 当天为准。pending 永不受日期过滤，
        避免产生无法发现和撤销的幽灵单。
        """
        window = order_session_window(now)
        start = window["session_start"]
        end = window["session_end"]

        def include(ev: OrderEvent) -> bool:
            if ev.status == "pending":
                return True
            submitted = _as_utc(ev.ts)
            updated = _as_utc(ev.updated_ts)
            return (
                submitted is not None and start <= submitted < end
            ) or (
                updated is not None and start <= updated < end
            )

        return self._grouped(include, compact=compact)

    def recent(self, _hours: float | None = None, now: datetime | None = None) -> dict:
        """兼容旧调用；生产语义已改为当前 ET 自然日，不再使用小时滑动窗口。"""
        return self.current_session(now)

    def pending_order_ids(self) -> set[str]:
        """全部在途订单 ID；不能用 ``today()``，跨日挂单也必须继续同步。"""
        with self._apply_lock:
            return {ev.order_id for ev in self._events.values() if ev.status == "pending"}

    def pending_program_sells_for(
        self,
        option_code: str,
        *,
        parent_order_id: str | None = None,
    ) -> list[dict]:
        """返回同合约仍在途的本程序卖单快照，供重启后恢复 close_order_id。

        订单归属仍只认 Alpaca ``client_order_id=ttt-*``；若给出开仓生命周期 ID，
        还必须精确匹配 parent_order_id，不能仅凭合约相同猜关联。
        """
        code = str(option_code or "").upper()
        parent = str(parent_order_id or "")
        with self._apply_lock:
            return [
                ev.to_dict()
                for ev in self._events.values()
                if ev.status == "pending"
                and ev.side == "sell"
                and ev.option_code.upper() == code
                and str(ev.client_order_id or "").startswith(
                    PROGRAM_CLIENT_ORDER_PREFIX
                )
                and (
                    not parent
                    or str(ev.parent_order_id or "") == parent
                )
            ]

    def tracking_order_ids(self) -> set[str]:
        """需要继续消费券商更新的订单。

        除 pending 外，还包含“TradingStream 已先写成 filled、但成交尚未应用到本地持仓”
        的加仓订单，消除 WS 与 watchdog 之间的竞态。
        """
        with self._apply_lock:
            return {
                ev.order_id
                for ev in self._events.values()
                if ev.status == "pending"
                or (
                    ev.action == "add"
                    and ev.status == "filled"
                    and ev.applied_qty < ev.qty
                )
            }

    def mark_applied(self, order_id: str, cumulative_qty: int) -> None:
        """记录某订单已有多少累计成交量被应用到本地持仓（幂等）。"""
        with self._apply_lock:
            ev = self._events.get(order_id)
            if ev is not None:
                qty = max(ev.applied_qty, max(0, int(cumulative_qty)))
                ev.applied_qty = qty
                if ev.price is not None:
                    ev.applied_notional = ev.price * Decimal(qty)

    def claim_fill_delta(
        self,
        order_id: str,
        cumulative_qty: int,
        cumulative_avg_price: Decimal,
    ) -> tuple[int, Decimal | None]:
        """原子认领尚未应用的累计成交，并返回新增量及其真实增量均价。

        同步 submit 响应与 TradingStream 可能同时看见同一终态；先认领者负责更新持仓，
        后到者得到 ``(0, None)``。累计均价可能随部分成交变化，因此同时保存已应用名义额，
        不能把新的累计均价直接当作增量价格。
        """
        qty = max(0, int(cumulative_qty))
        price = Decimal(str(cumulative_avg_price))
        with self._apply_lock:
            ev = self._events.get(order_id)
            if ev is None or qty <= ev.applied_qty:
                return 0, None
            cumulative_notional = price * Decimal(qty)
            delta_qty = qty - ev.applied_qty
            delta_notional = cumulative_notional - ev.applied_notional
            delta_price = delta_notional / Decimal(delta_qty)
            ev.applied_qty = qty
            ev.applied_notional = cumulative_notional
            return delta_qty, delta_price

    def _grouped(self, include, *, compact: bool = False) -> dict:
        with self._apply_lock:
            groups: dict[str, list[dict]] = {"pending": [], "filled": [], "cancelled": []}
            for ev in self._events.values():
                if not include(ev):
                    continue
                if ev.status in groups:
                    groups[ev.status].append(ev)
            return {
                k: [
                    (e.to_wire_dict() if compact else e.to_dict())
                    for e in sorted(v, key=lambda e: e.updated_ts, reverse=True)
                ]
                for k, v in groups.items()
            }

    def upsert_broker_orders(self, orders, *, persist: bool = True) -> dict:
        order_list = list(orders)
        with self._apply_lock:
            result = self._upsert_broker_orders_locked(order_list, persist=False)
        if persist:
            # 数据库 I/O 必须在锁外；否则一次慢写会同时堵住 WS 合并和前端快照读取。
            for order in order_list:
                order_id = _broker_text(_broker_field(order, "id"))
                if order_id:
                    self.persist(order_id)
        return result

    def _upsert_broker_orders_locked(self, orders, *, persist: bool = True) -> dict:
        """把 Alpaca REST 基线或 TradingStream 增量合并进订单读模型。

        这里只更新内存展示/在途跟踪，不把账户里的外部订单自动登记成受 watchdog 管理的
        ``ManagedPosition``。同一订单以券商 ``updated_at`` 判新旧，避免重连后的 REST
        快照覆盖刚收到的 WS 终态。

        归属三分（共用同一个 Alpaca 账户）：
          - ``ttt-``：本程序单，全量收，``counts_toward_risk=True``（计入⑨、落库）；
          - 无前缀：手动/外部单，**照收**（前端标「外部程序」），但不计风控、不落库；
          - ``sos-`` 等兄弟系统前缀：**整条丢弃**，见 ``SIBLING_PROGRAM_ORDER_PREFIXES``。
        """
        inserted = updated = skipped = foreign = 0
        batch_changed = False
        for order in orders:
            try:
                order_id = _broker_text(_broker_field(order, "id"))
                symbol = (_broker_text(_broker_field(order, "symbol")) or "").upper()
                side = (_broker_text(_broker_field(order, "side")) or "").lower()
                raw_status = (_broker_text(_broker_field(order, "status")) or "").lower()
                client_order_id = _broker_text(_broker_field(order, "client_order_id"))
                # 兄弟系统（sos-）的单在同一个账户、同一条 TradingStream 上流过来。
                # 本读模型收了它，它就会出现在 0DTE 订单流的「未成交」组里——而那一组
                # 每行都带撤单按钮，等于把它的止盈保护摆在手边一键可撤（2026-08-07 现场）。
                # SOS 已于 2026-08-15 移出本项目，但账户里可能还有残留的 `sos-` 挂单，
                # 且已无 SOS 面板可管，故这条丢弃规则保留。手动/外部单不在此列，那个该看见。
                if sibling_program_of(client_order_id) is not None:
                    foreign += 1
                    continue
                status = _journal_status(raw_status)
                filled_qty = _broker_int(_broker_field(order, "filled_qty"))
                if status == "cancelled" and filled_qty > 0:
                    # Alpaca 对“部分成交后撤余量”给出的终态仍是 canceled；订单流必须保留
                    # 实际成交部分，不能把整笔显示成未成交。
                    status = "filled"
                if not order_id or not symbol or status is None:
                    skipped += 1
                    continue

                submitted_at = (
                    _as_utc(_broker_field(order, "submitted_at"))
                    or _as_utc(_broker_field(order, "created_at"))
                    or _as_utc(_broker_field(order, "timestamp"))
                    or _utcnow()
                )
                updated_at = (
                    _as_utc(_broker_field(order, "updated_at"))
                    or _as_utc(_broker_field(order, "timestamp"))
                    or submitted_at
                )
                current = self._events.get(order_id)
                before_payload = current.to_dict() if current is not None else None
                # 判帧新旧只能拿**券商钟比券商钟**。此处原本拿 current.updated_ts
                # （本地钟）去比，跨机器时钟偏差会让判定失真；而且下面还会用券商钟
                # 反向覆盖 updated_ts，把「提交→成交」的耗时抹成 0（2026-08-03）。
                if (
                    current is not None
                    and current.broker_updated_ts is not None
                    and current.broker_updated_ts > updated_at
                ):
                    terminal_enrichment = (
                        current.status == status
                        or current.status == "pending"
                        or (status == "filled" and filled_qty > 0)
                    )
                    if not terminal_enrichment:
                        skipped += 1
                        continue

                qty = (
                    filled_qty if status == "filled" and filled_qty > 0
                    else _broker_int(_broker_field(order, "qty"))
                )
                limit_price = _as_dec(_broker_field(order, "limit_price"))
                filled_price = _as_dec(_broker_field(order, "filled_avg_price"))
                order_type = (_broker_text(
                    _broker_field(order, "order_type", _broker_field(order, "type"))
                ) or "").lower() or None
                ticker = _ticker_for_symbol(symbol)
                price = filled_price if status == "filled" else (
                    current.price if current is not None else None
                )
                fail_reason = None
                parsed_origin, parsed_action, parsed_cancel_origin = _client_order_semantics(
                    client_order_id, raw_status)
                if status == "cancelled":
                    if current is not None:
                        probe = current
                        if probe.cancel_origin is None:
                            probe.cancel_origin = parsed_cancel_origin
                        probe.broker_status = raw_status
                    else:
                        probe = OrderEvent(
                            order_id=order_id, ticker=ticker, option_code=symbol,
                            side=side, qty=qty, origin=parsed_origin, action=parsed_action,
                            cancel_origin=parsed_cancel_origin, broker_status=raw_status,
                            client_order_id=client_order_id,
                        )
                    fail_reason = _terminal_reason(probe, raw_status)

                if current is None:
                    broker_is_program = str(client_order_id or "").lower().startswith("ttt-")
                    inserted_event = OrderEvent(
                        order_id=order_id,
                        ticker=ticker,
                        option_code=symbol,
                        side=side,
                        qty=qty,
                        status=status,
                        price=price,
                        ts=submitted_at,
                        updated_ts=updated_at,
                        price_type=order_type,
                        limit_price=limit_price,
                        fail_reason=fail_reason,
                        # 本地从未登记过（重启找回/外部单）：两列都用券商钟，
                        # 同源相减仍是有意义的耗时。
                        broker_updated_ts=updated_at,
                        counts_toward_risk=broker_is_program,
                        origin=parsed_origin,
                        action=parsed_action,
                        cancel_origin=parsed_cancel_origin,
                        broker_status=raw_status,
                        client_order_id=client_order_id,
                    )
                    self._events[order_id] = inserted_event
                    if persist:
                        self._persist_event(inserted_event)
                    inserted += 1
                    batch_changed = True
                    continue

                current.ticker = ticker
                current.option_code = symbol
                current.side = side or current.side
                current.qty = qty or current.qty
                current.status = status
                current.broker_updated_ts = updated_at
                current.price_type = order_type or current.price_type
                current.limit_price = limit_price if limit_price is not None else current.limit_price
                current.price = price
                current.fail_reason = (
                    fail_reason if fail_reason is not None else current.fail_reason
                )
                current.origin = current.origin if current.origin != "unknown" else parsed_origin
                current.action = current.action if current.action != "unknown" else parsed_action
                current.cancel_origin = current.cancel_origin or parsed_cancel_origin
                current.broker_status = raw_status
                current.client_order_id = client_order_id or current.client_order_id
                # `updated_ts` 只在**确有变化**时推进，且盖的是本地钟（与 `ts` 同源），
                # 这样 `updated_ts - ts` 恒等于我们观察到这次变化所花的真实时间。
                # 必须在比对之后再赋值：`updated_ts` 本身在 to_dict() 里，先改就恒不相等。
                changed = current.to_dict() != before_payload
                if changed:
                    current.updated_ts = _utcnow()
                    batch_changed = True
                if persist:
                    self._persist_event(current)
                updated += 1
            except Exception:  # noqa: BLE001 — 单笔券商数据异常不能阻断其余订单
                skipped += 1
        if batch_changed:
            self._bump_revision_locked()
        return {"inserted": inserted, "updated": updated, "skipped": skipped,
                "foreign": foreign}

    def trade_count_today(self, now: datetime | None = None) -> int:
        """⑨ 今日交易次数 = 当日已成交的买入（入场）单数。"""
        ref = (now or _utcnow()).date()
        with self._apply_lock:
            return sum(
                1 for event in self._events.values()
                if event.ts.date() == ref
                and event.status == "filled"
                and event.side == "buy"
                and event.counts_toward_risk
            )

    def clear(self) -> None:
        with self._apply_lock:
            changed = bool(self._events)
            self._events.clear()
            if changed:
                self._bump_revision_locked()

    def rebuild_from_trades(self, rows) -> None:
        """进程重启后从 ttt_trades 行重建当日订单流（修「重启后订单模块不显示今天交易记录」）。

        ttt_trades 是持仓生命周期表（每单一行、状态迁移），不是订单事件日志：已平仓行 qty 归零、
        无独立卖单行。故此处按行的 status 反推事件——
          pending/open/cancelled → 一条 buy（qty=行 qty，准确）；
          closed/external_closed → buy(filled) + sell(filled)，卖单张数从 total_pnl 反推
            （(exit−entry)×100×qty）；external_closed 无成交价无法反推 → 记 0（如实，不臆造）。
        best-effort：单行异常只跳过该行，不影响其余重建。
        """
        with self._apply_lock:
            before = {key: event.to_dict() for key, event in self._events.items()}
        _BUY_STATUS = {"pending": "pending", "open": "filled", "cancelled": "cancelled",
                       "closed": "filled", "external_closed": "filled"}
        for r in rows:
            try:
                status = (r.get("status") or "").lower()
                oid = r["order_id"]
                ticker = (r.get("ticker") or "").upper()
                code = r.get("option_code") or ""
                entry_ts = _as_utc(r.get("entry_time")) or _utcnow()
                qty = _recover_trade_qty(r)
                entry_exists = oid in self._events
                if not entry_exists:
                    self._events[oid] = OrderEvent(
                        order_id=oid, ticker=ticker, option_code=code, side="buy", qty=qty,
                        status=_BUY_STATUS.get(status, "filled"), price=_as_dec(r.get("entry_mid")),
                        ts=entry_ts, updated_ts=_as_utc(r.get("updated_at")) or entry_ts,
                        origin="program", action="open")
                if status in ("closed", "external_closed"):
                    sid = oid + ":sell"
                    if any(
                        ev.side == "sell" and ev.parent_order_id == oid
                        for ev in self._events.values()
                    ):
                        continue
                    sell_ts = _as_utc(r.get("exit_time")) or entry_ts
                    close_reason = r.get("close_reason")
                    close_origin, close_action = _infer_local_semantics("sell", close_reason)
                    self._events[sid] = OrderEvent(
                        order_id=sid, ticker=ticker, option_code=code, side="sell", qty=qty,
                        status="filled", price=_as_dec(r.get("exit_mid")),
                        reason=close_reason, ts=sell_ts, updated_ts=sell_ts,
                        origin=close_origin, action=close_action,
                        parent_order_id=oid)
            except Exception:  # noqa: BLE001 — 单行重建失败不影响其余
                continue
        with self._apply_lock:
            after = {key: event.to_dict() for key, event in self._events.items()}
            if after != before:
                self._bump_revision_locked()

    def rebuild_from_order_events(self, rows) -> None:
        """从 ``ttt_order_events`` 恢复逐笔真实订单；不在恢复过程中反向写库。"""
        with self._apply_lock:
            before = {key: event.to_dict() for key, event in self._events.items()}
        for r in rows:
            try:
                status = str(r.get("status") or "").lower()
                if status not in {"pending", "filled", "cancelled"}:
                    continue
                order_id = str(r["order_id"])
                ts = _as_utc(r.get("submitted_at") or r.get("ts")) or _utcnow()
                self._events[order_id] = OrderEvent(
                    order_id=order_id,
                    ticker=str(r.get("ticker") or "").upper(),
                    option_code=str(r.get("option_code") or ""),
                    side=str(r.get("side") or "").lower(),
                    qty=_broker_int(r.get("qty")),
                    status=status,
                    price=_as_dec(r.get("price")),
                    reason=r.get("reason"),
                    ts=ts,
                    updated_ts=_as_utc(r.get("updated_at") or r.get("updated_ts")) or ts,
                    price_type=r.get("price_type"),
                    limit_price=_as_dec(r.get("limit_price")),
                    fail_reason=r.get("fail_reason"),
                    counts_toward_risk=True,
                    origin=str(r.get("origin") or "program"),
                    action=str(r.get("action") or "unknown"),
                    cancel_origin=r.get("cancel_origin"),
                    broker_status=r.get("broker_status"),
                    client_order_id=r.get("client_order_id"),
                    parent_order_id=r.get("parent_order_id"),
                    triggered_at=_as_utc(r.get("triggered_at")),
                    trigger_bid=_as_dec(r.get("trigger_bid")),
                    trigger_ask=_as_dec(r.get("trigger_ask")),
                    trigger_value=_as_dec(r.get("trigger_value")),
                    trigger_loss=_as_dec(r.get("trigger_loss")),
                    stop_mode=r.get("stop_mode"),
                    stop_threshold=_as_dec(r.get("stop_threshold")),
                    entry_value=_as_dec(r.get("entry_value")),
                    applied_qty=_broker_int(r.get("qty")) if status == "filled" else 0,
                    applied_notional=(
                        (_as_dec(r.get("price")) or Decimal("0"))
                        * Decimal(_broker_int(r.get("qty")))
                        if status == "filled" else Decimal("0")
                    ),
                )
            except Exception:  # noqa: BLE001 — 单行坏数据不能阻断其余订单恢复
                continue
        with self._apply_lock:
            after = {key: event.to_dict() for key, event in self._events.items()}
            if after != before:
                self._bump_revision_locked()


def _as_utc(dt) -> datetime | None:
    """DB 取回的时间统一成 tz-aware UTC（today() 按 UTC 日期过滤，须口径一致）。"""
    if dt is None:
        return None
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(dt, datetime):
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _as_dec(v) -> Decimal | None:
    return None if v in (None, "") else Decimal(str(v))


def _broker_field(obj, name: str, default=None):
    return obj.get(name, default) if isinstance(obj, dict) else getattr(obj, name, default)


def _broker_text(value) -> str | None:
    value = getattr(value, "value", value)
    return None if value is None else str(value)


def _broker_int(value) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def _journal_status(status: str) -> str | None:
    if status == "filled":
        return "filled"
    if status in _BROKER_PENDING:
        return "pending"
    if status in _BROKER_CANCELLED:
        return "cancelled"
    return None


def _infer_local_semantics(side: str, reason: str | None) -> tuple[str, str]:
    """兼容旧调用/DB 重建：只在理由有明确证据时推断，不把未知订单硬说成手动。"""
    text = str(reason or "")
    if text.startswith("watchdog:"):
        return "system", "auto_close"
    if text == "external_closed":
        return "external", "close"
    if "加仓" in text:
        return "user", "add"
    if "减仓" in text:
        return "user", "reduce"
    if "开仓" in text:
        return "user", "open"
    if side == "sell" and text:
        return "user", "close"
    return "unknown", "unknown"


def _client_order_semantics(
    client_order_id: str | None,
    broker_status: str,
) -> tuple[str, str, str | None]:
    """client_order_id 只证明“本程序订单”；业务动作/原因只从本地数据库恢复。"""
    cid = str(client_order_id or "").lower()
    if not cid.startswith("ttt-"):
        return "unknown", "unknown", None
    return "program", "unknown", None


def _terminal_reason(ev: OrderEvent, broker_status: str) -> str:
    """把 Alpaca 终态翻成用户可读且不越过证据边界的说明。"""
    status = str(broker_status or "").lower()
    if status == "rejected":
        return "Alpaca 拒单 · 券商未提供更详细原因"
    if status == "expired":
        return "Alpaca 到期未成交"
    if status == "done_for_day":
        return "Alpaca 收盘终止当日订单"
    if status in {"stopped", "suspended"}:
        return f"Alpaca 暂停订单（{status}）"
    if status == "replaced":
        return "Alpaca 已用新订单替换"
    if status in {"canceled", "cancelled"}:
        if ev.cancel_origin == "user":
            base = ev.fail_reason or "用户手动撤单"
            return f"{base} · Alpaca 已确认取消"
        if ev.cancel_origin == "system":
            base = ev.fail_reason
            if not base:
                base = "系统自动撤单"
            return f"{base} · Alpaca 已确认取消"
        if ev.cancel_origin == "external" or ev.origin == "external":
            return "外部订单已取消 · 无法判断由谁发起"
        return "Alpaca 已取消 · 无法从券商状态判断撤单发起者"
    return f"Alpaca 订单终止（{status or 'unknown'}）"


def _ticker_for_symbol(symbol: str) -> str:
    match = _OPTION_SYMBOL_RE.match(symbol)
    if not match:
        return symbol
    root = match.group(1)
    return "SPX" if root == "SPXW" else root


def _recover_trade_qty(r) -> int:
    """已平仓行 qty 归零 → 从 total_pnl 反推张数；反推不了（无成交价/价差为0）→ 退回行 qty。"""
    q = int(r.get("qty") or 0)
    if q > 0:
        return q
    em, xm, tp = _as_dec(r.get("entry_mid")), _as_dec(r.get("exit_mid")), _as_dec(r.get("total_pnl"))
    if em is not None and xm is not None and tp is not None and xm != em:
        n = (abs(tp) / (abs(xm - em) * _HUNDRED)).to_integral_value(rounding=ROUND_HALF_UP)
        return max(1, int(n))
    return q


JOURNAL = OrderJournal(persist=True)
