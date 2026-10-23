# -*- coding: utf-8 -*-
"""提交价优先读期权 WS 内存盘口，REST 只作兜底。

背景：`option_candidates` 协调器已经把每个 (ticker, direction) 的选中合约推进
Option WS scope（`option_candidates.py:179 / :284`，`PRIORITY_CANDIDATE`），盘口就在
内存里；而点击路径的 `_quote_at_submit` 一直无视它、每次都打一发 REST
（2026-08-11 实测 `quote_ms` 中位 276ms、p90 3598ms、最大 6721ms）。0DTE 上这几百毫秒
到几秒足以让 ask 抬走一整档。

**纪律：三级降级、全程 fail-open。** 2026-08-10 那次「强制要求已确认 WS 盘口、否则
拒单」的实验当天造成 9 笔 `trade_order_rejected`（`合约 XXX 缺少 1 秒内且已确认订阅的
期权 WS 盘口，拒绝使用 REST 或旧选约快照开仓`）被整体回退——点击路径的 `ctx.selected`
来自闸门自己那次选约，与协调器 15s 刷一次的 `state.selected` 不保证同一个 OCC，换档
瞬间点下去就没有 confirmed 报价。故本层只做「有则更快」，**任何一级拿不到都不得拦下单**，
与 `_quote_at_submit` docstring 既定的降级纪律一致。
"""
from __future__ import annotations

from decimal import Decimal as D
from types import SimpleNamespace

import app.exec_entry as exec_entry
from app.option_stream import OPTION_STREAM

_CODE = "QQQ260716C00500000"


def _sel():
    """选约快照：最后一级兜底价。"""
    return SimpleNamespace(option_code=_CODE, bid=D("0.90"), ask=D("0.95"))


def _source(quote=None, *, boom=False):
    calls: list[bool] = []

    def _option_quote(_code, fresh=False):
        calls.append(fresh)
        if boom:
            raise RuntimeError("REST 挂了")
        return quote

    src = SimpleNamespace(alpaca=SimpleNamespace(option_quote=_option_quote))
    return src, calls


def _stub_ws(monkeypatch, result):
    seen: list[dict] = []

    def _quote_with_age(code, *, max_age_s=None, require_confirmed=False,
                        require_origin=None):
        seen.append({"code": code, "max_age_s": max_age_s,
                     "require_confirmed": require_confirmed})
        return result

    monkeypatch.setattr(OPTION_STREAM, "quote_with_age", _quote_with_age)
    return seen


# ── WS 命中 ───────────────────────────────────────────────────────────────────
def test_ws盘口命中时直接用_不打REST(monkeypatch):
    seen = _stub_ws(monkeypatch, (D("1.20"), D("1.25"), 0.2))
    src, rest_calls = _source((D("9.99"), D("9.99")))

    bid, ask, origin = exec_entry._quote_at_submit(src, _sel())

    assert (bid, ask) == (D("1.20"), D("1.25"))
    assert origin == "ws"
    assert rest_calls == [], "WS 命中就不该再打 REST"


def test_ws读取必须要求已确认订阅且限定新鲜度(monkeypatch):
    seen = _stub_ws(monkeypatch, (D("1.20"), D("1.25"), 0.1))
    src, _ = _source((D("1.00"), D("1.05")))

    exec_entry._quote_at_submit(src, _sel())

    assert len(seen) == 1
    # 只认当前连接上已 ACK 的订阅，避免吃到上一次连接残留的缓存。
    assert seen[0]["require_confirmed"] is True
    # 给真实委托定价，不能用接近 staleness_s(5s) 上限的陈旧帧。
    assert seen[0]["max_age_s"] is not None and seen[0]["max_age_s"] <= 1.0


# ── 逐级降级（全部 fail-open）────────────────────────────────────────────────
def test_ws没有时回落REST(monkeypatch):
    _stub_ws(monkeypatch, None)
    src, rest_calls = _source((D("1.00"), D("1.05")))

    bid, ask, origin = exec_entry._quote_at_submit(src, _sel())

    assert (bid, ask) == (D("1.00"), D("1.05"))
    assert origin == "rest"
    assert rest_calls == [True], "兜底 REST 必须 fresh，否则吃 5s 缓存"


def test_ws与REST都拿不到时回落选约快照且绝不拦单(monkeypatch):
    _stub_ws(monkeypatch, None)
    src, _ = _source(boom=True)

    bid, ask, origin = exec_entry._quote_at_submit(src, _sel())

    # 挂个可能偏低的价最多是不成交（5 秒后照常撤单），拒单是直接丢掉这次机会。
    assert (bid, ask) == (D("0.90"), D("0.95"))
    assert origin == "snapshot"


def test_ws异常不得冒泡_照常回落REST(monkeypatch):
    def _boom(*_a, **_k):
        raise RuntimeError("WS 缓存读挂了")

    monkeypatch.setattr(OPTION_STREAM, "quote_with_age", _boom)
    src, rest_calls = _source((D("1.00"), D("1.05")))

    bid, ask, origin = exec_entry._quote_at_submit(src, _sel())

    assert (bid, ask) == (D("1.00"), D("1.05"))
    assert origin == "rest"
    assert rest_calls == [True]


def test_数据源没有alpaca时直接用快照(monkeypatch):
    _stub_ws(monkeypatch, None)

    bid, ask, origin = exec_entry._quote_at_submit(SimpleNamespace(), _sel())

    assert (bid, ask) == (D("0.90"), D("0.95"))
    assert origin == "snapshot"
