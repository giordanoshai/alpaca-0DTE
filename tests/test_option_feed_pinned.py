# -*- coding: utf-8 -*-
"""期权 REST 请求必须显式钉住 feed，与 WS 同源。

Alpaca SDK 的 `OptionChainRequest` / `OptionLatestQuoteRequest` 的 `feed` 默认是
**None → 服务端按订阅决定**（官方文档原文：`Default: opra if the user has the options
subscription, indicative otherwise`，见 docs/vendor/alpaca/python_api/data/option/requests.md）。
而 `OPTION_STREAM` 走的是 `config.ALPACA_OPTION_FEED`（默认 indicative，
`OPTION_STREAM_URL = .../v1beta1/{feed}`）。

两边一个隐式一个显式 → **哪天账户订阅了 OPRA，REST 会自动跳到 opra 而 WS 还留在
indicative**，于是入场链路出现「选约/兜底报价来自 A 源、WS 优先层来自 B 源」的静默分叉：
同一个合约两个价，闸门用一个、下单用另一个，且日志里看不出来。

故把 feed 钉死成同一个配置项：一个开关同时管住 REST 与 WS。
"""
from __future__ import annotations

from decimal import Decimal as D

from app.config import ALPACA_OPTION_FEED
from app.integrations.alpaca.data import AlpacaData

_CODE = "QQQ260716C00500000"


class _CapturingOptionClient:
    def __init__(self):
        self.requests = []

    def get_option_latest_quote(self, req):
        self.requests.append(req)
        return {_CODE: type("Q", (), {"bid_price": 1.0, "ask_price": 1.1})()}

    def get_option_chain(self, req):
        self.requests.append(req)
        return {}


def _data():
    d = AlpacaData()
    cap = _CapturingOptionClient()
    d._oc = lambda: cap
    return d, cap


def _feed_value(req):
    """SDK 里 feed 可能是 OptionsFeed 枚举，取其 value 做比较。"""
    return getattr(req.feed, "value", req.feed)


def test_最新报价请求钉住配置的feed():
    d, cap = _data()
    d.option_quote(_CODE, fresh=True)

    assert len(cap.requests) == 1
    assert _feed_value(cap.requests[0]) == ALPACA_OPTION_FEED


def test_期权链请求钉住配置的feed(monkeypatch):
    d, cap = _data()
    monkeypatch.setattr(d, "nearest_expiry", lambda _t: __import__("datetime").date(2026, 8, 14))

    d.contract_candidates(
        "QQQ", "long", D("500"),
        min_delta=D("0.40"), max_spread_pct=D("0.20"),
        max_contract_cost=D("600"), template_version="tpl-1",
    )

    assert cap.requests, "应至少发起一次期权链请求"
    for req in cap.requests:
        assert _feed_value(req) == ALPACA_OPTION_FEED


def test_feed取值必须是SDK认识的枚举值():
    """配置写错（如 'OPRA' 大写或拼错）会让服务端静默回退，必须在启动期就是合法值。"""
    from alpaca.data.enums import OptionsFeed

    assert ALPACA_OPTION_FEED in {m.value for m in OptionsFeed}
