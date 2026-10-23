# -*- coding: utf-8 -*-
"""进程级关停信号：让长连接端点在收到 Ctrl+C 的**那一刻**就能主动收尾。

为什么不能等 lifespan 的 finally——uvicorn 的关停顺序是：

    停止 accept → 逐连接 connection.shutdown() → 等连接自然结束
    （timeout_graceful_shutdown，本项目 5s）→ 超时则 task.cancel() 强杀
    → **最后**才 await lifespan.shutdown()

SSE 是无限流，永远不会「自然结束」：生成器阻塞在队列等待上，而
`StreamingResponse.listen_for_disconnect` 又在等一个永不到来的客户端断开消息。
于是每次 Ctrl+C 都要空等 5s，再被强杀成一串回溯：

    ERROR:    Cancel 1 running task(s), timeout graceful shutdown exceeded
    ...
    File "starlette/responses.py", line 244, in listen_for_disconnect
    asyncio.exceptions.CancelledError: Task cancelled, timeout graceful shutdown exceeded

等 lifespan 收尾时再置位已经晚了整整一个超时窗口。故在 uvicorn 自己的信号处理器
之前插一层：置位 `SHUTDOWN`，长连接端点据此立刻返回，连接随即排空。
"""
from __future__ import annotations

import asyncio
import json
import signal
import sys
import threading
import traceback
from typing import Callable

from app.core.dailylogger import get_logger

logger = get_logger("tt.lifecycle")

#: 进程正在关停。长连接端点（SSE/轮询）应在 await 点上与它竞争，置位后尽快返回。
SHUTDOWN = asyncio.Event()

# 必须与 uvicorn.server.HANDLED_SIGNALS 逐个对齐：漏掉哪个，走那个信号关停时就
# 绕过了本模块的置位，SSE 又会卡满整个 timeout_graceful_shutdown。win32 上
# uvicorn 额外挂了 SIGBREAK（Ctrl+Break）。
_HANDLED_SIGNALS = (signal.SIGINT, signal.SIGTERM)
if sys.platform == "win32":  # pragma: no cover — 平台分支
    _HANDLED_SIGNALS += (signal.SIGBREAK,)


def install_shutdown_signal_hook(loop: asyncio.AbstractEventLoop) -> dict:
    """在现有信号处理器（uvicorn 的 handle_exit）之前插一层置位动作。

    返回被替换掉的原处理器，供 `restore_signal_handlers()` 还原。
    非主线程、或平台不支持某信号时跳过（不抛错）。
    """
    installed: dict = {}
    if threading.current_thread() is not threading.main_thread():
        return installed

    for sig in _HANDLED_SIGNALS:
        try:
            previous = signal.getsignal(sig)
        except (ValueError, OSError):  # pragma: no cover — 平台不支持
            continue

        def _handler(signum, frame, _previous=previous):
            # 信号处理器在主线程的字节码间隙执行；用 call_soon_threadsafe 置位并
            # 顺带唤醒事件循环，避免它还阻塞在 select 上迟迟不响应。
            loop.call_soon_threadsafe(SHUTDOWN.set)
            # 链回原处理器，uvicorn 的 handle_exit 仍要照常跑（否则服务器不会停）
            if callable(_previous):
                _previous(signum, frame)

        try:
            signal.signal(sig, _handler)
            installed[sig] = previous
        except (ValueError, OSError):  # pragma: no cover — 平台不支持
            continue

    return installed


def restore_signal_handlers(handlers: dict) -> None:
    """还原 `install_shutdown_signal_hook()` 替换掉的处理器。"""
    for sig, previous in (handlers or {}).items():
        try:
            signal.signal(sig, previous)
        except (ValueError, OSError, TypeError):  # pragma: no cover
            continue


def log_process_exit(exc: BaseException | None) -> None:
    """把进程退出的原因写进日志（人读 ttt.log + 机读 ttt.events.jsonl）。

    2026-08-28 那次「突然退出」查不出原因，是因为退出路径上一个字都没留：uvicorn
    自己的输出不进文件日志，lifespan 的收尾只在出错时才写。这里补上唯一一处强制留痕。

    - ``None`` / ``SystemExit(0)``   正常退出 → cause=normal
    - ``KeyboardInterrupt``          Ctrl+C 打断 → cause=signal
    - ``SystemExit(非 0)``           致命启动错误（端口被占等）→ cause=exit_code
    - 其他异常                       连回溯一起写 → cause=exception

    **四类不许合并**：uvicorn 自己收到 Ctrl+C 时是走 handle_exit 优雅收尾、正常
    返回（cause=normal），而端口占用之类是 `sys.exit(1)`。把 SystemExit 一律记成
    「signal」会把人引去找一个根本没发生过的 Ctrl+C —— 这次事故正是吃了「看不出
    是哪种死法」的亏。

    **必须带 `event` 键**：机读事件流的 handler 只收含该键的 JSON，漏掉它这条记录
    进不了 ttt.events.jsonl，复盘脚本就又看不见退出点了。
    """
    code = exc.code if isinstance(exc, SystemExit) else None
    if exc is None or (isinstance(exc, SystemExit) and code in (None, 0)):
        cause = "normal"
    elif isinstance(exc, SystemExit):
        cause = "exit_code"
    elif isinstance(exc, KeyboardInterrupt):
        cause = "signal"
    else:
        cause = "exception"

    payload: dict = {"event": "process_exit", "cause": cause}
    if cause == "exit_code":
        payload["exit_code"] = code
    if exc is not None:
        payload["error"] = f"{type(exc).__name__}: {exc}"
        if cause == "exception":
            payload["traceback"] = "".join(traceback.format_exception(
                type(exc), exc, exc.__traceback__))
    logger.info(json.dumps(payload, ensure_ascii=False))


def run_with_exit_log(run: Callable[[], None]) -> None:
    """跑服务器主体，无论怎么退出都先留痕再放行。

    异常**原样往外抛**：留痕是附加动作，不许顺手把错误吞掉变成静默退出 ——
    那正是这次事故里最难受的部分。
    """
    try:
        run()
    except BaseException as exc:
        log_process_exit(exc)
        raise
    log_process_exit(None)
