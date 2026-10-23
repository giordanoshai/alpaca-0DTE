"""0DTE 交易终端 — FastAPI 应用（前端外壳阶段）。

职责（此阶段）：
  - 提供设计稿实现的单页（static/index.html + tokens.css + app.css + app.js）

启动（任一）：
  - python app/main.py            # 直接运行，见文件末尾 __main__
  - python -m app.main            # 从仓库根以模块运行
  - uvicorn app.main:app --reload --port 8002
默认 http://127.0.0.1:8002；服务器上靠 .env 的 TT_HOST / TT_PORT 改成 0.0.0.0:8010。

后续阶段在此单体应用内扩展：①信号接入 / ②市场状态 / ③合约选择 /
④validator / ⑤订单状态机 …（README 架构模块编号），async 优先，不拆微服务。
"""
from __future__ import annotations

import sys
from pathlib import Path

# 允许直接 `python app/main.py`：脚本启动时 sys.path[0] 是 app/ 目录，
# 需先把仓库根加入 sys.path，下方 `from app.xxx` 绝对导入才能解析。
BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import asyncio  # noqa: E402
import json  # noqa: E402
from contextlib import asynccontextmanager  # noqa: E402

from fastapi import FastAPI  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from app import trade_recorder  # noqa: E402
from app.exec_reconcile import (  # noqa: E402
    reconcile_pending_journal, recover_positions_on_startup,
)
from app.core import dailylogger  # noqa: F401, E402
from app.services.market_feed import MARKET_FEED  # noqa: E402
from app.option_stream import OPTION_STREAM  # noqa: E402
from app.order_journal import JOURNAL  # noqa: E402
from app.core.market_session import order_session_window  # noqa: E402
from app.services.option_candidates import OPTION_CANDIDATES  # noqa: E402
from app.services.order_sync import ORDER_SYNC  # noqa: E402
from app.trade_update_stream import TRADE_UPDATE_STREAM  # noqa: E402
from app.trading_state import TRADING_STATE  # noqa: E402
from app.watchdog import watchdog_loop  # noqa: E402

from app.routers import config, market, orders  # noqa: E402

STATIC_DIR = BASE_DIR / "static"

from app.core.dailylogger import get_logger

logger = get_logger("tt.gate")


