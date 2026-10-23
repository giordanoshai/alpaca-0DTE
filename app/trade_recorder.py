"""⑧ 交易记录器 —— 纯 best-effort 写穿式落库（ttt_trades + ttt_intercept_log）。

任何库异常只 logger.warning、绝不上抛（不得阻塞下单/平仓链路）。
键：ttt_trades 唯一键 = 入场买单 order_id（= ManagedPosition.order_id）。
app 侧只 upsert/update/insert，不建表（建表 + grant 由部署侧特权账号执行）。
"""
from __future__ import annotations

import functools
import json
import logging
from datetime import datetime, timezone
from decimal import Decimal

from app import db
from app.config import env
from app.order_journal import JOURNAL

from app.core.dailylogger import get_logger

logger = get_logger("tt.recorder")
audit_logger = get_logger("tt.trade")

_HUNDRED = Decimal("100")


def execution_source_label(source_name: str) -> str:
    """把运行时 watcher 名称细分为真实 PAPER 与本地 MOCK 账本来源。

    ``WatcherDataSource.name`` 在所有行情模式下都是 watcher；若直接入库会让白天回放
    成交污染晚间 PAPER 的交易次数、连亏和冷却。MOCK 模式或本机 Alpaca endpoint
    一律标记为 ``mock``，记录仍完整保留，但不进入真实纪律统计。
    """
    data_source = env("DATA_SOURCE", "yahoo").strip().lower()
    endpoint = env("ALPACA_END_POINT", "").strip().lower()
    if (
        data_source == "mock"
        or "://127.0.0.1" in endpoint
        or "://localhost" in endpoint
        or "://[::1]" in endpoint
    ):
        return "mock"
    return source_name


def _resolve_trade_id(explicit: str | None, fields: dict) -> str | None:
    """解析贯穿交易全生命周期的 ``trade_id``（2026-08-04 审查第 1 轮修正）。

    最初设计假定 ``order_id`` 在所有调用点都是入场买单 id，但
    ``exec_close._order_audit_context()`` 与 ``record_filled(side != "buy")``
    证伪了这个假设——平仓类事件的 ``order_id`` 装的是**平仓单自己的 id**，
    入场单 id 另存在 ``parent_order_id`` 里（或者要去 ``JOURNAL`` 查）。若继续
    直接回落 ``order_id``，同一笔交易的开仓/平仓事件会带不同的 ``trade_id``，
    「grep 一个 ID 重建全链路」失效，且是静默错——比没有这个字段更糟。

    解析优先级：
      1. 显式传入的 ``trade_id`` —— 最高优先，不得被覆盖。
      2. 调用方已给出的 ``parent_order_id`` —— 直接采用，省一次账本查找
         （``_order_audit_context`` 这类调用已经算好）。
      3. 拿 ``order_id`` 去 ``JOURNAL`` 查：查到就用
         ``ev.parent_order_id or order_id``——买单的 ``parent_order_id`` 为空则
         返回自己，卖单则返回其入场单 id。
      4. 查不到（例如闸门拦截阶段订单还没进账本）就用 ``order_id`` 原值。

    **新耦合（2026-08-04 审查提示）**：第 3 步 ``JOURNAL.get(order_id)`` 引入了
    「日志路径 → 交易状态锁」的耦合——``OrderJournal.get()`` 内部持有
    ``self._apply_lock``（``threading.RLock``）。已核实安全：``audit()`` 的调用方
    与写入 ``JOURNAL``（``submitted``/``mark_filled`` 等）的调用方是同一批业务
    代码，锁是可重入的 ``RLock``，同线程重入不会死锁；且这里只读一次 ``get()``
    立即释放，不会跨调用持锁。风险仅在于：若未来有人在已持有 ``_apply_lock``
    的调用栈里、由另一个不认识该锁的线程去调 ``audit()``，才可能出现跨线程等待；
    目前代码路径不存在这种用法。
    """
    if explicit is not None:
        return explicit
    parent = fields.get("parent_order_id")
    if parent:
        return parent
    order_id = fields.get("order_id")
    if not order_id:
        return None
    ev = JOURNAL.get(order_id)
    if ev is None:
        return order_id
    return ev.parent_order_id or order_id


