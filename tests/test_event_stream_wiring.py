"""事件流接线与降级 —— 锁死「日志失败绝不影响交易」。

这是整条链路上唯一会拖垮下单的方式：写盘炸了把异常抛回 logger.info() 的调用方。
本文件用一个 write 必抛的假文件对象来证明它不会。
"""
from __future__ import annotations

import json
import logging
import os

import pytest

from app.core.event_stream import (EventFilter, JsonlEventHandler,
                                   RepeatSuppressFilter)


def test_写盘失败时调用方完全不受影响(tmp_path):
    class _炸(JsonlEventHandler):
        def emit(self, record):
            try:
                raise OSError("磁盘满")
            except Exception:
                self.handleError(record)

    h = _炸(str(tmp_path / "e.jsonl"))
    h.addFilter(EventFilter())
    logger = logging.getLogger("tt.wiring.test")
    logger.addHandler(h)
    logger.setLevel(logging.INFO)
    logging.raiseExceptions = False
    try:
        logger.info('{"event":"execute","contract":"SPY260803C00754000"}')  # 不得抛
    finally:
        logging.raiseExceptions = True
        logger.removeHandler(h)


def test_事件写进jsonl文件且每行一个对象(tmp_path):
    path = tmp_path / "e.jsonl"
    h = JsonlEventHandler(str(path))
    h.addFilter(EventFilter())
    logger = logging.getLogger("tt.wiring.write")
    logger.handlers = [h]
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.info('{"event":"execute","qty":3}')
    logger.info("Connected to WebSocket.")          # 三方库文本，不该进
    logger.info('{"event":"trade_order_filled","fill_price":"1.11"}')
    h.flush()
    lines = [l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == 2
    assert [json.loads(l)["event"] for l in lines] == ["execute", "trade_order_filled"]


@pytest.fixture
def _isolated_dailylogger():
    """隔离 dailylogger 的模块级共享状态与 root logger，不用 importlib.reload。

    最初用 ``importlib.reload(dailylogger)`` 来拿到「一张白纸」，但 reload 会
    重新执行模块顶层代码——包括文件末尾那行 `setup_root_logger()`——把新建的
    handler/抑制器实例加到进程唯一的 root logger 上，而不会摘掉旧的。同一
    pytest 进程内多个用例各 reload 一次，root logger 上的 handler 与
    RepeatSuppressFilter 实例会跨用例累积（本文件内两个用例顺序执行即可
    复现：第二个用例会看到第一个用例遗留的实例，导致「共享同一实例」的断言
    失败）。改为显式保存/恢复 dailylogger 模块的共享全局变量与 root logger
    的 handlers 列表，验证目的不变（TT_DISABLE_FILE_LOG 真能关掉 JSONL、
    四个 handler 真共享同一抑制器实例），但不产生跨用例的残留副作用。
    """
    import app.core.dailylogger as dl

    shared_attrs = (
        "_shared_file_handler", "_shared_console_handler",
        "_shared_queue_handler", "_shared_jsonl_handler",
        "_shared_suppressor",
    )
    saved_globals = {name: getattr(dl, name) for name in shared_attrs}
    root_logger = logging.getLogger()
    saved_handlers = list(root_logger.handlers)
    saved_level = root_logger.level
    # setup_root_logger() 还有一处全局副作用：把 watchfiles/httpx 两个第三方
    # logger 的级别压到 WARNING（见 dailylogger.py 里的注释）。这不影响本
    # fixture 要验证的东西，但会残留到会话内其他测试，是个潜在的测试间
    # 污染源，一并保存/恢复。
    watchfiles_level = logging.getLogger("watchfiles").level
    httpx_level = logging.getLogger("httpx").level

    for name in shared_attrs:
        setattr(dl, name, None)

    try:
        yield dl
    finally:
        for name, value in saved_globals.items():
            setattr(dl, name, value)
        root_logger.handlers = saved_handlers
        root_logger.setLevel(saved_level)
        logging.getLogger("watchfiles").setLevel(watchfiles_level)
        logging.getLogger("httpx").setLevel(httpx_level)


def test_TT_DISABLE_FILE_LOG关闭时不建jsonl(tmp_path, monkeypatch, _isolated_dailylogger):
    monkeypatch.setenv("TT_DISABLE_FILE_LOG", "1")
    dl = _isolated_dailylogger
    file_h, _, _, jsonl_h = dl._get_shared_handlers(str(tmp_path))
    assert file_h is None
    assert jsonl_h is None
    assert not (tmp_path / "ttt.events.jsonl").exists()


def test_四个handler共享同一个抑制器实例(tmp_path, monkeypatch, _isolated_dailylogger):
    monkeypatch.delenv("TT_DISABLE_FILE_LOG", raising=False)
    dl = _isolated_dailylogger
    # 直接断言 `_get_shared_handlers` 这次调用产出的 4 个 handler，而不是扫描
    # 整个 root logger 的 handlers 列表：root logger 是进程内唯一单例，测试
    # 会话里只要有任何模块在此之前 import 过 dailylogger（会触发模块末尾的
    # `setup_root_logger()`），root logger 上就已经挂着一组更早的 handler/
    # 旧抑制器实例，与本用例待验证的“这一组 4 个 handler 是否共享同一个
    # 抑制器”是两回事，混在一起扫描会把历史遗留实例也算进去，产生假阳性
    # 失败。这里换成直接检查这次调用返回的 4 个 handler 本身，既不依赖
    # root logger 的历史状态，又是这次调用产出的handler
    handlers = dl._get_shared_handlers(str(tmp_path))
    assert len(handlers) == 4
    assert all(h is not None for h in handlers)
    suppressors = {
        id(f)
        for h in handlers
        for f in h.filters
        if isinstance(f, RepeatSuppressFilter)
    }
    assert len(suppressors) == 1, "必须共享一个实例，否则同一条记录被计数多次"

    # 再验证 setup_root_logger 确实把这一组 handler 原样挂上了 root logger
    # （而不是另建一套），这是 wiring 的另一半契约。
    dl.setup_root_logger(str(tmp_path))
    root_handlers = logging.getLogger().handlers
    for h in handlers:
        assert h in root_handlers


def test_真实接线下按event字段归并不依赖handler挂载顺序(tmp_path, monkeypatch, _isolated_dailylogger):
    """锁死审查发现的 Critical 缺陷：按 event 字段归并不能依赖 EventFilter
    已经先跑过。

    `setup_root_logger()` 的挂载顺序是 `[file_h, console_h, queue_h,
    jsonl_h]`，只有排最后的 jsonl_h 挂了 `EventFilter`；`Logger.
    callHandlers()` 按此顺序逐个 `handle()`，第一个评估 `RepeatSuppressFilter`
    的永远是没挂 `EventFilter` 的 file_h。`tests/test_event_stream_suppress.py`
    里的单测 helper `_rec()` 会无条件先手动调一次 `EventFilter().filter(rec)`
    再交给抑制器，测的是"理想顺序"，测不出这个问题——本用例改为经真实的
    `tt.gate` 子 logger 走 `setup_root_logger()` 建出来的完整 4-handler 链路，
    才能真正暴露/锁死它。

    两条 WARNING 的 `event` 字段相同，但业务详情文本（非数字部分）不同，
    只有按 `event` 字段归并才会判定为同一 streak（第二条被抑制、不落盘）；
    若退化成"抹数字比对"，两条文本不同会被误判成不同 streak，都会落盘。
    """
    monkeypatch.delenv("TT_DISABLE_FILE_LOG", raising=False)
    dl = _isolated_dailylogger
    root_logger = logging.getLogger()
    # 先清空 root logger 上的历史 handler（可能来自会话内更早的模块 import，
    # 指向项目真实的 log/ttt.log），只留本次调用产生的 4 个 handler 指向
    # tmp_path——否则下面真实 logger.warning() 调用会连带写进项目的生产日志
    # 文件。fixture 在 teardown 时会用测试开始前的快照整体替换回去，这里的
    # 清空不会影响会话内其他测试。
    root_logger.handlers = []
    dl.setup_root_logger(str(tmp_path))

    logger = logging.getLogger("tt.gate")

    def _warn(msg: str) -> None:
        # `_key()` 归并键里包含 `record.lineno`：同一处日志语句反复触发才是
        # 现实场景（例如某个 except 块里的 `logger.warning(json.dumps(...))`
        # 被同一行代码反复命中，每次异常详情文本不同）。若这里直接在测试体
        # 两处不同行各写一次 `logger.warning(...)`，两条记录天然就落在不同
        # lineno、天然是不同 key，测不出"按 event 字段归并"这件事——必须
        # 让两次调用共享同一个调用点（同一行号）。
        logger.warning(msg)

    _warn('{"event":"trading_state_refresh_error","error":"start cannot be after end"}')
    _warn('{"event":"trading_state_refresh_error","error":"end cannot be before now"}')

    content = (tmp_path / "ttt.log").read_text(encoding="utf-8")
    assert content.count("trading_state_refresh_error") == 1, (
        "第二条应被判为同一 streak 而抑制，未落盘；若按 event 字段归并失效，"
        "两条文本不同的记录会被误判成不同 streak，都会落盘变成 2 次"
    )
