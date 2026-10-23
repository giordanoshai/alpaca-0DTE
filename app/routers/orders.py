import asyncio
import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app import exec_close, exec_entry, exec_reconcile, trade_recorder
from app.buy_intent import BUY_INTENTS
from app.order_journal import JOURNAL
from app.services.data_source_registry import current_source
from app.order_gate import gate_order, normalize_direction

router = APIRouter(prefix="/api", tags=["orders"])

# 后台平仓任务集（防 asyncio.create_task 结果被 GC；close_run 自吞异常，无需回调记录）
_BG_CLOSE_TASKS: set = set()


def _schedule_bg(fn, *args) -> None:
    """把同步阻塞的后台工作丢线程池跑，不 await；存引用防 GC，完成即移除。"""
    task = asyncio.create_task(asyncio.to_thread(fn, *args))
    _BG_CLOSE_TASKS.add(task)
    task.add_done_callback(_BG_CLOSE_TASKS.discard)


class SelfCheck(BaseModel):
    """下单自查三问的答案（2026-09-01）。

    2026-09-01 删掉震荡 / 均线共振 / MACD 动能 / 追高四条自动闸之后，这三个判断交回
    给人做。前端在买入前弹窗，三个都答「否」才把提交按钮点亮。

    **后端刻意不强制**：三个都是 True 的请求照样受理。两个理由 ——
    `/api/order`、`/api/execute` 这两个同步端点和脚本调试还得能用；更要紧的是
    「我知道有风险但我就是要做」这条路堵死不如留档，堵死了人只会绕过前端直接开
    券商 App 下单，那才是真正什么都记不到。这里只负责**记录**。

    `dwell_ms` / `flips` 记的是弹窗停留了多久、开关一共拨了几次（含反悔）。这两个数
    比三个答案本身诚实：答案能骗自己，手速骗不了 —— 0.8 秒拨完三个的那批单和停了
    6 秒的那批，事后一比就知道这个弹窗还在起作用、还是已经退化成肌肉记忆了。
    """

    model_config = ConfigDict(extra="forbid")

    chop: bool                              # 震荡吗
    conflict: bool                          # 打架吗（1min MACD 线与 EMA 不同向）
    chase: bool                             # 追高吗
    dwell_ms: int = Field(default=0, ge=0)  # 弹窗停留毫秒
    flips: int = Field(default=0, ge=0)     # 开关拨动次数（含反悔）


class OrderRequest(BaseModel):
    """下单请求。

    ⑥（2026-07-16 用户决策）起放开数量：接受用户输入的 qty（默认 1），
    由 ④validator 守上限（数量×单手成本 ≤ 最大仓位×(1+容差)）。仍拒绝价格/strike 越权。
    ⑩ 价格类型（price_type/limit_price）仅执行端 /api/execute 用（下单闸门不涉及）。
    """

    model_config = ConfigDict(extra="forbid")

    ticker: str
    direction: str
    qty: int = Field(default=1, ge=1)
    stop_mode: str = "percent"          # percent / amount；只选择模式，阈值取当前纪律模板
    price_type: str | None = None      # limit / bid(买一) / ask(卖一) / mid(中间价) / market
    limit_price: str | None = None     # price_type=limit 时的限价（字符串→Decimal，守金额铁律）
    self_check: SelfCheck | None = None  # 下单自查三问；只记录不强制，见 SelfCheck
    # 本模型 extra="forbid"：前端多发一个键会 422，所以新字段必须在这里显式登记。

    @field_validator("direction")
    @classmethod
    def _valid_direction(cls, v: str) -> str:
        normalize_direction(v)  # 非法方向 → ValueError → 422
        return v

    @field_validator("stop_mode")
    @classmethod
    def _valid_stop_mode(cls, v: str) -> str:
        if v not in {"percent", "amount"}:
            raise ValueError("stop_mode 只能是 percent 或 amount")
        return v

    @field_validator("price_type")
    @classmethod
    def _valid_price_type(cls, v: str | None) -> str | None:
        if v is not None and v not in {"limit", "bid", "ask", "mid", "market"}:
            raise ValueError(f"未知价格类型: {v!r}（limit/bid/ask/mid/market）")
        return v