def audit(event: str, *, trade_id: str | None = None, intent_id: str | None = None,
          broker_ts=None, **fields) -> None:
    """写一条不会影响交易链路的结构化审计日志。

    ``tt.trade`` 只接收调用方挑选后的业务字段，不记录数据库连接、请求头或券商
    完整响应。Decimal/datetime 等统一转字符串，保证日志本身永不因序列化失败而
    打断提交、成交或平仓。

    事件级字段（2026-08-04 日志地基）：
      - ``trade_id``  贯穿该笔交易全生命周期。解析规则见 ``_resolve_trade_id()``
        ——不是简单回落 ``order_id``：平仓类事件的 ``order_id`` 是平仓单自己的
        id，必须换成入场单 id 才能保证开仓/平仓事件同一个 ``trade_id``。
      - ``intent_id`` 买入受理号，比 order_id 更早存在；闸门拦截的单只有它。
      - ``broker_ts`` 券商钟原始值。**不在此处算与本地钟的差**：单对时间戳里
        时钟偏移与真实延迟不可分离，可用的是两个同钟相减，推导留给分析侧。
    """
    try:
        payload = {
            "event": event,
            "data_source": env("DATA_SOURCE", "yahoo").lower(),
            **fields,
        }
        resolved = _resolve_trade_id(trade_id, fields)
        if resolved is not None:
            payload["trade_id"] = resolved
        if intent_id is not None:
            payload["intent_id"] = intent_id
        if broker_ts is not None:
            payload["broker_ts"] = broker_ts
        audit_logger.info(json.dumps(
            payload, ensure_ascii=False, default=str, separators=(",", ":")))
    except Exception:  # noqa: BLE001 — 审计旁路绝不能影响真实交易
        return None


def _besteffort(event: str):
    """把 record_* 包成 best-effort：异常只告警、返回 None，绝不 raise。"""
    def deco(fn):
        @functools.wraps(fn)
        def wrap(*a, **k):
            try:
                return fn(*a, **k)
            except Exception as e:  # noqa: BLE001 — 写库失败不得阻塞交易链路
                logger.warning(json.dumps(
                    {"event": "trade_record_error", "op": event, "error": str(e)},
                    ensure_ascii=False))
                return None
        return wrap
    return deco


def _write(sql: str, params: tuple) -> None:
    """单语句写 + commit + close（短连接，与 templates 的逐行持久化同口径）。"""
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


@_besteffort("order_event")
def record_order_event(event: dict) -> None:
    """逐笔订单写穿 ``ttt_order_events``。

    与一仓一行的 ``ttt_trades`` 不同，本表以每个真实 Alpaca ``order_id`` 为键，
    因此多次加仓、减仓、追价撤单和最终全平都不会互相覆盖。
    """
    _write(
        """
        insert into ttt_order_events
          (order_id, parent_order_id, client_order_id, ticker, option_code,
           side, qty, status, origin, action, reason, cancel_origin,
           fail_reason, broker_status, price, price_type, limit_price,
           submitted_at, updated_at, triggered_at, trigger_bid, trigger_ask,
           trigger_value, trigger_loss, stop_mode, stop_threshold, entry_value)
        values
          (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
           %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        on conflict (order_id) do update set
          parent_order_id=coalesce(excluded.parent_order_id, ttt_order_events.parent_order_id),
          client_order_id=coalesce(excluded.client_order_id, ttt_order_events.client_order_id),
          ticker=excluded.ticker,
          option_code=excluded.option_code,
          side=excluded.side,
          qty=excluded.qty,
          status=excluded.status,
          origin=excluded.origin,
          action=excluded.action,
          reason=coalesce(excluded.reason, ttt_order_events.reason),
          cancel_origin=coalesce(excluded.cancel_origin, ttt_order_events.cancel_origin),
          fail_reason=excluded.fail_reason,
          broker_status=coalesce(excluded.broker_status, ttt_order_events.broker_status),
          price=coalesce(excluded.price, ttt_order_events.price),
          price_type=coalesce(excluded.price_type, ttt_order_events.price_type),
          limit_price=coalesce(excluded.limit_price, ttt_order_events.limit_price),
          triggered_at=coalesce(ttt_order_events.triggered_at, excluded.triggered_at),
          trigger_bid=coalesce(ttt_order_events.trigger_bid, excluded.trigger_bid),
          trigger_ask=coalesce(ttt_order_events.trigger_ask, excluded.trigger_ask),
          trigger_value=coalesce(ttt_order_events.trigger_value, excluded.trigger_value),
          trigger_loss=coalesce(ttt_order_events.trigger_loss, excluded.trigger_loss),
          stop_mode=coalesce(ttt_order_events.stop_mode, excluded.stop_mode),
          stop_threshold=coalesce(ttt_order_events.stop_threshold, excluded.stop_threshold),
          entry_value=coalesce(ttt_order_events.entry_value, excluded.entry_value),
          submitted_at=least(excluded.submitted_at, ttt_order_events.submitted_at),
          updated_at=greatest(excluded.updated_at, ttt_order_events.updated_at)
        """,
        (
            event["order_id"],
            event.get("parent_order_id"),
            event.get("client_order_id"),
            event.get("ticker"),
            event.get("option_code"),
            event.get("side"),
            int(event.get("qty") or 0),
            event.get("status"),
            event.get("origin") or "unknown",
            event.get("action") or "unknown",
            event.get("reason"),
            event.get("cancel_origin"),
            event.get("fail_reason"),
            event.get("broker_status"),
            event.get("price"),
            event.get("price_type"),
            event.get("limit_price"),
            event.get("ts") or datetime.now(timezone.utc),
            event.get("updated_ts") or datetime.now(timezone.utc),
            event.get("triggered_at"),
            event.get("trigger_bid"),
            event.get("trigger_ask"),
            event.get("trigger_value"),
            event.get("trigger_loss"),
            event.get("stop_mode"),
            event.get("stop_threshold"),
            event.get("entry_value"),
        ),
    )
    parent_order_id = event.get("parent_order_id")
    if not parent_order_id and event.get("action") == "open":
        parent_order_id = event.get("order_id")
    if parent_order_id and event.get("status") in {"filled", "cancelled"}:
        _recompute_trade_from_order_events(str(parent_order_id))


