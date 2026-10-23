"""事件流过滤层 —— 锁死「只有业务事件进机读流」。

三方库（yfinance/alpaca/pandas_ta）发的是纯文本，业务事件发的是含 `event`
键的 JSON。这条边界如果松了，JSONL 会重新变回今天 ttt.log 的样子：
1326 行里只有 794 行能用。
"""
from __future__ import annotations

import logging

from app.core.event_stream import EventFilter


def _record(msg: str, level: int = logging.INFO) -> logging.LogRecord:
    return logging.LogRecord("tt.trade", level, "exec_entry.py", 210, msg, (), None)


def test_业务事件放行并挂载解析结果():
    f = EventFilter()
    rec = _record('{"event":"execute","contract":"SPY260803C00754000"}')
    assert f.filter(rec) is True
    assert rec._event_payload["event"] == "execute"
    assert rec._event_payload["contract"] == "SPY260803C00754000"


def test_三方库纯文本被挡掉():
    f = EventFilter()
    assert f.filter(_record("Connected to WebSocket.")) is False
    assert f.filter(_record("Subscribed to symbols: ['^VIX', 'AMD']")) is False


def test_非法json被挡掉():
    f = EventFilter()
    assert f.filter(_record('{"event":"execute"')) is False


def test_合法json但无event键被挡掉():
    f = EventFilter()
    assert f.filter(_record('{"count":3}')) is False


def test_json数组被挡掉():
    f = EventFilter()
    assert f.filter(_record('[1,2,3]')) is False


def test_run_id格式可排序且带随机后缀():
    from app.core.event_stream import RUN_ID
    head, _, tail = RUN_ID.rpartition("-")
    assert len(head) == len("20260803-214110")
    assert head[8] == "-"
    assert len(tail) == 4
    int(tail, 16)  # 必须是十六进制


class _BoomStr:
    """``__str__`` 必抛，用来伪造一个 ``getMessage()`` 必抛异常的 record。"""

    def __str__(self) -> str:
        raise RuntimeError("boom")


def test_filter内部真的抛异常时调用方不受影响():
    # 结构性回归：filter() 有顶层 try/except 兜底，任何内部异常都必须止于
    # filter() 内部，不得从 logger.info() 的调用方（下单/平仓热路径）冒出去。
    f = EventFilter()
    rec = logging.LogRecord("tt.trade", logging.INFO, "exec_entry.py", 210, _BoomStr(), (), None)
    assert f.filter(rec) is False


class _SlottedFakeRecord:
    """无 __dict__，只有 _msg 槽位；getMessage()/startswith/json.loads 全部走通，
    最后 `record._event_payload = payload` 因槽位限制必抛 AttributeError——
    这一行在两段局部 try 之外，只有顶层 try 能拦住。"""
    __slots__ = ("_msg",)

    def __init__(self, msg):
        self._msg = msg

    def getMessage(self):
        return self._msg


def test_顶层兜底_属性写入失败也不外泄():
    f = EventFilter()
    rec = _SlottedFakeRecord('{"event":"execute"}')
    assert f.filter(rec) is False
