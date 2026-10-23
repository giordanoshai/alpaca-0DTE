"""刷屏抑制 —— 锁死「只抑制 WARNING/ERROR、INFO 心跳一条不删」。

2026-08-03 那晚 532 行同一个 400 错误占了全文 40%。但心跳 trade_rest_reconciled
**不是**噪声：active_positions 会 0→1、trading_stream_connected 会翻转，按
「抹掉数字后比对」去重会把持仓从无到有这个关键状态变化吃掉。离线折叠可以这么干
（原始日志还在），写入时这么干就是永久丢数据。

本文件是那个取舍的守卫：谁日后顺手把 INFO 也纳入抑制，这里会红。
"""
from __future__ import annotations

import json
import logging

from app.core.event_stream import EventFilter, RepeatSuppressFilter, _MAX_STREAKS

_SPAM = ('{"event":"trading_state_refresh_error",'
         '"error":"start cannot be after end"}')


def _rec(msg: str, level: int = logging.WARNING, lineno: int = 309):
    rec = logging.LogRecord("tt.gate", level, "trading_state.py", lineno, msg, (), None)
    EventFilter().filter(rec)
    return rec


def _decide(f, rec):
    """每条记录只判一次；4 个 handler 复评时复用缓存（见实现注释）。"""
    return f.filter(rec)


def test_首条必出后续被抑制():
    f = RepeatSuppressFilter(window_s=60.0)
    assert _decide(f, _rec(_SPAM)) is True
    for _ in range(64):
        assert _decide(f, _rec(_SPAM)) is False


def test_窗口到点改写成汇总事件且计数准确():
    clock = {"t": 1000.0}
    f = RepeatSuppressFilter(window_s=60.0, monotonic=lambda: clock["t"])
    assert _decide(f, _rec(_SPAM)) is True
    for _ in range(64):
        _decide(f, _rec(_SPAM))
    clock["t"] += 61.0
    rec = _rec(_SPAM)
    assert _decide(f, rec) is True
    out = json.loads(rec.getMessage())
    assert out["event"] == "log_suppressed"
    assert out["of"] == "trading_state_refresh_error"
    assert out["count"] == 64
    assert out["window_s"] == 60.0


def test_INFO心跳全量放行():
    f = RepeatSuppressFilter(window_s=60.0)
    hb = ('{"event":"trade_rest_reconciled","trading_stream_connected":true,'
          '"next_reconcile_s":30.0,"active_positions":0}')
    for _ in range(50):
        assert _decide(f, _rec(hb, level=logging.INFO, lineno=784)) is True


def test_不同key互不干扰():
    f = RepeatSuppressFilter(window_s=60.0)
    other = '{"event":"trade_close_order_reprice_failed","attempt":2}'
    assert _decide(f, _rec(_SPAM)) is True
    assert _decide(f, _rec(other, lineno=695)) is True
    assert _decide(f, _rec(_SPAM)) is False
    assert _decide(f, _rec(other, lineno=695)) is False


def test_非json的warning也能被抑制():
    f = RepeatSuppressFilter(window_s=60.0)
    assert _decide(f, _rec("连接超时 重试 3 次", level=logging.WARNING)) is True
    assert _decide(f, _rec("连接超时 重试 5 次", level=logging.WARNING)) is False


def test_同一条记录被复评时只计一次():
    f = RepeatSuppressFilter(window_s=60.0)
    _decide(f, _rec(_SPAM))
    dup = _rec(_SPAM)
    assert f.filter(dup) is False
    assert f.filter(dup) is False  # 第 2 个 handler 复评
    assert f.filter(dup) is False  # 第 3 个
    assert f._streaks[f._key(dup)][1] == 1  # 只计了 1 次，不是 3 次（索引 1 = 计数）


def test_稀疏复发窗口到期但计数为零时原样放行():
    """审查发现的盲区：复发间隔本身就比窗口长（低频重试/周期性对账失败），
    窗口内一条重复都没出现就直接翻窗——此时 streak 计数为 0，绝不能走
    「改写成汇总事件」分支，否则会生成一条"抑制了 0 条"的空汇总，把这条
    真实告警的内容永久顶掉，此后每次复发都会重演，比不装这个 filter 还差。
    """
    clock = {"t": 1000.0}
    f = RepeatSuppressFilter(window_s=60.0, monotonic=lambda: clock["t"])
    rec1 = _rec(_SPAM)
    assert _decide(f, rec1) is True
    clock["t"] += 300.0  # 远超窗口，期间没有第三次出现（一条都没被抑制过）
    rec2 = _rec(_SPAM)
    assert _decide(f, rec2) is True
    assert rec2.getMessage() == _SPAM  # 必须原样放行，不能被顶成 log_suppressed


class _BoomCreated:
    """``created`` 访问必抛，模拟非法/损坏的时间戳，触发 `_decide()` 内部
    真实异常（而非仅仅是属性写入失败）。"""

    name = "tt.gate"
    filename = "trading_state.py"
    lineno = 309
    levelno = logging.WARNING
    args = ()

    def getMessage(self) -> str:
        return _SPAM

    @property
    def created(self):
        raise RuntimeError("boom")


def test_顶层兜底_内部异常时放行不吞真实告警():
    """方向必须与 EventFilter 相反：EventFilter 判定失败 return False
    （无害，只是不进机读流）；RepeatSuppressFilter 判定失败必须 return
    True——宁可漏抑制多几行重复日志，也绝不能把一条真实告警吞进异常里。
    """
    f = RepeatSuppressFilter(window_s=60.0)
    assert f.filter(_BoomCreated()) is True


class _SlottedFakeRecord:
    """无 ``__dict__``，模拟"record 不支持动态属性写入"（例如冻结字段的
    自定义 LogRecord 子类）。刻意不留 ``_suppress_decision`` 槽位——
    ``record._suppress_decision = decision`` 这一行必然抛
    ``AttributeError``，只有 `filter()` 的顶层 try/except 能拦住。
    """

    __slots__ = ("name", "filename", "lineno", "levelno", "created", "args", "_msg")

    def __init__(self, msg: str, lineno: int = 309):
        self.name = "tt.gate"
        self.filename = "trading_state.py"
        self.lineno = lineno
        self.levelno = logging.WARNING
        self.created = 1_700_000_000.0
        self.args = ()
        self._msg = msg

    def getMessage(self) -> str:
        return self._msg


def test_顶层兜底_属性写入失败时放行而不是误判为抑制():
    f = RepeatSuppressFilter(window_s=60.0)
    rec1 = _SlottedFakeRecord(_SPAM)
    assert f.filter(rec1) is True  # 首条本就该放行
    rec2 = _SlottedFakeRecord(_SPAM)
    # 正常情况下这里该判 False（命中窗口内重复，streak 已存在）；但 record
    # 不支持写 `_suppress_decision`，顶层兜底必须接管并放行（fail-open），
    # 不能让 AttributeError 冒出去，更不能把真实内容悄悄按"抑制"丢掉。
    assert f.filter(rec2) is True


def test_streaks字典有软上限不会无限增长():
    f = RepeatSuppressFilter(window_s=60.0)
    for i in range(600):
        _decide(f, _rec(_SPAM, lineno=2000 + i))  # 每条 lineno 不同 -> 各自独立 key
    assert len(f._streaks) <= _MAX_STREAKS