@asynccontextmanager
async def lifespan(_app):
    from app.watcher.watcher_manager import GLOBAL_WATCHER
    from app.db.signal_store import db_worker_loop
    from app.core.sb_client import get_supabase_client
    from app.config import USER_ID, MARKET_STATUS_TICKERS
    from app.lifecycle import (
        SHUTDOWN, install_shutdown_signal_hook, restore_signal_handlers,
    )

    # 关停信号必须在 SIGINT 触发的那一刻置位：uvicorn 是「先等连接关闭 → 超时强杀
    # → 才跑 lifespan shutdown」，等到本函数的 finally 已经晚了一个超时窗口，SSE
    # 会被强杀成回溯。详见 app/lifecycle.py。
    SHUTDOWN.clear()
    _signal_handlers = install_shutdown_signal_hook(asyncio.get_running_loop())

    # 提前显式注册事件流 mode（mock/paper）：client.py 里的 set_mode() 挂在
    # TradingClient 惰性构造里，首次真正下单前不会执行，导致启动段事件（如下方
    # journal_rebuilt）的 mode 恒为 unknown（2026-08-04 审查发现）。这里只做早
    # 注册，client.py 那处仍继续作为后续权威覆盖，不删除。
    from app.core.event_stream import set_mode
    from app.integrations.alpaca.client import endpoint_override
    set_mode("mock" if endpoint_override() else "paper")

    # 1. 启动 db_worker 队列
    db_task = asyncio.create_task(db_worker_loop())
    
    # 2. 加载 Watchlist 并预热历史数据 (默认保底包含 QQQ, IWM, SPY)
    tickers_to_start = list(dict.fromkeys(["QQQ", "IWM", "SPY"] + MARKET_STATUS_TICKERS))
    try:
        client = get_supabase_client()
        response = client.table('user_watchlist').select('ticker').eq('user_id', USER_ID).not_.is_('gear', 'null').execute()
        if response and response.data:
            tickers = [item['ticker'].lstrip('$') for item in response.data]
            tickers_to_start = list(dict.fromkeys(tickers + tickers_to_start))
    except Exception as e:
        logger.warning(f"[*] Supabase Watchlist 获取跳过/失败，使用默认保底票池: {e}")
        
    async def _async_start_watcher():
        logger.info(f"[*] 正在后台并行加载 {len(tickers_to_start)} 个盯盘标的...")
        await asyncio.gather(*[GLOBAL_WATCHER.add_ticker(t) for t in tickers_to_start], return_exceptions=True)
        print(f"[DataManager] ================= 全量 {len(tickers_to_start)} 个标的历史 K 线数据预热完成！=================")
        logger.info(f"[*] 成功加载 {len(tickers_to_start)} 个标的进行 Watcher 监控与数据预热。")
        await GLOBAL_WATCHER.start()

    watcher_task = asyncio.create_task(_async_start_watcher())

    # ⑦ 重启后优先从逐笔订单表恢复完整原因/动作，再用 ttt_trades 为上线前旧记录兜底。
    # best-effort，缺表或读库失败只告警不阻断启动。
    try:
        def _rebuild_journal():
            JOURNAL.rebuild_from_order_events(
                trade_recorder.load_order_events(order_session_window())
            )
            JOURNAL.rebuild_from_trades(trade_recorder.load_today())

        await asyncio.to_thread(_rebuild_journal)
        logger.info(json.dumps({"event": "journal_rebuilt", "count": len(JOURNAL._events)}, ensure_ascii=False))
    except Exception as e:  # noqa: BLE001
        logger.warning(json.dumps({"event": "journal_rebuild_error", "error": str(e)}, ensure_ascii=False))

    # 重建后核对 pending 买单是否在 Alpaca 侧仍真实在途（修「07-23 幽灵订单」：孤儿/测试污染的
    # 假 pending 单会被上一步原样重建成待成交，永远撤不掉）。best-effort，失败不阻断启动。
    try:
        result = await asyncio.to_thread(reconcile_pending_journal)
        logger.info(json.dumps({"event": "pending_journal_reconciled", **result}, ensure_ascii=False))
    except Exception as e:  # noqa: BLE001
        logger.warning(json.dumps({"event": "pending_journal_reconcile_error", "error": str(e)}, ensure_ascii=False))

    # 带仓重启：把券商实持仓恢复进 REGISTRY。**必须在 watchdog_loop 启动前完成** ——
    # 否则它对着空 REGISTRY 巡检，止损/超时/追踪全部失明（2026-08-14 实测 2 分 35 秒，
    # 详见 docs/reports/2026-08-14-未修缺陷清单.md §1）。此处 await 也顺带保证了
    # 「恢复完成前不得开新仓」：uvicorn 在 lifespan 启动段跑完前不 accept 请求。
    # 函数自身已吞异常并降级，不会阻断启动。
    recovery = await asyncio.to_thread(recover_positions_on_startup)
    logger.info(json.dumps(
        {"event": "startup_positions_recovered", **recovery}, ensure_ascii=False))

    order_sync_task = asyncio.create_task(ORDER_SYNC.run_forever())
    if not await ORDER_SYNC.wait_ready():
        order_sync_task.cancel()
        await asyncio.gather(order_sync_task, return_exceptions=True)
        raise RuntimeError("订单同步服务启动超时")
    trade_updates_task = asyncio.create_task(TRADE_UPDATE_STREAM.run_forever())
    task = asyncio.create_task(watchdog_loop())
    stream_task = asyncio.create_task(OPTION_STREAM.run_forever())  # ⑥ 期权报价 WS（按需建连）
    # UW Market Tide 徽章轮询（纯展示 + 交叉提醒，不接策略引擎）
    from app.routers.watcher_ws import GLOBAL_WS_MANAGER
    from app.services.market_tide_service import market_tide_refresh_loop
    tide_task = asyncio.create_task(market_tide_refresh_loop(GLOBAL_WS_MANAGER))
    from app.services.gex_wall_service import gex_wall_refresh_loop
    from app.watcher.watcher_manager import GLOBAL_WATCHER
    gex_wall_task = asyncio.create_task(
        gex_wall_refresh_loop(GLOBAL_WATCHER.active_tickers)
    )
    # 盘中快讯滚动条（纯展示，不接策略）。窗口 08:00-16:05 ET 含盘前 ——
    # spec: docs/superpowers/specs/2026-09-05-盘中快讯滚动条-design.md
    from app.services.news_feed_service import news_feed_refresh_loop
    news_task = asyncio.create_task(news_feed_refresh_loop(GLOBAL_WS_MANAGER))

    # 注：SOS（swing 期权筛选 + 0-1DTE 三线出场）已于 2026-08-15 整体移出本项目，
    # 备份在 D:\SOS_BAK（含还原说明）。原先这里启停 SOS_ENGINE / SOS_TRADING /
    # SOS_RISK_MONITOR / EXIT_LINES_MONITOR 四个后台单例，现已全部移除。

    market_feed_task = asyncio.create_task(MARKET_FEED.run())
    option_candidates_task = asyncio.create_task(OPTION_CANDIDATES.run())
    trading_state_task = asyncio.create_task(TRADING_STATE.run())
    trading_ws_task = asyncio.create_task(trading_ws.TRADING_WS_HUB.run())
    try:
        yield
    finally:
        # 非信号路径（编程式关停、测试）也要置位，长连接端点才会收尾
        SHUTDOWN.set()
        restore_signal_handlers(_signal_handlers)

        # 关停原则：每一步都要「尽力做完」，任何一步抛异常或卡死都不得让后续步骤
        # 被跳过 —— 否则会留下未取消的任务与未关闭的连接，Ctrl+C 时表现为一串
        # ASGI 异常回溯。故逐步隔离 + 各自限时。
        async def _step(name: str, coro, timeout: float = 3.0) -> None:
            try:
                await asyncio.wait_for(coro, timeout)
            except asyncio.TimeoutError:
                logger.warning(json.dumps(
                    {"event": "shutdown_step_timeout", "step": name, "timeout_s": timeout},
                    ensure_ascii=False))
            except Exception as e:  # noqa: BLE001 — 关停期不让单步失败中断整条收尾链
                logger.warning(json.dumps(
                    {"event": "shutdown_step_error", "step": name, "error": str(e)},
                    ensure_ascii=False))

        # order_sync_task 留到最后：先停掉所有生产者，再等它把在途的写完。
        background = [
            task, stream_task, tide_task, gex_wall_task, news_task,
            market_feed_task, option_candidates_task,
            trading_state_task, trading_ws_task, trade_updates_task,
            db_task, watcher_task,
        ]
        for t in background:
            t.cancel()
        # 每个被取消的任务都必须 await：只 cancel 不 await，事件循环关闭时会抛
        # "Task was destroyed but it is pending!"，且任务内的异常永不被取回。
        await _step("background_tasks",
                    asyncio.gather(*background, return_exceptions=True), timeout=8.0)

        await _step("order_sync_flush", ORDER_SYNC.wait_idle())
        order_sync_task.cancel()
        await _step("order_sync_task",
                    asyncio.gather(order_sync_task, return_exceptions=True))
        await _step("watcher", GLOBAL_WATCHER.stop(), timeout=8.0)


