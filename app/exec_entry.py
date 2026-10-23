"""开仓、加仓与合约预览执行。

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

from app.integrations.alpaca.trading import AlpacaTrading
from app.order_gate import (
    active_config, evaluate_with_context, existing_exposure, normalize_direction,
)
from app.order_pricing import build_limit_order, price_for
from app.buy_intent import BUY_INTENTS
from app.order_journal import JOURNAL
from app.position_registry import ManagedPosition, REGISTRY
from app.services.market_context import (
    build_market_context,
    entry_plan,
)
from app.validator.constants import CONTRACT_MULTIPLIER
from app.validator.core import position_ceiling
from app.templates import get_store
from app import trade_recorder

from app.core.dailylogger import get_logger

logger = get_logger("tt.gate")

_EXEC_SOURCES = {"live", "watcher"}
# execute_submit 的同标的+方向去重（防抖：连点买入在 execute_run 跑完前只认第一次，返回同一
# intent_id，不会各起一个 execute_run → 各提交一次真实买单）。原子性要求与 _CLOSE_SUBMIT_LOCK 同理。
_BUY_SUBMIT_LOCK = threading.Lock()
_BUY_INFLIGHT: dict[tuple[str, str], str] = {}   # (ticker, direction) -> intent_id（尚在 execute_run 中）
# 防止前一笔 execute_run 已完成、下一次点击又选中同一 OCC 合约时，
# 两笔券商订单都成交而后者覆盖 PositionRegistry 中的前者。
_OPEN_CONTRACT_LOCK = threading.Lock()


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


def _quote_at_submit(source, sel) -> tuple[Decimal, Decimal, str]:
    """取**提交这一刻**的盘口，返回 (bid, ask, 来源)。来源 = ws / rest / snapshot。

    `sel.bid/sel.ask` 是闸门评估时的期权链快照。从评估到提交之间还隔着选约的期权链
    拉取，0DTE 上一两秒就足以让 ask 抬走一整档 —— 2026-08-03 实测 8 笔首单因此挂在
    过期 ask 上 5 秒未成被撤，重试挂高一档立即成交
    （见 docs/reports/2026-08-03-订单耗时0ms与首单必撤诊断.md）。

    三级降级，越靠前越快：

    1. **ws** —— `option_candidates` 协调器已把每个 (ticker, direction) 的选中合约推进
       Option WS scope（`option_candidates.py:179 / :284`，PRIORITY_CANDIDATE），盘口就在
       内存里，命中即 0 网络往返。要求「当前连接已 ACK 订阅」+ 年龄 ≤ 1s，宁可不命中。
    2. **rest** —— `option_quote(fresh=True)`，即本函数 2026-08-03 起的原有行为。
       2026-08-11 实测 quote_ms 中位 276ms、p90 3598ms、最大 6721ms。
    3. **snapshot** —— 选约快照。

    **任何一级拿不到都绝不拦下单**：报价接口抖动不该把入场掐掉 —— 挂个可能偏低的价
    最多是不成交（5 秒后照常撤单），而拒单是直接丢掉这次机会。2026-08-10 那次把第 1 级
    做成 fail-closed（没有已确认 WS 盘口就拒单）的实验当天打出 9 笔 trade_order_rejected
    并被整体回退，原因见 docs/reports/2026-08-11-下单卡顿与拦截诊断.md §A —— 点击路径的
    合约与协调器 15s 刷一次的选中合约不保证同一个 OCC，换档瞬间必然没有 confirmed 报价。
    """
    # ① WS 内存盘口
    try:
        from app.option_stream import fresh_entry_quote

        hit = fresh_entry_quote(sel.option_code)
        if hit is not None:
            bid, ask = hit
            return bid, ask, "ws"
    except Exception as e:  # noqa: BLE001 — WS 侧任何问题都只是「没命中」，继续降级
        logger.warning(json.dumps(
            {"event": "entry_quote_ws_error", "contract": sel.option_code,
             "error": str(e)[:200]}, ensure_ascii=False))

    # ② REST
    alpaca = getattr(source, "alpaca", None)
    quote_reader = getattr(alpaca, "option_quote", None)
    if quote_reader is None:
        return sel.bid, sel.ask, "snapshot"
    try:
        quote = quote_reader(sel.option_code, fresh=True)
    except Exception as e:  # noqa: BLE001 — 见 docstring 的降级纪律
        logger.warning(json.dumps(
            {"event": "entry_quote_refresh_error", "contract": sel.option_code,
             "error": str(e)[:200]}, ensure_ascii=False))
        return sel.bid, sel.ask, "snapshot"
    if not quote:
        logger.warning(json.dumps(
            {"event": "entry_quote_refresh_empty", "contract": sel.option_code},
            ensure_ascii=False))
        return sel.bid, sel.ask, "snapshot"
    bid, ask = quote
    return bid, ask, "rest"


def execute(source, req) -> dict:
    """⑤ 下单执行：过闸门 → 若放行，提交所选 0DTE 合约的可成交限价单（仅可执行档：watcher/live）。

    数量由 validator 计算，合约由 ③ 选。仅在可执行数据源（watcher/live）下可执行（真实券商）。
    夜盘期权闭市 → 提交会被券商拒（不建仓），验证接线触达券商；盘中方能成交。
    """
    direction = normalize_direction(req.direction)
    tpl = get_store().active()
    # 提交前四段网络调用的耗时（2026-08-11 诊断）：闸门 / 提交价刷新 / 持仓归属核对 /
    # 券商提交。券商侧 submitted→filled 只要 55–500ms，慢的全在这四段里，此前无埋点。
    # 闸门内部的细分由 market_context 的 entry_context_timing 另行给出。
    stage_ms = {"gate_ms": 0.0, "quote_ms": 0.0, "owner_check_ms": 0.0, "submit_ms": 0.0}
    _stage_started = time.perf_counter()
    try:
        # 数据源(watcher)在离线/不可交易/极端波动时会抛异常 → 优雅降级为拒单（与 /api/order 一致），
        # 保留清晰纪律原因，避免穿透成不透明 500。
        # evaluate_with_context 同时回传评估用的 MarketContext：闸门选中的完整合约（ctx.selected）
        # 直接复用于下单，免 entry_plan 再拉一遍（Alpaca 期权链 + UW delta），验/提交同一份合约。
        result, ctx = evaluate_with_context(
            req.ticker,
            direction,
            provider=lambda symbol, side: build_market_context(source, symbol, side),
            config=active_config(),
            qty=req.qty,
        )
        stage_ms["gate_ms"] = (time.perf_counter() - _stage_started) * 1000
    except Exception as e:  # noqa: BLE001 — 数据源不可用/拦截 → 拒单而非 500
        stage_ms["gate_ms"] = (time.perf_counter() - _stage_started) * 1000
        logger.warning(json.dumps({"event": "execute_gate_error", "source": source.name,
                                   "gate_ms": round(stage_ms["gate_ms"], 1),
                                   "error": str(e)}, ensure_ascii=False))
        trade_recorder.audit(
            "trade_gate_rejected", ticker=req.ticker.upper(), direction=direction,
            qty=getattr(req, "qty", None), rule="datasource", reason=str(e), source=source.name)
        return {"executed": False, "gate": {"allowed": False, "reason": f"数据源({source.name})不可用: {e}", "rule": "datasource", "qty": 0}}
    # skipped 透出到前端与审计日志：allowed=True 里混着「真判过」和「缺输入没判」两种，
    # 不分开的话，一道空转的闸在日志里跟一道真在把关的闸长得一模一样。
    gate = {"allowed": result.allowed, "reason": result.reason, "rule": result.rule,
            "qty": result.qty, "skipped": list(result.skipped)}
    if not result.allowed:
        # 带上 gate_ms：慢样本里多数最终是拒单（08-10 21:43:47 IONQ 耗时 7.5s 后被拒），
        # 只在成功路径记耗时会漏掉大半证据。
        trade_recorder.audit(
            "trade_gate_rejected", ticker=req.ticker.upper(), direction=direction,
            qty=result.qty, rule=result.rule, reason=result.reason, source=source.name,
            gate_ms=round(stage_ms["gate_ms"], 1))
        return {"executed": False, "gate": gate}
    sel = getattr(ctx, "selected", None)
    if sel is None:
        trade_recorder.audit(
            "trade_order_rejected", ticker=req.ticker.upper(), direction=direction,
            qty=result.qty, stage="contract_selection", reason="无合格合约", source=source.name)
        return {"executed": False, "gate": gate, "error": "无合格合约"}
    # ⑩ 价格类型：默认卖一(ask)=现行可成交买入；limit 需带 limit_price
    ptype = req.price_type or "ask"
    lp = Decimal(req.limit_price) if req.limit_price not in (None, "") else None
    _quote_started = time.perf_counter()
    bid, ask, quote_source = _quote_at_submit(source, sel)
    # 保持 quote_fresh 的原语义（拿到的是实时盘口，不是选约快照），
    # 另出 quote_source 才能看出 WS 优先层的命中率——只看 quote_ms 分不清
    # 「命中 WS 所以快」和「REST 这次正好不慢」。
    quote_fresh = quote_source in ("ws", "rest")
    stage_ms["quote_ms"] = (time.perf_counter() - _quote_started) * 1000
    try:
        limit = price_for("buy", ptype, bid, ask, limit_price=lp)
    except (ValueError, ArithmeticError) as e:
        trade_recorder.audit(
            "trade_order_rejected", ticker=req.ticker.upper(), direction=direction,
            contract=sel.option_code, qty=result.qty, stage="pricing",
            reason=str(e), source=source.name)
        return {"executed": False, "gate": gate, "error": f"价格类型/限价无效: {e}",
                "delta_source": getattr(sel, "delta_source", "approx")}
    order_req = build_limit_order(
        sel.option_code, "buy", result.qty, limit, extended_hours=False)
    with _OPEN_CONTRACT_LOCK:
        existing = REGISTRY.get(sel.option_code)
        if existing is not None and existing.status in ("pending", "open", "closing"):
            reason = "同一合约已有在途订单或持仓；如需增加数量，请使用加仓"
            logger.warning(json.dumps({
                "event": "execute_duplicate_contract_rejected",
                "contract": sel.option_code,
                "existing_order_id": existing.order_id,
                "existing_status": existing.status,
            }, ensure_ascii=False))
            trade_recorder.audit(
                "trade_order_rejected", ticker=req.ticker.upper(), direction=direction,
                contract=sel.option_code, side="buy", qty=result.qty,
                stage="duplicate_contract", reason=reason, source=source.name)
            return {
                "executed": False,
                "gate": gate,
                "contract": sel.option_code,
                "error": reason,
                "duplicate_contract": True,
                "delta_source": getattr(sel, "delta_source", "approx"),
            }
        # 归属核对：券商侧持仓按 symbol 合并成**一条**，没有 client_order_id 可分
        # （只有订单才带 ttt- / sos- 前缀）。REGISTRY 无登记却在券商查到该合约，那就是
        # 人工仓、或 SOS 时期残留的 swing 仓（SOS 已于 2026-08-15 移出本项目）——买进去
        # 之后本系统按自己的账本算平仓数量，会把不属于自己的张数一并卖掉，而风控不会
        # 报错，只当张数都归自己。残留 swing 合约熬到到期日当天就成了 0DTE 的可选标的，
        # 撞上的概率并不低。
        _owner_started = time.perf_counter()
        try:
            broker_held = AlpacaTrading().position_qty(sel.option_code)
        except Exception as e:  # noqa: BLE001 — 核不清归属就不开仓，宁可少做一单
            reason = f"无法核对该合约在券商的持仓归属，拒绝开仓: {e}"
            logger.warning(json.dumps({
                "event": "execute_position_owner_check_failed",
                "contract": sel.option_code, "error": str(e)}, ensure_ascii=False))
            trade_recorder.audit(
                "trade_order_rejected", ticker=req.ticker.upper(), direction=direction,
                contract=sel.option_code, side="buy", qty=result.qty,
                stage="owner_check", reason=reason, source=source.name)
            return {"executed": False, "gate": gate, "contract": sel.option_code,
                    "error": reason,
                    "delta_source": getattr(sel, "delta_source", "approx")}
        if broker_held > 0:
            reason = (f"该合约在券商已有非本系统持仓（{broker_held} 张），"
                      "拒绝开仓以防两套系统卖同一批张数")
            logger.warning(json.dumps({
                "event": "execute_foreign_position_rejected",
                "contract": sel.option_code, "broker_qty": broker_held},
                ensure_ascii=False))
            trade_recorder.audit(
                "trade_order_rejected", ticker=req.ticker.upper(), direction=direction,
                contract=sel.option_code, side="buy", qty=result.qty,
                stage="foreign_position", reason=reason, source=source.name)
            return {"executed": False, "gate": gate, "contract": sel.option_code,
                    "error": reason, "foreign_position": True,
                    "delta_source": getattr(sel, "delta_source", "approx")}

        stage_ms["owner_check_ms"] = (time.perf_counter() - _owner_started) * 1000
        _submit_started = time.perf_counter()
        try:
            order = AlpacaTrading().submit(order_req)
        except Exception as e:  # noqa: BLE001 — 券商拒单（如闭市）如实返回
            logger.warning(json.dumps({"event": "execute_submit_error", "contract": sel.option_code, "error": str(e)}, ensure_ascii=False))
            trade_recorder.audit(
                "trade_order_rejected", ticker=req.ticker.upper(), direction=direction,
                contract=sel.option_code, side="buy", qty=result.qty, limit_price=limit,
                stage="broker_submit", reason=str(e), source=source.name)
            return {"executed": False, "gate": gate, "contract": sel.option_code, "submit_error": str(e),
                    "delta_source": getattr(sel, "delta_source", "approx")}
        stage_ms["submit_ms"] = (time.perf_counter() - _submit_started) * 1000
        # entry_mid 是成交价回填前的临时成本基准（美元止损线据此起算），
        # 用刷新后的 mid 比用选约快照更接近真实成本。
        entry_mid = (bid + ask) / 2
        mp = ManagedPosition(
            option_code=sel.option_code, ticker=req.ticker.upper(), direction=direction,
            qty=result.qty, entry_mid=entry_mid, entry_time=datetime.now(timezone.utc),
            order_id=order["id"], status="pending",
            stop_pct=tpl.stop_pct, dollar_line=tpl.max_loss_per_trade,
            stop_mode=getattr(req, "stop_mode", "percent"),
            max_hold_s=float(tpl.max_hold_mins) * 60)
        REGISTRY.record(mp)
    JOURNAL.submitted(order["id"], req.ticker, sel.option_code, "buy", result.qty, entry_mid,
                      reason="手动开仓", price_type=ptype, limit_price=limit,
                      origin="user", action="open",
                      client_order_id=order.get("client_order_id"))  # ⑦ 订单流：入场单入账
    trade_recorder.record_submitted(mp, source.name, _entry_snapshot(req))  # ⑧ 入场落库
    # pre_submit_ms 是「点击到券商收到单」的那段，与券商自身的 submit_ms 分开看：
    # 08-10 实测券商 submitted→filled 仅 55–500ms，慢的全在 pre_submit_ms 里。
    timing = {key: round(value, 1) for key, value in stage_ms.items()}
    timing["pre_submit_ms"] = round(
        stage_ms["gate_ms"] + stage_ms["quote_ms"] + stage_ms["owner_check_ms"], 1)
    # 快照价与提交价一并记下：事后复盘「首单为什么没成」只能靠这两个数的差。
    logger.info(json.dumps({"event": "execute", "trade_id": order["id"],
                            "contract": sel.option_code, "qty": result.qty, "limit": str(limit),
                            "quote_fresh": quote_fresh,
                            "quote_source": quote_source,
                            "snapshot_bid": str(sel.bid), "snapshot_ask": str(sel.ask),
                            "submit_bid": str(bid), "submit_ask": str(ask),
                            **timing,
                            "delta_source": getattr(sel, "delta_source", "approx"), "order": order}, ensure_ascii=False))
    return {"executed": True, "gate": gate, "contract": sel.option_code, "limit": str(limit), "order": order,
            "delta_source": getattr(sel, "delta_source", "approx")}


def _entry_snapshot(req) -> dict:
    """入场落库快照 = 当前生效模板 + 下单自查三问（2026-09-01）。

    自查答案塞进 ``ttt_trades.template_snapshot`` 这个已有的 jsonb 列，**不新开表也
    不加列**：那一列本来记的就是「这一单当时的设置长什么样」，自查是同一类东西。

    没带 ``self_check``（脚本 / `/api/execute` 同步端点 / 旧前端）→ 不写这个键，
    读的时候按缺失处理，不要伪造一个「三个都答否」的假记录。
    """
    snap = dict(trade_recorder.template_snapshot() or {})
    self_check = getattr(req, "self_check", None)
    if self_check is not None:
        snap["self_check"] = self_check.model_dump()
    return snap


def execute_submit(source, req, route_started: float | None = None) -> dict:
    """⑤ 买入受理（快、同步）：建受理号即返回；真正 gate/选约/提交丢后台 execute_run 跑。

    2026-07-23 簇B #2：execute() 本身在 HTTP 请求里同步等 gate（数据源往返）+ 选约 + 券商提交，
    最坏 ~2s 转圈。gate 判定前还没有真实持仓/订单可挂状态，故新开一个轻量过渡态 BuyIntent
    （不是 ManagedPosition/OrderEvent）供前端轮询。

    同标的+方向若已有 execute_run 在跑（连点/网络重试）→ 幂等返回同一 intent_id，不重复起
    worker、不重复提交真实买单（同 close_submit 的并发防护同理）。
    """
    if not _execution_enabled(source.name):
        raise HTTPException(status_code=400, detail="下单执行需切到 watcher（或 live）数据源")
    # route_started 是**路由函数入口**的 perf_counter。事件链此前从本函数开头才起步，
    # 而本函数跑在 asyncio.to_thread 的工作线程里 —— 中间的调度排队从来没被量过。
    # 事件循环与默认线程池是全局共享的（_positions_loop 0.5s、TradingStateHub.run 0.5s、
    # option_candidates._discover、WS hub 广播，以及 execute_run 自己最长占 8.5s），
    # 负载下这段是不是仍然可忽略，需要数据而不是假设。同为 perf_counter，无跨钟问题。
    # 没传就不带这个字段——内部调用方（测试/脚本）没有路由入口时刻，
    # 发一个 route_wait_ms:null 只会污染事件。
    timing = (
        {} if route_started is None
        else {"route_wait_ms": round((time.perf_counter() - route_started) * 1000, 1)}
    )
    direction = normalize_direction(req.direction)
    key = (req.ticker.upper(), direction)
    with _BUY_SUBMIT_LOCK:
        existing_id = _BUY_INFLIGHT.get(key)
        if existing_id is not None:
            trade_recorder.audit(
                "trade_intent_accepted", intent_id=existing_id, ticker=req.ticker.upper(),
                direction=direction, source=source.name, status="processing",
                duplicate=True, **timing)
            return {"accepted": True, "intent_id": existing_id, "status": "processing", "spawn": False}
        intent = BUY_INTENTS.create(req.ticker, direction)
        _BUY_INFLIGHT[key] = intent.intent_id
    trade_recorder.audit(
        "trade_intent_accepted", intent_id=intent.intent_id, ticker=req.ticker.upper(),
        direction=direction, source=source.name, status="processing",
        duplicate=False, **timing)
    return {"accepted": True, "intent_id": intent.intent_id, "status": "processing", "spawn": True}


def execute_run(source, req, intent_id: str) -> None:
    """⑤ 买入后台执行：跑 execute()（原样，未改一字）→ 按结果回填 BuyIntent。

    在后台线程跑（不占 HTTP）。任何异常都转成 intent status=error，不得让后台任务静默崩溃。
    """
    direction = normalize_direction(req.direction)
    key = (req.ticker.upper(), direction)
    try:
        result = execute(source, req)
    except Exception as e:  # noqa: BLE001 — 后台失败不得崩溃：回填 intent 而非抛出
        logger.warning(json.dumps({"event": "execute_run_error", "ticker": req.ticker.upper(),
                                   "error": str(e)}, ensure_ascii=False))
        BUY_INTENTS.resolve(intent_id, status="error", reason=str(e))
        trade_recorder.audit(
            "trade_intent_resolved", intent_id=intent_id, ticker=req.ticker.upper(),
            direction=direction, status="error", reason=str(e), source=source.name)
        return
    finally:
        with _BUY_SUBMIT_LOCK:
            if _BUY_INFLIGHT.get(key) == intent_id:
                del _BUY_INFLIGHT[key]
    if result.get("executed"):
        BUY_INTENTS.resolve(intent_id, status="submitted", result=result)
        order = result.get("order") if isinstance(result.get("order"), dict) else {}
        trade_recorder.audit(
            "trade_intent_resolved", intent_id=intent_id, order_id=order.get("id"),
            ticker=req.ticker.upper(), direction=direction, contract=result.get("contract"),
            status="submitted", source=source.name)
    else:
        gate = result.get("gate") or {}
        reason = gate.get("reason") or gate.get("rule") or result.get("error") or "不可下单"
        BUY_INTENTS.resolve(intent_id, status="rejected", reason=reason, result=result)
        trade_recorder.audit(
            "trade_intent_resolved", intent_id=intent_id, ticker=req.ticker.upper(),
            direction=direction, status="rejected", rule=gate.get("rule"),
            reason=reason, source=source.name)



_PREVIEW_CACHE: dict[tuple[str, str, str], tuple[float, dict]] = {}

def contract_preview(source, ticker: str, direction: str) -> dict:
    """③ 合约预览：下单前显示将要选中的真实 0DTE 合约（与 /api/execute 同一选择逻辑，只读不下单）。

    仅可执行档（watcher/live）可用；选约结果短缓存 1 秒，盘口每次缓存失效后强制取最新报价。
    """
    if not _execution_enabled(source.name):
        return {"available": False}
    direction = normalize_direction(direction)
    cache_key = (source.name, ticker.upper(), direction)
    now = time.monotonic()
    hit = _PREVIEW_CACHE.get(cache_key)
    if hit and (now - hit[0]) < 1.0:
        return hit[1]

    try:
        sel = entry_plan(source, ticker, direction)
    except Exception as e:  # noqa: BLE001 — 预览失败不拦下单链路，仅前端显示占位
        return {"available": False, "error": str(e)}
    if sel is None:
        res = {"available": False}
    else:
        bid, ask = sel.bid, sel.ask
        # 预览是**每张卡片持续调用**的背景负载，打的正是那个已经在限流的行情 API
        # （08-11 三次 alpaca_rate_limited 全来自 cands-adaptive 期权链）。
        # WS 有现价就别再打 REST；与下单路径同一口径（fresh_entry_quote）。
        ws_quote = None
        try:
            from app.option_stream import fresh_entry_quote

            ws_quote = fresh_entry_quote(sel.option_code)
        except Exception as e:  # noqa: BLE001 — WS 侧问题只当没命中
            logger.warning(json.dumps(
                {"event": "contract_preview_ws_error",
                 "contract": sel.option_code, "error": str(e)[:200]},
                ensure_ascii=False))
        if ws_quote is not None:
            bid, ask = ws_quote
        else:
            quote_reader = getattr(source, "option_quote", None)
            if quote_reader is not None:
                try:
                    quote = quote_reader(sel.option_code, fresh=True)
                    if quote is not None:
                        bid, ask = quote
                except Exception as e:  # noqa: BLE001 — 预览报价失败可退回选约快照
                    logger.warning(json.dumps(
                        {"event": "contract_preview_quote_error",
                         "contract": sel.option_code, "error": str(e)},
                        ensure_ascii=False))
        mid = (bid + ask) / 2
        spread_pct = (ask - bid) / mid if mid > 0 else Decimal("1")
        res = {
            "available": True, "option_code": sel.option_code,
            "bid": str(bid), "ask": str(ask), "mid": str(mid),
            "spread_pct": str(spread_pct),
            "delta": None if sel.delta is None else str(sel.delta), "delta_source": sel.delta_source,
        }
    _PREVIEW_CACHE[cache_key] = (now, res)
    return res



_ADD_GATE_EXEMPT_RULES = frozenset({"contract", "position", "funds"})


def add_position(source, req) -> dict:
    """针对现有持仓进行加仓操作。只允许在有对应持仓时加仓，使用主控面板上的价格类型（如市价/卖一）。

    2026-07-25（审查 2.3）起接入闸门：加仓是**开新敞口**，此前它完全绕过 validator——
    不查熔断/冷却/开盘·午后禁入/震荡·反向·追高·距墙/资金/仓位上限，连「盯盘离线→下单硬锁」
    都对它无效，却又会计入今日交易次数。这是系统里最大的风控窟窿。
    """
    if not _execution_enabled(source.name):
        raise HTTPException(status_code=400, detail="加仓需切到 watcher（或 live）数据源")
    pos = REGISTRY.get(req.option_code)
    if pos is None:
        raise HTTPException(status_code=404, detail="未在本系统登记的持仓，拒绝加仓")

    # ④ 闸门：市场/账户/纪律各条规则（数据源不可用 → 拒单，与 gate_order 同款失安降级）
    cfg = active_config()
    try:
        result, ctx = evaluate_with_context(
            pos.ticker,
            pos.direction,
            provider=lambda symbol, side: build_market_context(source, symbol, side),
            config=cfg,
            qty=req.qty,
        )
    except Exception as e:  # noqa: BLE001 — 含"盯盘引擎未启动"硬锁
        logger.warning(json.dumps({"event": "add_gate_error", "contract": req.option_code,
                                   "error": str(e)}, ensure_ascii=False))
        trade_recorder.audit(
            "trade_gate_rejected", order_id=pos.order_id, ticker=pos.ticker,
            direction=pos.direction, contract=req.option_code, qty=req.qty,
            rule="datasource", reason=str(e), source=source.name, action="add")
        raise HTTPException(status_code=400, detail=f"加仓被拦截: {e}") from e
    if not result.allowed and result.rule not in _ADD_GATE_EXEMPT_RULES:
        trade_recorder.record_intercept(
            rule=result.rule, reason=result.reason, ticker=pos.ticker, direction=pos.direction,
            qty=req.qty, source_name=source.name, market_snapshot=trade_recorder.market_snapshot(ctx),
            template_snapshot=trade_recorder.template_snapshot())
        logger.info(json.dumps({"event": "add_gate_reject", "contract": req.option_code,
                                "rule": result.rule, "reason": result.reason}, ensure_ascii=False))
        raise HTTPException(status_code=400, detail=f"加仓被闸门拒绝: {result.reason}")

    ptype = req.price_type or "ask"
    lp = Decimal(req.limit_price) if req.limit_price not in (None, "") else None

    # 获取当前报价：可执行档（watcher/live）持有 AlpacaData，直接查该合约最新 bid/ask
    alpaca = getattr(source, "alpaca", None)
    quote = alpaca.option_quote(req.option_code, fresh=True) if alpaca is not None else None
    if not quote:
        raise HTTPException(status_code=400, detail=f"无法获取当前合约 {req.option_code} 报价，拒绝加仓")
    bid, ask = quote
    mid = (bid + ask) / 2

    # 仓位/资金守卫：用**持仓合约**的真实 mid 重算（闸门那份算的是它另选的 ATM，口径不对）。
    # existing_exposure 已含本仓，故 total 就是加完之后这只票上的总敞口。
    cost = mid * CONTRACT_MULTIPLIER * Decimal(req.qty)
    total = existing_exposure(pos.ticker) + cost
    ceiling = position_ceiling(cfg)
    if total > ceiling:
        raise HTTPException(
            status_code=400,
            detail=f"加仓后同标的持仓合计 ${total:.0f} 超上限（约 ${ceiling:.0f}）")
    if cost > ctx.account.buying_power:
        raise HTTPException(status_code=400, detail="加仓资金不足")

    try:
        limit = price_for("buy", ptype, bid, ask, limit_price=lp)
    except (ValueError, ArithmeticError) as e:
        raise HTTPException(status_code=400, detail=f"价格类型/限价无效: {e}")

    order_req = build_limit_order(
        req.option_code, "buy", req.qty, limit, extended_hours=False)

    try:
        order = AlpacaTrading().submit(order_req)
    except Exception as e:
        logger.warning(json.dumps({"event": "add_position_error", "contract": req.option_code, "error": str(e)}, ensure_ascii=False))
        trade_recorder.audit(
            "trade_add_order_rejected", parent_order_id=pos.order_id, ticker=pos.ticker,
            direction=pos.direction, contract=req.option_code, side="buy", qty=req.qty,
            limit_price=limit, price_type=ptype, reason=str(e), source=source.name)
        raise HTTPException(status_code=500, detail=f"加仓券商拒单: {e}") from e

    # submit 只代表券商受理，不代表成交。先记事件，持仓数量和成本必须等真实
    # filled_qty + filled_avg_price 到达后再更新，禁止用报价 mid/limit 乐观记账。
    JOURNAL.submitted(order["id"], pos.ticker, req.option_code, "buy", req.qty, mid,
                      reason="手动加仓", price_type=ptype, limit_price=limit,
                      origin="user", action="add",
                      client_order_id=order.get("client_order_id"),
                      parent_order_id=pos.order_id)
    # Mock/极快成交可能在 submit 响应里直接带终态。只有同时拿到真实成交量和均价
    # 才立即结算；字段不全则保持 pending，交给 TradingStream/REST 补齐。
    broker_status = str(order.get("status") or "").rsplit(".", 1)[-1].lower()
    try:
        filled_qty = int(float(order.get("filled_qty") or 0))
    except (TypeError, ValueError):
        filled_qty = 0
    filled_px = _as_dec(order.get("filled_avg_price"))
    if broker_status == "filled" and filled_qty > 0 and filled_px is not None:
        delta_qty, delta_px = JOURNAL.claim_fill_delta(
            str(order["id"]), filled_qty, filled_px)
        if delta_qty > 0 and delta_px is not None:
            REGISTRY.add_qty(req.option_code, delta_qty, delta_px)
            trade_recorder.record_add(pos.order_id, pos.qty, pos.entry_mid)
        JOURNAL.mark_filled(order["id"], price=filled_px, qty=filled_qty)
    trade_recorder.audit(
        "trade_add_order_submitted",
        order_id=order.get("id"),
        parent_order_id=pos.order_id,
        ticker=pos.ticker,
        direction=pos.direction,
        contract=req.option_code,
        side="buy",
        qty=req.qty,
        limit_price=limit,
        price_type=ptype,
        status=broker_status or "submitted",
        source=source.name,
    )
    logger.info(json.dumps({
        "event": "add_position", "contract": req.option_code, "add_qty": req.qty,
        "limit": str(limit), "order": order
    }, ensure_ascii=False))
    return {"executed": True, "order": order, "contract": req.option_code}
