"""Alpaca 429 有界退避重试。

背景（2026-08-11 诊断，docs/reports/2026-08-11-下单卡顿与拦截诊断.md）：
08-10 三次撞到 Alpaca 限流 —— `{"message": "too many requests."}`，落在
`exec_reconcile.py:153`（持仓报价）与 `watchdog.py:795`（对账巡检）。
`app/integrations/alpaca/` 当时 `retry|backoff|sleep|429` 全部 0 命中，
限流一到就抛异常，持仓报价直接空掉一拍。

作用域铁律：重试只包在 `AlpacaData._cached()` 这个**只读取数**收口上。
下单/撤单在 `trading.py`，是非幂等调用 —— 429 之后原请求可能已经到达券商，
重试会造成重复下单。本文件用 `test_下单不得被重试` 守住这条边界。

代价取舍：只读重试会给点击路径最多加约 0.75s（两次退避），但只在真撞限流时
发生（08-10 全天 3 次）。相比拿不到报价直接拒单/挂错价，这个代价是划算的。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.integrations.alpaca import data as data_mod
from app.integrations.alpaca.data import AlpacaData


class _RateLimited(Exception):
    """复刻 Alpaca SDK 的 APIError 形态：正文就是那句 too many requests。"""

    def __str__(self) -> str:
        return '{"message": "too many requests."}\n'


@pytest.fixture()
def slept(monkeypatch) -> list[float]:
    """拦掉真实 sleep，既让测试跑得快，也让退避时长可断言。"""
    delays: list[float] = []
    monkeypatch.setattr(data_mod.time, "sleep", delays.append)
    return delays


def _calls_until(n_failures: int, exc_factory):
    """前 n_failures 次抛异常，之后返回 "ok"；同时记录被调用次数。"""
    state = {"calls": 0}

    def fn():
        state["calls"] += 1
        if state["calls"] <= n_failures:
            raise exc_factory()
        return "ok"

    return fn, state


def test_限流后重试并最终成功(slept):
    fn, state = _calls_until(2, _RateLimited)

    got = AlpacaData()._cached("k", 0, fn)

    assert got == "ok"
    assert state["calls"] == 3, "应为 1 次原调用 + 2 次重试"


def test_退避时长递增(slept):
    fn, _ = _calls_until(2, _RateLimited)

    AlpacaData()._cached("k", 0, fn)

    assert len(slept) == 2
    assert slept[0] > 0
    assert slept[1] > slept[0], "第二次退避必须比第一次长"


def test_非限流错误立即抛出不重试(slept):
    fn, state = _calls_until(1, lambda: ValueError("期权链解析失败"))

    with pytest.raises(ValueError):
        AlpacaData()._cached("k", 0, fn)

    assert state["calls"] == 1, "非 429 不该浪费时间重试"
    assert slept == []


def test_持续限流耗尽预算后如实抛出(slept):
    fn, state = _calls_until(99, _RateLimited)

    with pytest.raises(_RateLimited):
        AlpacaData()._cached("k", 0, fn)

    assert state["calls"] == 3, "重试必须有界，不能无限刷券商"


def test_成功结果照常写回缓存(slept):
    """重试包装不得破坏 _cached 原有的缓存语义。"""
    fn, state = _calls_until(1, _RateLimited)
    client = AlpacaData()

    assert client._cached("k", 999, fn) == "ok"
    assert client._cached("k", 999, fn) == "ok"
    assert state["calls"] == 2, "第二次应命中缓存，不再调用 fn"


def test_下单不得被重试(slept):
    """非幂等调用的边界：429 之后原单可能已到券商，重试会造成重复下单。"""
    from app.integrations.alpaca.trading import AlpacaTrading

    state = {"calls": 0}

    def _submit_order(_req):
        state["calls"] += 1
        raise _RateLimited()

    trading = object.__new__(AlpacaTrading)
    trading._tc = SimpleNamespace(submit_order=_submit_order)

    with pytest.raises(_RateLimited):
        trading.submit(object())

    assert state["calls"] == 1, "submit 只能打一次，绝不重试"
    assert slept == []