@router.post("/order")
async def place_order(req: OrderRequest) -> dict:
    """唯一下单闸门：过 ④validator，返回通行/拒绝 + 程序算出的张数。

    数量由 validator 计算，外部无从指定。市场/账户/合约取自 WATCHER。
    拒绝/放行均落结构化日志（拦截日志，见 order_gate.gate_order）。
    """
    source = current_source()
    # WATCHER 组装市场上下文可能包含外部取数 → 丢线程池，避免卡事件循环
    return await asyncio.to_thread(gate_order, source, req.ticker, req.direction, req.qty)


@router.post("/execute")
async def execute_order(req: OrderRequest) -> dict:
    """⑤ 下单执行：过闸门 → 若放行，提交所选 0DTE 合约的可成交限价单。

    数量由 validator 计算，合约由 ③ 选；交易终端固定使用 WATCHER 执行链。
    夜盘期权闭市 → 提交会被券商拒（不建仓），验证接线触达券商；盘中方能成交。

    同步端点，保留供脚本/直接调试用；前端已改走 /api/execute-submit（受理即返回）+
    /api/execute-status（轮询），见簇B #2。
    """
    source = current_source()
    if not exec_entry._execution_enabled(source.name):
        raise HTTPException(status_code=400, detail="下单执行需切到 watcher（或 live）数据源")
    return await asyncio.to_thread(exec_entry.execute, source, req)


@router.post("/execute-submit")
async def execute_submit_ep(req: OrderRequest) -> dict:
    """⑤ 买入受理：execute_submit 秒返回受理号 → execute_run 丢后台跑真正 gate/选约/提交。

    前端靠 /api/execute-status/{intent_id} 轮询回显最终结果（submitted/rejected/error）。
    """
    source = current_source()
    # 路由入口时刻交给工作线程，用来量 asyncio.to_thread 的调度排队（见 execute_submit）。
    route_started = time.perf_counter()
    result = await asyncio.to_thread(
        exec_entry.execute_submit, source, req, route_started)
    if result.get("spawn"):
        _schedule_bg(exec_entry.execute_run, source, req, result["intent_id"])
    return result


@router.get("/execute-status/{intent_id}")
async def execute_status_ep(
    intent_id: str,
    client_submit_ms: float | None = None,
    poll_attempt: int | None = None,
) -> dict:
    """⑤ 买入受理号状态查询（前端轮询用）。

    `client_submit_ms` 是**浏览器用自己的钟**量出的 `/api/execute-submit` 往返耗时
    （`performance.now()` 自减，无时钟偏移）。服务端拿不到点击时刻，这是补上
    「点击 → trade_intent_accepted」那段盲区的唯一办法。搭已有的轮询顺路带回，
    不新增端点、不新增请求；只在首次看到终态时落一条，一单一行。
    """
    it = BUY_INTENTS.get(intent_id)
    if it is None:
        raise HTTPException(status_code=404, detail="未知的下单受理号")
    if (
        client_submit_ms is not None
        and it.status != "processing"
        and not it.client_timing_logged
    ):
        it.client_timing_logged = True
        trade_recorder.audit(
            "client_entry_timing", intent_id=intent_id, ticker=it.ticker,
            direction=it.direction, status=it.status,
            client_submit_ms=round(float(client_submit_ms), 1),
            poll_attempt=poll_attempt)
    return it.to_dict()


@router.get("/positions")
async def list_positions() -> dict:
    """本系统登记的持仓（不含账户里非本系统的既有仓）+ 当前报价，供前端算实时盈亏。

    报价由 WATCHER 持有的 AlpacaData 获取；取不到时 quote=None，
    前端回退显示入场价（不拦、不报错，报价源抖动不得影响持仓可见性）。
    """
    return await asyncio.to_thread(exec_reconcile.list_positions, current_source())


@router.post("/reconcile")
async def reconcile() -> dict:
    """对真实券商账户做只读扫描，把 REGISTRY 里没有、但账户里真实存在的期权持仓找回来
    （重新登记进 REGISTRY，恢复 watchdog 止损/触墙止盈监控）。

    背景：REGISTRY/JOURNAL 是纯内存态（阶段0，未落库），服务热重载/进程重启会清空。
    若某笔仓是在重载前用本系统开的，重载后就会变成「券商里真实存在、系统完全不认识」
    的孤儿仓——不止界面上看不到，watchdog 也停止保护它了，止损/超时失明。
    找回时优先用该合约最近一笔"已成交买入单"的价格和时间做入场基准；找不到匹配订单
    （比如仓位本来就不是本系统开的）就退化为用当前市价近似、入场时间记为"现在"——
    止损/超时线会用当前生效模板的默认值，不是原始开仓时的参数（那个已经丢了，找不回，
    只能近似）。
    """
    source = current_source()
    if not exec_entry._execution_enabled(source.name):
        raise HTTPException(status_code=400, detail="对账需切到 watcher（或 live）数据源")
    return await asyncio.to_thread(exec_reconcile.reconcile, source)