def _recompute_trade_from_order_events(parent_order_id: str) -> None:
    """用同一生命周期的精确逐笔成交重算 ``ttt_trades`` 汇总账。

    任一已成交事件仍缺价格时保持现状，等 TradingStream/REST 后续补价再重算；
    绝不拿 0 或盘口猜测覆盖交易账。
    """
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                select side, qty, price, reason, updated_at
                from ttt_order_events
                where (order_id=%s or parent_order_id=%s)
                  and status='filled'
                order by submitted_at, order_id
                """,
                (parent_order_id, parent_order_id),
            )
            rows = cur.fetchall()
            if not rows or any(row[2] is None for row in rows):
                return

            buys = [(int(qty), Decimal(str(price))) for side, qty, price, _, _ in rows
                    if side == "buy" and int(qty) > 0]
            sells = [(int(qty), Decimal(str(price)), reason, updated_at)
                     for side, qty, price, reason, updated_at in rows
                     if side == "sell" and int(qty) > 0]
            if not buys:
                return

            bought_qty = sum(qty for qty, _ in buys)
            sold_qty = sum(qty for qty, _, _, _ in sells)
            buy_cost = sum(price * Decimal(qty) for qty, price in buys)
            entry_mid = buy_cost / Decimal(bought_qty)
            sell_proceeds = sum(
                price * Decimal(qty) for qty, price, _, _ in sells
            )
            realized = (
                sell_proceeds - entry_mid * Decimal(sold_qty)
            ) * _HUNDRED
            remaining = max(0, bought_qty - sold_qty)
            last_reason = next(
                (reason for _, _, reason, _ in reversed(sells) if reason),
                None,
            )

            if remaining == 0 and sold_qty > 0:
                exit_mid = sell_proceeds / Decimal(sold_qty)
                exit_time = max(updated_at for _, _, _, updated_at in sells)
                cur.execute(
                    """
                    update ttt_trades
                    set qty=0, entry_mid=%s, realized_pnl=%s,
                        exit_mid=%s, exit_time=%s, total_pnl=%s,
                        status='closed',
                        close_reason=coalesce(%s, close_reason),
                        updated_at=now()
                    where order_id=%s and qty=0
                    """,
                    (
                        entry_mid, realized, exit_mid, exit_time, realized,
                        last_reason, parent_order_id,
                    ),
                )
            else:
                qty_guard = "" if sold_qty == 0 else " and qty=%s"
                params = [
                    remaining, entry_mid, realized,
                    last_reason, parent_order_id,
                ]
                if sold_qty > 0:
                    params.append(remaining)
                cur.execute(
                    f"""
                    update ttt_trades
                    set qty=%s, entry_mid=%s, realized_pnl=%s,
                        status='open',
                        close_reason=coalesce(%s, close_reason),
                        updated_at=now()
                    where order_id=%s{qty_guard}
                    """,
                    tuple(params),
                )
        conn.commit()
    finally:
        conn.close()


def load_order_events(session: dict) -> list[dict]:
    """读取当前 ET 自然日终态和全部 pending；缺表/断库时降级为空。"""
    where_clause = (
        "status='pending'"
        " or (submitted_at >= %s and submitted_at < %s)"
        " or (updated_at >= %s and updated_at < %s)"
    )
    params = (
        session["session_start"],
        session["session_end"],
        session["session_start"],
        session["session_end"],
    )
    try:
        conn = db.connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    select order_id, parent_order_id, client_order_id, ticker, option_code,
                           side, qty, status, origin, action, reason, cancel_origin,
                           fail_reason, broker_status, price, price_type, limit_price,
                           submitted_at, updated_at, triggered_at, trigger_bid,
                           trigger_ask, trigger_value, trigger_loss, stop_mode,
                           stop_threshold, entry_value
                    from ttt_order_events
                    where {where_clause}
                    order by updated_at
                    """,
                    params,
                )
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001 — 新表未部署/临时断库不得阻断交易启动
        logger.warning(json.dumps(
            {"event": "trade_record_error", "op": "load_order_events", "error": str(e)},
            ensure_ascii=False))
        return []