app = FastAPI(title="0DTE 交易终端", version="0.1.0", lifespan=lifespan)

# 注册所有路由模块
app.include_router(config.router)
app.include_router(market.router)
app.include_router(orders.router)

from app.routers import trading_ws, watcher_api, watcher_ws  # noqa: E402
app.include_router(trading_ws.router)
app.include_router(watcher_api.router)
app.include_router(watcher_ws.router)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
@app.get("/trading")
async def index() -> FileResponse:
    """0DTE 终端入口。"""
    return FileResponse(str(STATIC_DIR / "index.html"))

@app.get("/watcher")
async def watcher_index() -> FileResponse:
    """4in1 盯盘入口。"""
    return FileResponse(str(STATIC_DIR / "watcher" / "index.html"))


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


if __name__ == "__main__":
    import faulthandler
    import os

    import uvicorn

    from app.lifecycle import run_with_exit_log

    # 崩溃留痕（2026-08-28）：解释器级的硬崩溃（原生扩展段错误等）不会走 Python 的
    # 异常路径，只有 faulthandler 能在死之前把 C 栈吐出来。文件单独一份，避免与
    # 按天轮转的 ttt.log 抢句柄；句柄必须一直开着，不能用 with。
    _crash_dump = open(os.path.join(BASE_DIR, "log", "ttt.faulthandler.log"),
                       "a", encoding="utf-8")
    faulthandler.enable(file=_crash_dump)

    # reload 会 spawn 新解释器子进程去 import "app.main:app"，它不继承本进程
    # 顶部的 sys.path 修改，故用 PYTHONPATH 把仓库根传给它。
    root = str(BASE_DIR)
    os.environ["PYTHONPATH"] = root + os.pathsep + os.environ.get("PYTHONPATH", "")

    # 热重载默认**关闭**（2026-07-25）：REGISTRY/BUY_INTENTS 是纯内存态，reload 一次就清空
    # ——盘中改任何一行代码都会让 watchdog 对已有持仓完全失明（止损/触墙止盈停摆），
    # 而 JOURNAL 会从 ttt_trades 自动重建、REGISTRY 不会，于是变成「订单面板看得到、
    # 持仓面板空了、没人管止损」的错位状态，必须手动点对账才能恢复。
    # 开发期需要热重载：设 TT_RELOAD=1（只在无持仓时用）。
    reload_on = os.environ.get("TT_RELOAD", "").strip().lower() in ("1", "true", "yes", "on")
    if reload_on:
        print("[!] TT_RELOAD=1 已开启热重载：重启会清空持仓登记（REGISTRY），"
              "watchdog 将对已有持仓失明，请勿在持仓时使用。")
    # timeout_graceful_shutdown：关停时最多等 5s 让连接收尾，超时强制关闭，
    # 避免 Ctrl+C 被长连接 /ws 拖住（Windows reload 子进程尤甚）。
    #
    # log_config=None（2026-08-28）：不让 uvicorn 用自己的 dictConfig 覆盖日志配置。
    # 它默认给 uvicorn/uvicorn.access 设 propagate=False + 自带 handler，于是启停提示
    # 与 ASGI 回溯只出现在终端，文件日志里一个字都没有 —— 这正是 21:44 那次「突然退出」
    # 查不出原因的直接原因（当天 ttt.log 里 "uvicorn" 出现 0 次）。关掉后它的记录沿用
    # root 的 handler 落进 ttt.log；访问日志的刷屏在 dailylogger 里单独压掉。
    #
    # run_with_exit_log：无论正常停、Ctrl+C 还是抛异常，都往日志里写一条 process_exit。
    # 监听地址/端口来自 .env（TT_HOST / TT_PORT），本机不配就是 127.0.0.1:8002；
    # 服务器那份 .env 配 0.0.0.0:8010，两边共用这一份代码。
    from app.config import server_bind

    host, port = server_bind()
    print(f"[+] 监听 http://{host}:{port}（热重载 {'开' if reload_on else '关'}）")

    run_with_exit_log(lambda: uvicorn.run(
        "app.main:app", host=host, port=port, reload=reload_on,
        timeout_graceful_shutdown=5, log_config=None))
