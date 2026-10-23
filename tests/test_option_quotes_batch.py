# -*- coding: utf-8 -*-
"""期权报价批量取：N 个合约一发 REST，而不是 N 发。

`OptionLatestQuoteRequest.symbol_or_symbols` 官方类型是 `Union[str, List[str]]`
（docs/vendor/alpaca/python_api/data/option/requests.md:164），但我们一直逐个合约调用。
最痛的是 `exec_reconcile.list_positions`：它被 `TradingStateHub._positions_loop` 以
**0.5 秒一拍**驱动，持仓 N 个、Option WS 又没命中时就是每拍 N 发 REST，打的正是那个
已经在限流的行情 API（2026-08-11 三次 alpaca_rate_limited）。

批量与单发**必须共用同一份 5 秒缓存**，否则两条路会各自持有不同时刻的价，
出现「面板显示一个价、下单兜底拿到另一个价」。
"""
from __future__ import annotations

from decimal import Decimal as D

from app.config import ALPACA_OPTION_FEED
from app.integrations.alpaca.data import AlpacaData

_A = "QQQ260716C00500000"
_B = "QQQ260716P00490000"
_C = "SPY260716C00600000"


class _Q:
    def __init__(self, bid, ask):
        self.bid_price, self.ask_price = bid, ask


class _FakeOptionClient:
    """记录每次请求带的符号集合；quotes 可被测试改动。"""

    def __init__(self):
        self.calls: list[list[str]] = []
        self.quotes = {_A: (1.00, 1.05), _B: (2.00, 2.06), _C: (3.00, 3.09)}

    def get_option_latest_quote(self, req):
        syms = req.symbol_or_symbols
        syms = [syms] if isinstance(syms, str) else list(syms)
        self.calls.append(syms)
        self.last_feed = getattr(req.feed, "value", req.feed)
        return {s: _Q(*self.quotes[s]) for s in syms if s in self.quotes}


def _data():
    d = AlpacaData()
    fake = _FakeOptionClient()
    d._oc = lambda: fake
    return d, fake


# ── 批量本身 ─────────────────────────────────────────────────────────────────
def test_批量取价只打一发请求():
    d, fake = _data()

    out = d.option_quotes([_A, _B, _C])

    assert len(fake.calls) == 1, f"应合并成一发，实得 {len(fake.calls)} 发"
    assert sorted(fake.calls[0]) == sorted([_A, _B, _C])
    assert out[_A] == (D("1.0"), D("1.05"))
    assert out[_C] == (D("3.0"), D("3.09"))


def test_批量请求同样钉住feed():
    d, fake = _data()
    d.option_quotes([_A, _B])
    assert fake.last_feed == ALPACA_OPTION_FEED


def test_空列表不发请求():
    d, fake = _data()
    assert d.option_quotes([]) == {}
    assert fake.calls == []


def test_响应缺失的合约不进结果也不抛():
    d, fake = _data()
    missing = "XYZ260716C00010000"

    out = d.option_quotes([_A, missing])

    assert _A in out and missing not in out


def test_重复符号只请求一次():
    d, fake = _data()
    d.option_quotes([_A, _A, _B])
    assert sorted(fake.calls[0]) == sorted([_A, _B])


# ── 与单发共用缓存 ───────────────────────────────────────────────────────────
def test_批量只请求未命中缓存的部分():
    d, fake = _data()
    d.option_quote(_A)                      # 先把 A 灌进缓存
    assert fake.calls == [[_A]]

    out = d.option_quotes([_A, _B])

    assert fake.calls[1] == [_B], "A 已在缓存里，不该重复请求"
    assert out[_A] == (D("1.0"), D("1.05"))
    assert out[_B] == (D("2.0"), D("2.06"))


def test_批量结果写回缓存供单发命中():
    d, fake = _data()
    d.option_quotes([_A, _B])
    fake.quotes[_A] = (9.99, 9.99)          # 券商侧改价，但缓存内应仍是旧值

    assert d.option_quote(_A) == (D("1.0"), D("1.05"))
    assert len(fake.calls) == 1, "第二次读该命中缓存，不该再发请求"


def test_fresh强制全部穿透缓存():
    d, fake = _data()
    d.option_quotes([_A, _B])
    fake.quotes[_A] = (5.00, 5.10)

    out = d.option_quotes([_A, _B], fresh=True)

    assert len(fake.calls) == 2
    assert sorted(fake.calls[1]) == sorted([_A, _B])
    assert out[_A] == (D("5.0"), D("5.1"))


def test_全部命中缓存时不发请求():
    d, fake = _data()
    d.option_quotes([_A, _B])
    d.option_quotes([_A, _B])
    assert len(fake.calls) == 1