@_besteffort("submitted")
def record_submitted(pos, source_name: str, template_snapshot: dict | None) -> None:
    """入场提交：upsert 一行 pending（键 order_id）。已存在则不动（幂等）。"""
    source_name = execution_source_label(source_name)
    audit(
        "trade_order_submitted",
        order_id=pos.order_id,
        ticker=pos.ticker,
        direction=pos.direction,
        contract=pos.option_code,
        side="buy",
        qty=pos.qty,
        reference_price=pos.entry_mid,
        status="pending",
        source=source_name,
    )
    frozen_snapshot = dict(template_snapshot or {})
    frozen_snapshot["stop_mode"] = getattr(pos, "stop_mode", "percent")
    _write(
        """
        insert into ttt_trades
          (order_id, ticker, direction, option_code, qty, entry_mid, entry_time,
           status, source, template_snapshot)
        values (%s, %s, %s, %s, %s, %s, %s, 'pending', %s, %s)
        on conflict (order_id) do nothing
        """,
        (pos.order_id, pos.ticker, pos.direction, pos.option_code, pos.qty,
         pos.entry_mid, pos.entry_time, source_name,
         json.dumps(frozen_snapshot, ensure_ascii=False)),
    )


@_besteffort("filled")
def record_filled(
    order_id: str,
    entry_mid=None,
    qty: int | None = None,
    *,
    side: str = "buy",
) -> None:
    """成交：pending → open（幂等，仅 pending 行迁移）。

    entry_mid/qty 给出时一并回填**券商真实成交均价与成交量**（审查 3.2）——
    入场行原本记的是选约时的 mid 与委托量，事后复盘和风控 PnL 都会偏。
    """
    audit(
        "trade_order_filled",
        order_id=order_id,
        side=side,
        fill_price=entry_mid,
        filled_qty=qty,
        status="open" if side == "buy" else "filled",
    )
    if side != "buy":
        # 卖单逐笔状态由 ttt_order_events 保存；ttt_trades 的主键是入场买单 id，
        # 不能拿 close order id 去更新生命周期，更不能在审计里谎报成 buy。
        return
    sets = ["status='open'", "updated_at=now()"]
    params: list = []
    if entry_mid is not None:
        sets.append("entry_mid=%s")
        params.append(entry_mid)
    if qty is not None:
        sets.append("qty=%s")
        params.append(qty)
    params.append(order_id)
    _write(
        f"update ttt_trades set {', '.join(sets)} where order_id=%s and status='pending'",
        tuple(params),
    )
    _recompute_trade_from_order_events(order_id)


@_besteffort("cancelled")
def record_cancelled(order_id: str, *, side: str = "buy") -> None:
    """成交前撤单：pending → cancelled。"""
    audit(
        "trade_order_cancelled",
        order_id=order_id,
        side=side,
        status="cancelled",
    )
    if side != "buy":
        return
    _write(
        "update ttt_trades set status='cancelled', updated_at=now() "
        "where order_id=%s and status='pending'",
        (order_id,),
    )


