# -*- coding: utf-8 -*-
"""期权报价新鲜度：追单必须每手重取真实报价，不能吃 5 秒缓存。

2026-07-25 审查发现：`AlpacaData.option_quote` 有 5s 缓存，而 `atomic_close` 的穿价追单
是 attempts=3 × wait_s=1.5 = 4.5 秒内跑完 —— 三手命中的是**同一条缓存**。于是注释里写的
「每手撤单、重取报价、更深重挂」实际只是把**同一个旧 bid** 依次减去 1/3、2/3、3/3 的
spread：报价一跳走，三手都在追一个已经不存在的价位。这与「0DTE 三秒内权利金变化可观」
（本系统 watchdog 拍频 3s→1s 的既定理由）直接冲突。

缓存本身要保留——/api/positions 每 2 秒轮询 N 个持仓，取消缓存会打爆 Alpaca。
故加 fresh 旗标：只有追单这种「定价依据」的读取强制穿透，展示类读取照旧吃缓存。
"""
from decimal import Decimal as D

from app.integrations.alpaca.data import AlpacaData
from app.order_pricing import atomic_close

_CODE = "QQQ260716C00500000"


class _FakeQuoteItem:
    def __init__(self, bid, ask):
        self.bid_price, self.ask_price = bid, ask


class _FakeOptionClient:
    """记录真实往返次数；bid 可被测试改动以模拟报价跳动。"""

    def __init__(self, bid=1.00):
        self.calls = 0
        self.bid = bid

    def get_option_latest_quote(self, req):
        self.calls += 1
        code = req.symbol_or_symbols
        return {code: _FakeQuoteItem(self.bid, self.bid + 0.10)}


def _data_with_fake():
    d = AlpacaData()
    fake = _FakeOptionClient()
    d._oc = lambda: fake
    return d, fake


# ── 缓存行为 ──────────────────────────────────────────────────────────────────
def test_option_quote_uses_cache_by_default():
    """默认读走缓存：连续两次只打一次券商（保护 /api/positions 轮询）。"""
    d, fake = _data_with_fake()
    d.option_quote(_CODE)
    d.option_quote(_CODE)
    assert fake.calls == 1


def test_option_quote_fresh_bypasses_cache():
    """fresh=True 强制穿透缓存，拿到跳动后的新报价。"""
    d, fake = _data_with_fake()
    assert d.option_quote(_CODE) == (D("1.0"), D("1.1"))
    fake.bid = 0.80                      # 报价跳走
    bid, ask = d.option_quote(_CODE, fresh=True)
    assert fake.calls == 2
    assert bid == D("0.8")


def test_fresh_read_refreshes_cache_for_later_readers():
    """fresh 取到的新值要写回缓存，否则紧随其后的普通读又拿回旧价（前后不一致）。"""
    d, fake = _data_with_fake()
    d.option_quote(_CODE)
    fake.bid = 0.80
    d.option_quote(_CODE, fresh=True)
    assert d.option_quote(_CODE) == (D("0.8"), D("0.9"))
    assert fake.calls == 2               # 第三次读命中被刷新过的缓存


# ── 追单必须用 fresh ──────────────────────────────────────────────────────────
class _FakeEC:
    def __init__(self, qty):
        self.qty = qty
        self.submits = 0
        self.cancelled = set()

    def open_order_ids_for(self, symbol):
        return []

    def cancel(self, oid):
        self.cancelled.add(oid)

    def submit(self, req):
        self.submits += 1
        return {"id": f"s{self.submits}"}

    def position_qty(self, symbol):
        return self.qty

    def get_order(self, oid):
        return {
            "status": "canceled" if oid in self.cancelled else "new",
            "filled_qty": "0",
            "filled_avg_price": None,
        }


class _RecordingAlpaca:
    def __init__(self, quote):
        self._q = quote
        self.fresh_flags = []

    def option_quote(self, code, fresh=False):
        self.fresh_flags.append(fresh)
        return self._q


def test_chase_requests_fresh_quote_on_every_attempt():
    """穿价追单每一手都强制重取报价——这是「追单」二字的全部意义所在。"""
    ec = _FakeEC(qty=2)                  # 持仓不降 → 三手全部未成交
    alpaca = _RecordingAlpaca((D("1.00"), D("1.10")))
    atomic_close(ec, alpaca, _CODE, 2, "watchdog:止损",
                 attempts=3, wait_s=0.02, poll_s=0.01)
    assert alpaca.fresh_flags == [True, True, True]


def test_single_shot_fallback_also_uses_fresh_quote():
    """单发回退同样是在给真实委托定价，不能拿陈旧报价挂出去。"""
    ec = _FakeEC(qty=2)
    alpaca = _RecordingAlpaca((D("1.00"), D("1.10")))
    atomic_close(ec, alpaca, _CODE, 2, "手动平仓",
                 price_type="ask", wait_s=0.02, poll_s=0.01)
    assert alpaca.fresh_flags == [True]
