# tests/test_audit_trade_fields.py
"""audit() 的事件级字段 —— 锁死 trade_id 解析规则。

74 处 audit() 调用里绝大多数已经在传 order_id，而 ttt_trades 的唯一键本来就是
「入场买单 order_id」。但 order_id 在平仓类事件里装的是**平仓单自己的 id**
（入场单 id 在 parent_order_id 里，或者要去 JOURNAL 查），所以 trade_id 不是
简单回落 order_id，而是走 ``_resolve_trade_id()``：显式传入 > 调用方给的
parent_order_id > 查 JOURNAL 换成入场单 id > 查不到则用 order_id 原值。
2026-08-04 审查第 1 轮修正：最初设计直接回落 order_id 会让同一笔交易的开仓/
平仓事件带不同 trade_id，「grep 一个 ID 重建全链路」静默失效。
"""
from __future__ import annotations

import json

import pytest

import app.trade_recorder as tr


@pytest.fixture()
def 捕获(monkeypatch):
    收 = []
    monkeypatch.setattr(tr.audit_logger, "info", lambda m: 收.append(json.loads(m)))
    return 收


class _假订单事件:
    """替身 OrderEvent：只需要 parent_order_id 这一个属性。"""
    def __init__(self, parent_order_id=None):
        self.parent_order_id = parent_order_id


class _假账本:
    """替身 JOURNAL：从内存字典查，不触发真实数据库/持久化副作用。"""
    def __init__(self, 表: dict):
        self._表 = 表

    def get(self, order_id):
        return self._表.get(order_id)


class _会爆炸的账本:
    """查找即抛异常的假账本，用来证明「已给 parent_order_id 时不查账本」。"""
    def get(self, order_id):
        raise AssertionError("不该查账本：调用方已经给了 parent_order_id")


def test_trade_id_未传时回落order_id(捕获):
    tr.audit("trade_close_result", order_id="855a0f47", close_order_id="d0b97f40")
    assert 捕获[0]["trade_id"] == "855a0f47"
    assert 捕获[0]["order_id"] == "855a0f47"
    assert 捕获[0]["close_order_id"] == "d0b97f40"


def test_显式trade_id优先于order_id(捕获):
    tr.audit("trade_close_order_submitted", order_id="不是入场单", trade_id="855a0f47")
    assert 捕获[0]["trade_id"] == "855a0f47"


def test_没有order_id时不产出trade_id键(捕获):
    tr.audit("trade_intent_accepted", intent_id="bic67ac21236")
    assert "trade_id" not in 捕获[0]
    assert 捕获[0]["intent_id"] == "bic67ac21236"


def test_broker_ts原样落不做派生计算(捕获):
    tr.audit("trade_order_filled", order_id="855a0f47",
             broker_ts="2026-08-03T13:40:45.298+00:00")
    out = 捕获[0]
    assert out["broker_ts"] == "2026-08-03T13:40:45.298+00:00"
    assert not any(k.startswith("clock_skew") for k in out), (
        "单对时间戳里时钟偏移与真实延迟不可分离，算出来是假精度")


def test_None值的事件级字段不进payload(捕获):
    tr.audit("x", order_id="a", intent_id=None, broker_ts=None)
    assert "intent_id" not in 捕获[0]
    assert "broker_ts" not in 捕获[0]


def test_序列化失败仍不抛(monkeypatch):
    class 不可序列化:
        def __str__(self): raise RuntimeError("炸")
    tr.audit("x", order_id="a", weird=不可序列化())  # 不得抛


# ---- 2026-08-04 审查第 1 轮修正：trade_id 必须换成入场单 id，不是原样回落 order_id ----

def test_卖单事件回落入场单id而非平仓单id(monkeypatch, 捕获):
    """order_id 是平仓单自己的 id；账本里记着它的 parent_order_id 是入场单 id。"""
    monkeypatch.setattr(tr, "JOURNAL", _假账本({
        "close-order-99": _假订单事件(parent_order_id="entry-order-1"),
    }))
    tr.audit("trade_close_order_filled", order_id="close-order-99")
    assert 捕获[0]["trade_id"] == "entry-order-1"


def test_买单事件账本parent_order_id为空回落自己(monkeypatch, 捕获):
    """入场买单在账本里没有 parent_order_id（它自己就是链路起点）。"""
    monkeypatch.setattr(tr, "JOURNAL", _假账本({
        "entry-order-1": _假订单事件(parent_order_id=None),
    }))
    tr.audit("trade_order_filled", order_id="entry-order-1")
    assert 捕获[0]["trade_id"] == "entry-order-1"


def test_调用方已给parent_order_id不查账本(monkeypatch, 捕获):
    """模拟 _order_audit_context() 那种已经算好 parent_order_id 的调用；

    用会抛异常的假账本证明：只要 fields 里有 parent_order_id，压根不会碰 JOURNAL.get()。
    """
    monkeypatch.setattr(tr, "JOURNAL", _会爆炸的账本())
    tr.audit("trade_cancel_confirmed", order_id="close-order-99",
             parent_order_id="entry-order-1")
    assert 捕获[0]["trade_id"] == "entry-order-1"


def test_账本查不到该order_id回落原值(monkeypatch, 捕获):
    """闸门拦截阶段订单还没进账本这类场景：查无此单，回落 order_id 原值，不抛异常。"""
    monkeypatch.setattr(tr, "JOURNAL", _假账本({}))
    tr.audit("trade_intent_rejected", order_id="ghost-order")
    assert 捕获[0]["trade_id"] == "ghost-order"


def test_回归_开仓平仓事件trade_id必须相同(monkeypatch, 捕获):
    """锁死这次的缺陷：同一笔交易的开仓事件与平仓事件必须带同一个 trade_id。

    平仓事件的 order_id 是平仓单自己的 id（不是入场单 id）——这条测试的存在
    意义就是防止日后有人把 trade_id 解析改回「直接回落 order_id」。
    """
    入场单id, 平仓单id = "entry-order-1", "close-order-99"
    monkeypatch.setattr(tr, "JOURNAL", _假账本({
        入场单id: _假订单事件(parent_order_id=None),
        平仓单id: _假订单事件(parent_order_id=入场单id),
    }))
    tr.audit("trade_order_filled", order_id=入场单id)         # 开仓事件
    tr.audit("trade_close_order_filled", order_id=平仓单id)   # 平仓事件

    assert 捕获[0]["trade_id"] == 捕获[1]["trade_id"] == 入场单id