@_besteffort("add")
def record_add(order_id: str, new_qty: int, new_entry_mid) -> None:
    """加仓：更新入场行的 qty 与加权 entry_mid（与 REGISTRY.add_qty 同口径，值由调用方给）。"""
    audit(
        "trade_position_added",
        order_id=order_id,
        side="buy",
        total_qty=new_qty,
        average_entry_price=new_entry_mid,
        status="open",
    )
    _write(
        "update ttt_trades set qty=%s, entry_mid=%s, updated_at=now() where order_id=%s",
        (new_qty, new_entry_mid, order_id),
    )


@_besteffort("position_sync")
def record_position_sync(order_id: str, new_qty: int, new_entry_mid=None) -> None:
    """券商真源校正持仓后同步生命周期行。

    与 ``record_filled`` 不同，这里必须允许更新已经是 open 的行；否则内存虽按券商
    校正，数据库仍会保留加仓竞态产生的旧数量。均价只有在券商明确给出时才更新，
    不用本地报价臆造。
    """
    sets = ["qty=%s", "updated_at=now()"]
    params: list = [new_qty]
    if new_entry_mid is not None:
        sets.append("entry_mid=%s")
        params.append(new_entry_mid)
    params.append(order_id)
    _write(
        f"update ttt_trades set {', '.join(sets)} "
        "where order_id=%s and status='open'",
        tuple(params),
    )


@_besteffort("position_reconciled")
def record_reconciled_position(order_id: str, new_qty: int, new_entry_mid) -> None:
    """对账确认逐笔订单仍有本系统净持仓时，恢复误关的生命周期汇总行。

    ``ttt_order_events`` 保留已实现卖单，故 realized_pnl 不清零；但仓位重新为 open 后，
    原先误写的全平字段必须清除，避免界面/统计同时把它当作已结束交易。
    """
    audit(
        "trade_position_reconciled",
        order_id=order_id,
        side="buy",
        qty=new_qty,
        average_entry_price=new_entry_mid,
        status="open",
    )
    _write(
        """
        update ttt_trades
        set qty=%s, entry_mid=%s, status='open',
            exit_mid=null, exit_time=null, total_pnl=null, close_reason=null,
            updated_at=now()
        where order_id=%s
        """,
        (new_qty, new_entry_mid, order_id),
    )


@_besteffort("closed")
def record_closed(order_id: str, exit_mid, sold_qty: int, reason: str,
                  external: bool = False) -> None:
    """(部分)平仓：读现行 → 累计 realized_pnl、减 qty；全平落定 exit/total_pnl。

    exit_mid=None（外部平仓无成交价）→ 本次加 0，total_pnl = 累计 realized。
    行不存在（入场行未写成，best-effort）→ 静默返回。
    """
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "select entry_mid, qty, realized_pnl from ttt_trades where order_id=%s",
                (order_id,))
            row = cur.fetchone()
            if row is None:
                return
            entry_mid, qty, realized = row
            exit_d = None if exit_mid is None else Decimal(str(exit_mid))
            add_pnl = (Decimal(0) if exit_d is None
                       else (exit_d - entry_mid) * _HUNDRED * Decimal(sold_qty))
            new_realized = realized + add_pnl
            new_qty = qty - sold_qty
            if new_qty <= 0:
                status = "external_closed" if external else "closed"
                cur.execute(
                    "update ttt_trades "
                    "set qty=0, realized_pnl=%s, exit_mid=%s, exit_time=now(), "
                    "total_pnl=%s, status=%s, close_reason=%s, updated_at=now() "
                    "where order_id=%s",
                    (new_realized, exit_d, new_realized, status, reason, order_id))
            else:
                cur.execute(
                    "update ttt_trades set qty=%s, realized_pnl=%s, status='open', "
                    "close_reason=%s, updated_at=now() "
                    "where order_id=%s",
                    (new_qty, new_realized, reason, order_id))
        conn.commit()
        audit(
            "trade_position_closed" if new_qty <= 0 else "trade_position_reduced",
            order_id=order_id,
            side="sell",
            sold_qty=sold_qty,
            entry_price=entry_mid,
            exit_price=exit_d,
            trade_pnl=add_pnl,
            realized_pnl=new_realized,
            remaining_qty=max(new_qty, 0),
            status=("external_closed" if external else "closed") if new_qty <= 0 else "open",
            reason=reason,
            external=external,
        )
        # 平仓改变了已平仓成交集 → 失效风控成交列表缓存，下次 gate 即时重读（连亏冷却/日亏当场生效）。
        from app.services.account_state import invalidate_closed_trades_cache
        invalidate_closed_trades_cache()
    finally:
        conn.close()
    _recompute_trade_from_order_events(order_id)


