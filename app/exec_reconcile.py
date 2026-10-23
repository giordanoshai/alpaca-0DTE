"""持仓、订单基线与重启恢复对账。

从 routers/orders.py 收口迁出（Task 5）：路由只负责解析请求 + 前置 4xx 判断（_execution_enabled）
+ `await asyncio.to_thread(order_exec.<fn>, source, ...)` 透传；本模块内聚真正的业务体，
均为同步纯函数，`source`（当前数据源）与 `req`（router 的 Pydantic 模型）作参数，返回纯 dict。

对账/未登记仓扫描改用 AlpacaTrading 公开方法（broker_positions/closed_buy_orders），
消除对 `ec._tc` 的私有穿透。
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from decimal import Decimal

from app.broker_pnl import PROGRAM_CLIENT_ORDER_PREFIX
from app.broker_order_state import (
    broker_status as _broker_status,
    is_order_not_found as _is_order_not_found,
)
from app.integrations.alpaca.trading import AlpacaTrading
from app.order_journal import JOURNAL
from app.core.market_session import order_session_window
from app.position_registry import ManagedPosition, REGISTRY
from app.templates import get_store
from app import trade_recorder

from app.core.dailylogger import get_logger

logger = get_logger("tt.gate")

_EXEC_SOURCES = {"live", "watcher"}
_OCC_RE = re.compile(r"^([A-Z]+)\d{6}([CP])\d{8}$")
_BROKER_ORDER_PENDING = frozenset({
    "accepted", "accepted_for_bidding", "calculated", "held", "new",
    "partially_filled", "pending_cancel", "pending_new", "pending_replace",
    "stopped",
})
def _execution_enabled(name: str) -> bool:
    """可走 Alpaca 真实（paper）执行的数据源：watcher 实盘、live 直连。"""
    return name in _EXEC_SOURCES


def fetch_broker_order_session(now: datetime | None = None) -> list:
    """读取当前 ET 自然日订单，并补齐所有跨日 OPEN 单。

    只供启动、ET 换日和 TradingStream 重连建立基线；后续增量由 ``trade_updates`` WS
    提供。
    """
    window = order_session_window(now)
    query_session = window["query_until"] > window["session_start"]
    after = window["session_start"] if query_session else None
    until = window["query_until"] if query_session else None
    return AlpacaTrading().broker_order_session(after=after, until=until)


def _as_dec(v) -> Decimal | None:
    """atomic_close 回传的价位是字符串 → Decimal；缺失/不可解析 → None（订单流展示用，不得抛）。"""
    if v in (None, ""):
        return None
    try:
        return Decimal(str(v))
    except Exception:  # noqa: BLE001
        return None


def _stop_mode_from_snapshot(value) -> str:
    """恢复开仓时冻结的止损模式；旧记录没有该字段时按新默认 percent。"""
    try:
        snapshot = json.loads(value) if isinstance(value, str) else (value or {})
        mode = snapshot.get("stop_mode")
        return mode if mode in {"percent", "amount"} else "percent"
    except Exception:  # noqa: BLE001 — 坏快照不能阻断持仓恢复
        return "percent"



def list_unmanaged_positions(source) -> list[dict]:
    """定时 REST 真源扫描：只返回 REGISTRY 里没登记的期权持仓。

    口径变更（2026-08-15，SOS 移出本项目后）：此前这里会向 SOS 账本查一次 swing
    持仓并从券商张数里扣掉——因为订单能靠 `ttt-` / `sos-` 前缀分流，**持仓不能**
    （券商侧持仓只有 symbol + qty，没有 client_order_id，同合约被两套系统持有时
    是合并的一条），归属只能各自账本自报。SOS 已整体移出，那本账现在不存在了。

    **因此：若券商账户里还残留 SOS 时期的 swing 仓，它会被列进这个「未登记持仓」
    表。** 这是只读展示、本身不平仓，但别顺手去点找回——把它拉进 REGISTRY 就等于
    交给 0DTE 风控按 0DTE 的止损纪律处置。要处理这类残仓，请手动在券商端操作。
    """
    unmanaged = []
    if not _execution_enabled(source.name):
        return unmanaged
    try:
        ec = AlpacaTrading()
        for bp in ec.broker_positions():
            m = _OCC_RE.match(bp.symbol)
            if not m:
                continue
            managed = REGISTRY.get(bp.symbol)
            broker_qty = int(float(bp.qty))
            managed_qty = managed.qty if managed is not None else 0
            # 只扣已登记的张数，多出来的仍是来路不明的仓，照报不误
            unmanaged_qty = broker_qty - managed_qty
            if unmanaged_qty <= 0:
                continue
            unmanaged.append({"option_code": bp.symbol, "ticker": m.group(1),
                              "type": "CALL" if m.group(2) == "C" else "PUT",
                              "qty": unmanaged_qty,
                              "avg_entry_price": str(bp.avg_entry_price),
                              "same_contract_managed_qty": managed_qty})
    except Exception as e:  # noqa: BLE001 — 对比失败不得挡住已登记持仓的展示
        logger.warning(json.dumps({"event": "unmanaged_check_error", "error": str(e)}, ensure_ascii=False))
    return unmanaged


def _position_quotes(alpaca, codes: list[str]) -> dict:
    """持仓盘口：Option WS 优先，未命中的合并成**一发**批量 REST。

    本函数在 `TradingStateHub._positions_loop` 里 0.5 秒跑一拍。原实现逐个持仓
    `option_quote()`，N 个仓没命中 WS 就是每拍 N 发 REST，打的正是那个已经在限流的
    行情 API（2026-08-11 三次 alpaca_rate_limited）；而这条循环是串行 while，
    它卡多久前端持仓面板就停更多久。

    **报价拿不到不得挡住持仓可见性**：任何一环失败都退化为该合约 quote=None，
    前端回退显示入场价。
    """
    out: dict = {}
    if alpaca is None or not codes:
        return out

    from app.option_stream import OPTION_STREAM

    pending: list[str] = []
    for code in codes:
        try:
            hit = OPTION_STREAM.quote(code)
        except Exception:  # noqa: BLE001 — WS 侧问题只当没命中，走 REST 兜底
            hit = None
        if hit:
            out[code] = hit
        else:
            pending.append(code)
    if not pending:
        return out

    batch = getattr(alpaca, "option_quotes", None)
    try:
        if batch is not None:
            out.update(batch(pending) or {})
        else:
            # mock/精简数据源可能没实现批量接口，退回逐个取，行为与改动前一致。
            for code in pending:
                q = alpaca.option_quote(code)
                if q:
                    out[code] = q
    except Exception as e:  # noqa: BLE001 — 报价失败不得挡住整个持仓列表
        logger.warning(json.dumps(
            {"event": "position_quote_error", "contracts": pending,
             "error": str(e)[:200]}, ensure_ascii=False))
    return out


def list_positions(source, *, include_unmanaged: bool = True) -> dict:
    """本系统登记的持仓（不含账户里非本系统的既有仓）+ 当前报价，供前端算实时盈亏。

    报价仅可执行档（watcher/live，持有 AlpacaData）能取；取不到时 quote=None，
    前端回退显示入场价（不拦、不报错，报价源抖动不得影响持仓可见性）。
    """
    alpaca = getattr(source, "alpaca", None)
    # 持仓面板只展示真已成交的仓（open/closing）；pending（已提交未成交）在「订单流」面板
    # 里已经作为待成交单展示了，混进持仓列表会显得"还没成交的单子出现在持仓上"。
    positions = [p for p in REGISTRY.active() if p.status != "pending"]

    quotes = _position_quotes(alpaca, [p.option_code for p in positions])

    out = []
    for p in positions:
        quote = None
        q = quotes.get(p.option_code)
        if q:
            bid, ask = q
            quote = {"bid": str(bid), "ask": str(ask), "mid": str((bid + ask) / 2)}
        out.append({
            "option_code": p.option_code, "ticker": p.ticker, "direction": p.direction,
            "order_id": p.order_id,
            "qty": p.qty, "entry_mid": str(p.entry_mid), "status": p.status,
            "realized_pnl": str(p.realized_pnl),
            "close_error": p.close_error,
            "sync_note": p.sync_note,             # 非空=与券商聚合仓存在差异或已向下校正
            "close_order_id": p.close_order_id,   # 非空=平仓挂单在券商排队（可撤）
            "close_qty": p.close_qty,
            "cancel_requested": p.cancel_requested,
            "close_timeout_policy": p.close_timeout_policy,
            "risk_exit_state": p.risk_exit_state,
            "risk_exit_reason": p.risk_exit_reason,
            "entry_time": p.entry_time.isoformat(),
            "stop_pct": str(p.stop_pct), "dollar_line": str(p.dollar_line),
            "stop_mode": p.stop_mode, "max_hold_s": p.max_hold_s,
            "quote": quote,
        })
    # 未登记持仓（券商账户里真实存在、REGISTRY 没有的期权仓）：只读展示，不在此处自动
    # 找回——REGISTRY/JOURNAL 目前是纯内存态，热重载/重启会清空，是它们变成孤儿仓最
    # 常见的原因（找回见 POST /api/reconcile）。仅可执行档才有真实券商仓可比对。
    unmanaged = list_unmanaged_positions(source) if include_unmanaged else []
    return {"positions": out, "unmanaged": unmanaged}


def _owned_event_lifecycles(events: list[dict]) -> dict[str, dict]:
    """从可信的本程序订单事件计算每个开仓生命周期的净持仓。

    账户 position 只能证明“有仓”，不能证明归属。这里要求根开仓同时满足：
    ``origin=user|program``、``action=open``、client_order_id 以 ``ttt-`` 开头；
    后续加减/平仓也只统计带 ``ttt-`` client_order_id 的成交事件。
    """
    roots: dict[str, dict] = {}
    trusted_origins = {"user", "program", "system"}
    for event in events:
        client_order_id = str(event.get("client_order_id") or "")
        if (
            event.get("status") == "filled"
            and event.get("side") == "buy"
            and event.get("action") == "open"
            and event.get("origin") in trusted_origins
            and client_order_id.startswith("ttt-")
            and event.get("order_id")
            and event.get("option_code")
        ):
            root_id = str(event["order_id"])
            roots[root_id] = {
                "order_id": root_id,
                "option_code": event["option_code"],
                "ticker": event.get("ticker"),
                "bought_qty": 0,
                "sold_qty": 0,
                "buy_notional": Decimal("0"),
                "buy_prices_complete": True,
                "entry_time": event.get("submitted_at"),
            }

    for event in events:
        if event.get("status") != "filled":
            continue
        client_order_id = str(event.get("client_order_id") or "")
        if not client_order_id.startswith("ttt-"):
            continue
        root_id = str(
            event.get("parent_order_id")
            or (
                event.get("order_id")
                if event.get("action") == "open"
                else ""
            )
        )
        lifecycle = roots.get(root_id)
        if (
            lifecycle is None
            or event.get("option_code") != lifecycle["option_code"]
        ):
            continue
        try:
            qty = int(event.get("qty") or 0)
        except (TypeError, ValueError):
            continue
        if qty <= 0:
            continue
        side = event.get("side")
        if side == "buy":
            lifecycle["bought_qty"] += qty
            price = _as_dec(event.get("price"))
            if price is None:
                lifecycle["buy_prices_complete"] = False
            else:
                lifecycle["buy_notional"] += price * Decimal(qty)
        elif side == "sell":
            lifecycle["sold_qty"] += qty

    return {
        root_id: {
            **lifecycle,
            "net_qty": max(
                0,
                lifecycle["bought_qty"] - lifecycle["sold_qty"],
            ),
            "entry_mid": (
                lifecycle["buy_notional"] / Decimal(lifecycle["bought_qty"])
                if (
                    lifecycle["bought_qty"] > 0
                    and lifecycle["buy_prices_complete"]
                )
                else None
            ),
        }
        for root_id, lifecycle in roots.items()
    }


def _reattach_recovered_close(pos: ManagedPosition, ec) -> str | None:
    """将重启前的唯一在途程序卖单重新绑定到恢复后的持仓。

    本地事件只用于找候选；最终仍逐笔查询 Alpaca，核对 ID、合约、卖出方向、
    ``ttt-*`` 归属和在途状态。任一不符或同生命周期有多张候选都不绑定。
    """
    candidates = JOURNAL.pending_program_sells_for(
        pos.option_code,
        parent_order_id=pos.order_id,
    )
    if len(candidates) != 1:
        if len(candidates) > 1:
            logger.warning(json.dumps({
                "event": "reconcile_close_order_ambiguous",
                "contract": pos.option_code,
                "parent_order_id": pos.order_id,
                "order_ids": [item["order_id"] for item in candidates],
            }, ensure_ascii=False))
        return None

    event = candidates[0]
    order_id = str(event["order_id"])
    try:
        order = ec.get_order(order_id) or {}
    except Exception as exc:  # noqa: BLE001 — 对账失败时不猜绑定，订单面板仍可按 ID 撤
        logger.warning(json.dumps({
            "event": "reconcile_close_order_query_error",
            "contract": pos.option_code,
            "close_order_id": order_id,
            "error": str(exc),
        }, ensure_ascii=False))
        return None

    status = _broker_status(order)
    side = str(order.get("side") or "").rsplit(".", 1)[-1].lower()
    client_order_id = str(order.get("client_order_id") or "")
    if (
        str(order.get("id") or "") != order_id
        or str(order.get("symbol") or "").upper() != pos.option_code.upper()
        or side != "sell"
        or not client_order_id.startswith(PROGRAM_CLIENT_ORDER_PREFIX)
        or status not in _BROKER_ORDER_PENDING
    ):
        JOURNAL.upsert_broker_orders([order])
        return None

    try:
        order_qty = max(1, int(float(order.get("qty") or event.get("qty") or 1)))
    except (TypeError, ValueError):
        order_qty = max(1, int(event.get("qty") or 1))
    try:
        filled_qty = max(0, int(float(order.get("filled_qty") or 0)))
    except (TypeError, ValueError):
        filled_qty = 0

    pos.status = "closing"
    pos.close_order_id = order_id
    pos.close_qty = order_qty
    # reconcile 已按 Alpaca 当前持仓恢复剩余数量，因此重启前已发生的部分成交视为已应用，
    # 后续只结算该订单累计量相对此基线的新增部分。
    pos.close_applied_qty = min(filled_qty, order_qty)
    pos.close_reason = event.get("reason")
    pos.close_submitted_at = (
        datetime.fromisoformat(event["ts"])
        if event.get("ts")
        else datetime.now(timezone.utc)
    )
    pos.cancel_requested = status == "pending_cancel"
    pos.close_timeout_policy = (
        "persistent"
        if str(event.get("reason") or "").startswith("目标限价")
        else "quick"
    )
    trade_recorder.audit(
        "trade_close_order_reattached",
        order_id=pos.order_id,
        close_order_id=order_id,
        ticker=pos.ticker,
        direction=pos.direction,
        contract=pos.option_code,
        side="sell",
        qty=order_qty,
        filled_qty=filled_qty,
        broker_status=status,
        status="closing",
        reason=pos.close_reason,
    )
    return order_id


def _resolve_pending_entry_fills(ec) -> list[dict]:
    """把「券商已成交、本地还当未成交」的仓救回来。

    2026-08-25 现场：QQQ 认沽 23:06:34.551 就在券商成交了，但 watchdog 卡在一发
    没有超时的 REST 上整整 8 分 48 秒，登记表里那笔仓的状态一直停在 ``pending``。
    而 `list_positions` 按设计把 ``pending`` 藏起来 —— 于是这笔真实存在的仓在
    持仓总览里**完全不存在**：看不到、没有卖出按钮、F 快捷键也点不到。

    用户点了「对账」没有用，因为 `reconcile()` 只找「券商有、登记表完全没有」的
    孤儿仓，而这笔仓登记表里**是有的**（只是状态没更新）。恰恰因为系统"认识"它，
    对账才认定没事可做 —— 正好卡在中间的盲区。

    这一步补的就是那个盲区：拿每一笔在途仓的原始买单去问券商成交了没，成交了就
    按 watchdog 同一套逻辑（`_apply_entry_fill`）落地，避免两条路径口径不一致。

    **查不到不得臆断**：网络抖动 ≠ 成交，失败就保持 pending，下一次对账或
    watchdog 恢复后再核。
    """
    from app.watchdog_order_sync import _apply_entry_fill

    resolved: list[dict] = []
    for pos in REGISTRY.active():
        if pos.status != "pending":
            continue
        try:
            order = ec.get_order(pos.order_id)
        except Exception as e:  # noqa: BLE001 — 保持 pending，绝不猜成交
            logger.warning(json.dumps({
                "event": "reconcile_pending_fill_query_error",
                "order_id": pos.order_id,
                "contract": pos.option_code,
                "error": str(e),
            }, ensure_ascii=False))
            continue
        # 上面那发 REST 要走一个网络往返（实测 200~500ms），期间 watchdog 完全可能
        # 已经把同一笔成交落地了。此时再调 `_apply_entry_fill` 会走「非 pending」
        # 分支并**照样返回 True** —— 不重新看一眼状态就会谎报一笔「对账补上的成交」。
        if pos.status != "pending":
            continue
        if not _apply_entry_fill(pos, order):
            continue  # 券商侧确实还在排队 / 没有成交量与成交价
        resolved.append({
            "option_code": pos.option_code,
            "qty": pos.qty,
            "entry_mid": str(pos.entry_mid),
        })
    if resolved:
        logger.info(json.dumps({
            "event": "reconcile_pending_fills_resolved",
            "codes": [r["option_code"] for r in resolved],
        }, ensure_ascii=False))
    return resolved


def reconcile(source) -> dict:
    """按本程序账本恢复重启后丢失的在管持仓。

    Alpaca position 是账户级同合约聚合仓，无法证明其中每一张由哪个程序买入。因此优先
    用带 ``ttt-`` client_order_id 的逐笔成交链计算本程序净持仓；兼容旧数据时才回退到
    ``ttt_trades.status=open``。券商聚合数量多出的部分仍按外部仓忽略。

    做两件事，顺序不能反：
      1. `_resolve_pending_entry_fills`：登记表里有、但状态还停在「未成交」的仓
         —— 券商说成交了就转成持仓（2026-08-25 那次界面空窗就是卡在这）；
      2. 孤儿仓找回：券商有、登记表完全没有的仓（重启清空内存态后的常见形态）。
    """
    ec = AlpacaTrading()
    resolved_pending = _resolve_pending_entry_fills(ec)
    # closed 只是内存历史，不代表当前仍在管理券商仓位；否则同合约残仓无法对账找回。
    known = {p.option_code for p in REGISTRY.active()}
    broker_positions = [bp for bp in ec.broker_positions() if _OCC_RE.match(bp.symbol)]
    orphans = [bp for bp in broker_positions if bp.symbol not in known]
    if not orphans:
        return {
            "recovered": [],
            "resolved_pending": resolved_pending,
            "still_broker_positions": len(broker_positions),
        }

    trade_rows = trade_recorder.load_today()
    trade_rows_by_id = {
        str(row.get("order_id")): row
        for row in trade_rows
        if row.get("order_id")
    }
    candidates = {
        row.get("option_code"): {
            "db_row": row,
            "owned_qty": int(row.get("qty") or 0),
            "event_ledger": False,
            "event_entry_mid": None,
        }
        for row in trade_rows
        if (
            row.get("status") == "open"
            and row.get("option_code")
            and int(row.get("qty") or 0) > 0
        )
    }
    owned_lifecycles = _owned_event_lifecycles(
        trade_recorder.load_order_events(order_session_window()),
    )
    for root_id, lifecycle in owned_lifecycles.items():
        if lifecycle["net_qty"] <= 0:
            continue
        db_row = trade_rows_by_id.get(root_id)
        # 事件链和 ttt_trades 根行双重匹配，避免单张损坏/伪造事件扩大接管范围。
        if (
            db_row is None
            or db_row.get("option_code") != lifecycle["option_code"]
        ):
            continue
        candidates[lifecycle["option_code"]] = {
            "db_row": db_row,
            "owned_qty": lifecycle["net_qty"],
            "event_ledger": True,
            "event_entry_mid": lifecycle["entry_mid"],
        }

    tpl = get_store().active()
    recovered = []
    ignored = []
    for bp in orphans:
        m = _OCC_RE.match(bp.symbol)
        candidate = candidates.get(bp.symbol)
        broker_qty = int(float(bp.qty))
        if not candidate:
            ignored.append({
                "option_code": bp.symbol,
                "broker_qty": broker_qty,
                "reason": "本程序数据库无 open 生命周期，按外部仓忽略",
            })
            continue
        db_row = candidate["db_row"]
        owned_qty = int(candidate["owned_qty"])
        if owned_qty <= 0:
            ignored.append({
                "option_code": bp.symbol,
                "broker_qty": broker_qty,
                "reason": "本程序数据库持仓数量为 0，拒绝接管",
            })
            continue
        direction = "long" if m.group(2) == "C" else "short"
        # Alpaca position.qty 是同账户同合约聚合量。若它大于本程序账本数量，多出的只能
        # 视为外部数量，绝不能并进 watchdog；若更小则说明外部已卖/券商真仓减少，向下封顶。
        qty = min(owned_qty, broker_qty)
        entry_mid = Decimal(str(
            db_row.get("entry_mid")
            if db_row.get("entry_mid") is not None
            else (
                candidate["event_entry_mid"]
                if candidate["event_entry_mid"] is not None
                else bp.avg_entry_price
            )
        ))
        entry_time = db_row.get("entry_time") or datetime.now(timezone.utc)
        order_id = str(db_row["order_id"])
        recovered_pos = ManagedPosition(
            option_code=bp.symbol, ticker=m.group(1), direction=direction,
            qty=qty, entry_mid=entry_mid, entry_time=entry_time,
            order_id=order_id, status="open",
            realized_pnl=Decimal(str(db_row.get("realized_pnl") or 0)),
            stop_pct=tpl.stop_pct, dollar_line=tpl.max_loss_per_trade,
            stop_mode=_stop_mode_from_snapshot(db_row.get("template_snapshot")),
            max_hold_s=float(tpl.max_hold_mins) * 60,
            entry_filled_qty=qty, entry_fill_mid=entry_mid,
        )
        REGISTRY.record(recovered_pos)
        _reattach_recovered_close(recovered_pos, ec)
        db_qty = int(db_row.get("qty") or 0)
        if (
            candidate["event_ledger"]
            and (db_row.get("status") != "open" or db_qty != qty)
        ):
            trade_recorder.record_reconciled_position(
                order_id,
                qty,
                entry_mid,
            )
        elif qty != db_qty:
            trade_recorder.record_position_sync(order_id, qty, entry_mid)
        recovered.append({
            "option_code": bp.symbol,
            "qty": qty,
            "entry_mid": str(entry_mid),
            "matched_order": True,
            "matched_trade": True,
            "broker_qty": broker_qty,
            "ignored_external_qty": max(0, broker_qty - qty),
        })
    logger.info(json.dumps({"event": "reconcile_recovered",
                            "codes": [r["option_code"] for r in recovered]}, ensure_ascii=False))
    return {
        "recovered": recovered,
        "resolved_pending": resolved_pending,
        "ignored": ignored,
        "still_broker_positions": len(broker_positions),
    }


def recover_positions_on_startup() -> dict:
    """启动时自动把券商实持仓恢复进 REGISTRY。**只自动化调用时机，判据全部复用 reconcile()**
    （数据库无记录的外部仓仍拒绝接管、券商聚合量多出的部分仍按外部仓忽略）。

    不这样做的后果（2026-08-14 23:49 实测）：带仓重启后 watchdog 对着**空** REGISTRY 巡检，
    `trade_rest_reconciled` 连报五次 `active_positions: 0`，止损/超时/追踪失明 **2 分 35 秒**，
    直到人工 `POST /api/reconcile`；当晚 QQQ 的 15 分钟超时因此晚触发 3 分 40 秒。
    根因是启动路径调的是 `reconcile_pending_journal()`（只对账在途未决订单），
    而能 `REGISTRY.record()` 恢复持仓的 `reconcile()` 此前**仅**挂在 `POST /api/reconcile` 上。
    详见 docs/reports/2026-08-14-未修缺陷清单.md §1。

    「恢复完成前不得开新仓」由**调用位置**保证：本函数在 lifespan 启动段被 await，
    而 uvicorn 在 startup 跑完前不 accept 任何请求，因此无需另加开仓闸。

    失败只告警不抛：恢复不了很危险，但服务整个起不来更危险 —— 与相邻的 journal 重建、
    pending 对账同口径。
    """
    from app.services.data_source_registry import current_source
    from app import exec_entry

    source_name = "unknown"
    try:
        source = current_source()
        source_name = source.name
        if not exec_entry._execution_enabled(source_name):
            # mock 回放等不可执行源不得碰券商，跳过而非抛错，否则 mock 根本起不来。
            logger.info(json.dumps({
                "event": "startup_position_recovery_skipped",
                "source": source_name,
            }, ensure_ascii=False))
            return {"skipped": True, "source": source_name}
        return reconcile(source)
    except Exception as exc:  # noqa: BLE001 — 恢复失败不得阻断启动
        logger.warning(json.dumps({
            "event": "startup_position_recovery_error",
            "source": source_name,
            "error": str(exc),
        }, ensure_ascii=False))
        return {"error": str(exc), "source": source_name}


def reconcile_pending_journal() -> dict:
    """启动时核对 JOURNAL 里 pending 买单是否在 Alpaca 侧仍真实在途（修 07-23 幽灵订单）。

    背景：REGISTRY 是纯内存态、重启即清，但 JOURNAL 会从 ttt_trades 重建（rebuild_from_trades），
    把库里仍 status=pending 的行原样重建成「待成交」——若这行本就是孤儿（进程重启丢失/测试误写
    真库的假单），券商侧要么根本没有这笔单、要么早已终态，本地却永远显示待成交且撤不掉
    （cancel() 靠 REGISTRY.by_order_id 找不到，404）。此处只动 JOURNAL 展示 + DB 落库状态，
    不碰 REGISTRY/持仓（真实持仓找回见既有 POST /api/reconcile）。
    """
    pending_buys = [ev for ev in JOURNAL._events.values() if ev.side == "buy" and ev.status == "pending"]
    if not pending_buys:
        return {"checked": 0, "resolved": 0}
    ec = AlpacaTrading()
    resolved = 0
    for ev in pending_buys:
        try:
            order = ec.get_order(ev.order_id)
        except Exception as e:  # noqa: BLE001
            if _is_order_not_found(e):
                # 券商确认查无此单（404，如测试污染的假 order_id）→ 视为从未真正在途
                JOURNAL.mark_cancelled(
                    ev.order_id,
                    fail_reason="Alpaca 查无此订单，本地历史在途记录已清理",
                    cancel_origin="unknown",
                )
                trade_recorder.record_cancelled(ev.order_id)
                resolved += 1
            else:
                # 网络类异常（超时/断网/代理故障）≠ 查无此单：保留 pending，下次启动/巡检再核，
                # 否则一次网络抖动就会把真实在途买单误标已撤、随后券商成交而系统失去跟踪
                logger.warning(json.dumps({"event": "reconcile_get_order_error",
                                           "order_id": ev.order_id, "error": str(e)}, ensure_ascii=False))
            continue
        status = (order.get("status") or "").lower()
        if status == "filled":
            JOURNAL.mark_filled(ev.order_id)
            trade_recorder.record_filled(ev.order_id)
            resolved += 1
        elif status in ("canceled", "cancelled", "expired", "rejected", "done_for_day"):
            JOURNAL.mark_cancelled(ev.order_id, broker_status=status)
            trade_recorder.record_cancelled(ev.order_id)
            resolved += 1
        # 其余（new/accepted/pending_new 等）= 券商侧确实仍在排队，保留 pending
    return {"checked": len(pending_buys), "resolved": resolved}
