r"""关停信号钩子必须覆盖 uvicorn 的全部信号，并链回原处理器。

历史背景（原文件名 `test_sse_shutdown.py`）：这套测试是为 `/api/sos/stream` 写的
——它是无限 SSE 流，生成器阻塞在队列等待上，而 `StreamingResponse.listen_for_disconnect`
又在等一个永不到来的客户端断开消息。uvicorn 关停时先等连接自然结束
（timeout_graceful_shutdown=5s），等不到就强杀：

    ERROR:    Cancel 1 running task(s), timeout graceful shutdown exceeded
    File "starlette/responses.py", line 244, in listen_for_disconnect
    asyncio.exceptions.CancelledError: Task cancelled, timeout graceful shutdown exceeded

且 uvicorn 是「等连接 → 强杀 → **才** 跑 lifespan shutdown」，所以标志位必须在信号
触发那一刻置位，等 lifespan 收尾已晚了整个超时窗口。

2026-08-15 SOS 移出本项目后 `/api/sos/stream` 一并消失，而它是全项目唯一的 SSE
端点，故三条「SSE 关停行为」用例随之删除（备份在 D:\SOS_BAK\tests\test_sse_shutdown.py）。
本文件保留的两条测的是 `app/lifecycle.py` 的信号钩子本身，与 SSE 无关、仍在服役：
`main.py` 的 lifespan 照旧 install/restore 它。**将来若再引入任何 SSE 长连接端点，
请把删掉的那三条从备份里取回并改指向新端点**——那才是这套机制真正要防的回归。
"""
from __future__ import annotations

import asyncio
import signal

import pytest

from app.lifecycle import (
    SHUTDOWN, _HANDLED_SIGNALS, install_shutdown_signal_hook,
    restore_signal_handlers,
)


@pytest.fixture(autouse=True)
def _clean_event():
    SHUTDOWN.clear()
    yield
    SHUTDOWN.clear()


def test_钩子覆盖uvicorn全部关停信号():
    """漏挂任一信号 = 走那条路关停时绕过置位，长连接又卡满 timeout_graceful_shutdown。

    win32 上 uvicorn 比 Unix 多一个 SIGBREAK（Ctrl+Break），最初就漏了它。
    """
    from uvicorn.server import HANDLED_SIGNALS

    missing = set(HANDLED_SIGNALS) - set(_HANDLED_SIGNALS)
    assert not missing, (
        f"uvicorn 处理但本模块未挂钩的信号：{[s.name for s in missing]}"
    )


def test_信号钩子链回原处理器():
    """必须链回 uvicorn 的 handle_exit，否则置了位但服务器根本不会停。"""
    called = []

    def _previous(signum, frame):
        called.append(signum)

    original = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, _previous)
    try:
        async def _run():
            loop = asyncio.get_running_loop()
            handlers = install_shutdown_signal_hook(loop)
            assert handlers, "未安装信号钩子"
            installed = signal.getsignal(signal.SIGINT)
            assert installed is not _previous, "钩子未生效"

            installed(signal.SIGINT, None)   # 直接调用，避免真的发信号打断测试进程
            await asyncio.sleep(0)           # 让 call_soon_threadsafe 落地
            restore_signal_handlers(handlers)
            return SHUTDOWN.is_set(), signal.getsignal(signal.SIGINT)

        was_set, restored = asyncio.run(_run())
        assert was_set, "钩子未置位 SHUTDOWN"
        assert called == [signal.SIGINT], "未链回原处理器，uvicorn 不会停机"
        assert restored is _previous, "未还原原处理器"
    finally:
        signal.signal(signal.SIGINT, original)