@router.get("/contract-preview")
async def contract_preview(ticker: str, direction: str) -> dict:
    """③ 合约预览：下单前显示将要选中的真实 0DTE 合约（与 /api/execute 同一选择逻辑，只读不下单）。

    从 WATCHER 当前候选缓存读取；无实盘合约链时返回 available=false。
    """
    return await asyncio.to_thread(exec_entry.contract_preview, current_source(), ticker, direction)


@router.get("/orders")
async def list_orders() -> dict:
    """⑦ 当前 ET 自然日终态订单 + 全部跨日未终态订单，按三态分组。"""
    return JOURNAL.current_session(compact=True)


@router.post("/orders/{order_id}/cancel")
async def cancel_order_ep(order_id: str) -> dict:
    """⑦ 撤在途单：撤券商未成交单 + 登记与订单流置「已取消」。"""
    return await asyncio.to_thread(exec_close.cancel, current_source(), order_id)


class AddPositionRequest(BaseModel):
    """加仓请求。qty（加仓张数）；price_type 可选（默认卖一 ask=可成交买）。"""

    model_config = ConfigDict(extra="forbid")
    option_code: str
    qty: int = Field(ge=1)                        # 必须 >= 1
    price_type: str | None = None                 # limit/bid/ask/mid/market；None=默认卖一
    limit_price: str | None = None

    @field_validator("price_type")
    @classmethod
    def _valid_price_type(cls, v: str | None) -> str | None:
        if v is not None and v not in {"limit", "bid", "ask", "mid", "market"}:
            raise ValueError(f"未知价格类型: {v!r}（limit/bid/ask/mid/market）")
        return v


@router.post("/add")
async def add_position_ep(req: AddPositionRequest) -> dict:
    """针对现有持仓进行加仓操作。只允许在有对应持仓时加仓，使用主控面板上的价格类型（如市价/卖一）。"""
    return await asyncio.to_thread(exec_entry.add_position, current_source(), req)


class CloseRequest(BaseModel):
    """平仓请求。⑪ qty 可选（部分平仓，None=全平）；⑩ price_type 可选（默认买一 bid=可成交卖）。"""

    model_config = ConfigDict(extra="forbid")
    option_code: str
    reason: str = "手动平仓"
    qty: int | None = Field(default=None, ge=1)   # None=全平；否则部分平仓张数
    price_type: str | None = None                 # limit/bid/ask/mid/market；None=默认买一
    limit_price: str | None = None
    timeout_policy: str = "quick"                 # quick=走模板超时撤单；persistent=持续挂单

    @field_validator("price_type")
    @classmethod
    def _valid_price_type(cls, v: str | None) -> str | None:
        if v is not None and v not in {"limit", "bid", "ask", "mid", "market"}:
            raise ValueError(f"未知价格类型: {v!r}（limit/bid/ask/mid/market）")
        return v

    @field_validator("timeout_policy")
    @classmethod
    def _valid_timeout_policy(cls, v: str) -> str:
        if v not in {"quick", "persistent"}:
            raise ValueError("timeout_policy 只能是 quick 或 persistent")
        return v


@router.post("/close")
async def close_order(req: CloseRequest) -> dict:
    """⑤ 手动平仓受理：close_submit 秒返回 → close_run 丢后台跑 atomic_close。仅可执行档。

    前端靠 /api/positions 的 status(closing→掉出=成交)/close_error(失败) 回显最终结果。
    """
    source = current_source()
    route_started = time.perf_counter()
    result = await asyncio.to_thread(
        exec_close.close_submit, source, req, route_started)
    if result.get("spawn"):
        _schedule_bg(exec_close.close_run, source, req)
    return result


class CancelCloseRequest(BaseModel):
    """撤在途平仓挂单请求。"""

    model_config = ConfigDict(extra="forbid")
    option_code: str


@router.post("/close/cancel")
async def cancel_close_ep(req: CancelCloseRequest) -> dict:
    """⑤ 撤在途平仓挂单：撤券商未成交平仓卖单 + 回退持仓 open（可重新平仓）。仅可执行档。"""
    return await asyncio.to_thread(exec_close.cancel_close, current_source(), req.option_code)