def load_today() -> list[dict]:
    """读「与当日（UTC）相关」的 ttt_trades 行（entry 或 exit 落在今天），供启动时重建订单流。

    best-effort：任何库异常 → 返回 []（订单面板退化为空白，不阻断启动）。返回列名→值 dict 列表。
    """
    from datetime import datetime, timezone
    day_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        conn = db.connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "select order_id, ticker, direction, option_code, qty, entry_mid, entry_time, "
                    "status, realized_pnl, exit_mid, exit_time, total_pnl, close_reason, "
                    "template_snapshot, updated_at "
                    "from ttt_trades where entry_time >= %s or exit_time >= %s",
                    (day_start, day_start))
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001 — 读库失败不得阻断启动
        logger.warning(json.dumps({"event": "trade_record_error", "op": "load_today", "error": str(e)},
                                  ensure_ascii=False))
        return []


def _s(v) -> str | None:
    """Decimal/numeric → str；None → None（jsonb 写入前用）。"""
    return None if v is None else str(v)


def template_snapshot() -> dict | None:
    """当前生效模板快照 {name, params}；取数失败 → None（不得阻塞调用方）。"""
    try:
        from app.templates import get_store
        tpl = get_store().active()
        return {"name": tpl.name, "params": tpl.to_dict()}
    except Exception as e:  # noqa: BLE001
        logger.warning(json.dumps(
            {"event": "trade_record_error", "op": "template_snapshot", "error": str(e)},
            ensure_ascii=False))
        return None


def market_snapshot(ctx) -> dict | None:
    """MarketContext → 拦截留痕用 dict（signal + account，Decimal→str）。

    在 gate_order reject 分支作 record_intercept 的实参构造。取值失败 → None
    （与 template_snapshot 对称，保证「gate_order 参数永不抛」不变量；record_intercept
    收到 None 写 NULL）。
    """
    try:
        sig, acct = ctx.signal, ctx.account
        return {
            # 2026-09-01: 去掉 er / ema_dir_1m / ema_dir_5m —— 它们是规则 5 与
            # 规则 6 的判据，两条规则同日删除。剩下这三个是「拒单当时行情什么样」
            # 的纯记录，不对应任何判据（2026-08-26 之前这里记的 regime 也是同样
            # 的性质，那次删掉是因为它每行都写同一个常量）。
            "signal": {"price": _s(sig.price), "vwap": _s(sig.vwap), "atr": _s(sig.atr)},
            "account": {"daily_pnl": _s(acct.daily_pnl), "weekly_pnl": _s(acct.weekly_pnl),
                        "consecutive_losses": acct.consecutive_losses},
        }
    except Exception as e:  # noqa: BLE001 — 快照构造失败不得把干净拒单变成 500
        logger.warning(json.dumps(
            {"event": "trade_record_error", "op": "market_snapshot", "error": str(e)},
            ensure_ascii=False))
        return None


@_besteffort("intercept")
def record_intercept(rule: str, reason: str, ticker: str, direction: str | None,
                     qty: int, source_name: str,
                     market_snapshot: dict | None,
                     template_snapshot: dict | None) -> None:
    """拒单：insert 一行 ttt_intercept_log（append-only）。"""
    audit(
        "trade_gate_rejected",
        ticker=ticker,
        direction=direction,
        qty=qty,
        rule=rule,
        reason=reason,
        source=source_name,
    )
    _write(
        """
        insert into ttt_intercept_log
          (ticker, direction, qty_requested, rule, reason, source,
           market_snapshot, template_snapshot)
        values (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (ticker, direction, qty, rule, reason, source_name,
         None if market_snapshot is None else json.dumps(market_snapshot, ensure_ascii=False),
         None if template_snapshot is None else json.dumps(template_snapshot, ensure_ascii=False)),
    )
