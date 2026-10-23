# -*- coding: utf-8 -*-
"""⑩ 价格类型的「可成交 / 被动」判定。

2026-07-25 QQQ 事件根因：前端买卖共用一个 priceType（默认 ask=卖一），减仓时把它原样
传给 /api/close → atomic_close 走单发回退分支、以**卖一**挂出被动卖单 → 排在卖方队列里
等人来买，0DTE 上基本不成交（用户观察：Alpaca 仍 2 手、1 手在挂单）。

is_marketable 把「这个价格类型对这个方向是吃单还是挂单」变成可判定的纯函数，供
atomic_close 标记 passive、close_run 如实告知用户"你这单是被动排队、可能不会成交"。
"""
from decimal import Decimal as D

import pytest

from app.order_pricing import is_marketable, price_for


# ── 卖出 ──────────────────────────────────────────────────────────────────────
def test_sell_at_bid_is_marketable():
    """卖挂买一 = 吃掉买方挂单 → 可成交。"""
    assert is_marketable("sell", "bid") is True


def test_sell_at_ask_is_passive():
    """卖挂卖一 = 排进卖方队列等人来买 → 被动，这正是 QQQ 减仓不成交的原因。"""
    assert is_marketable("sell", "ask") is False


# ── 买入 ──────────────────────────────────────────────────────────────────────
def test_buy_at_ask_is_marketable():
    """买挂卖一 = 吃掉卖方挂单 → 可成交（开仓默认就该是这个）。"""
    assert is_marketable("buy", "ask") is True


def test_buy_at_bid_is_passive():
    """买挂买一 = 排进买方队列 → 被动。"""
    assert is_marketable("buy", "bid") is False


# ── market / limit ────────────────────────────────────────────────────────────
def test_market_is_marketable_on_both_sides():
    """market 向对手方向追档，两个方向都是吃单。"""
    assert is_marketable("buy", "market") is True
    assert is_marketable("sell", "market") is True


def test_limit_is_treated_as_marketable():
    """limit = 调用方显式指定价位，不替他二次猜测 → 不标被动（不打扰）。"""
    assert is_marketable("buy", "limit") is True
    assert is_marketable("sell", "limit") is True


def test_mid_uses_rounded_midpoint_and_is_passive():
    """中间价使用一分 tick 四舍五入；它不保证立即成交。"""
    assert price_for("buy", "mid", D("1.00"), D("1.01")) == D("1.01")
    assert price_for("sell", "mid", D("1.00"), D("1.02")) == D("1.01")
    assert is_marketable("buy", "mid") is False
    assert is_marketable("sell", "mid") is False


# ── 归一化与非法输入 ──────────────────────────────────────────────────────────
def test_case_insensitive():
    assert is_marketable("SELL", "ASK") is False
    assert is_marketable("Buy", "Ask") is True


def test_unknown_side_raises():
    with pytest.raises(ValueError):
        is_marketable("hold", "bid")


def test_unknown_price_type_raises():
    with pytest.raises(ValueError):
        is_marketable("sell", "midpoint")
