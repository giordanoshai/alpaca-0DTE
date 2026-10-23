# -*- coding: utf-8 -*-
"""点击路径复用协调器已选中的合约，不再每次重拉期权链。

背景：`build_market_context` 每次点击都调 `select_contract_full` → `contract_candidates`，
而后者的缓存 key 含 `str(price.normalize())`（`alpaca/data.py:313`）。协调器用
MARKET_FEED 的 tick 价、点击路径用 `signal.price`，两者几乎不可能逐厘相同 ——
**每次点击都是 cache miss，必然完整拉一次期权链**（还要走 `_CHAIN_WIDTHS` 最多 4 档扩宽）。
2026-08-11 实测 `contract_ms` 中位 330ms、p90 3836ms、最大 6889ms，是提交前最大的一段。

而协调器本来就为每个 (ticker, direction) 维护着选中合约、并把它推进 Option WS scope
（`option_candidates.py:179 / :284`）。点击路径直接用它即可，代价为 0。

**新鲜度契约（务必读懂再改）**：
  - 合约**身份**（strike/OCC）可以旧到 `_IDENTITY_TTL_S`(15s) 或标的漂 `_PRICE_MOVE_RATIO`(0.5%)
    为止 —— 判据直接复用协调器自己的 `_needs_refresh()`，它认为该刷新的就不给用。
  - 合约**报价**必须新鲜：bid/ask 一律从 Option WS 现取（≤1s、已 ACK 订阅），
    因为闸门要拿 mid 算同标的持仓上限与资金占用，用 15 秒前的价会低估成本、放松风控。
  - 两者任何一个拿不到 → 回落 `select_contract_full`（即今天的行为）。**fail-open**。
"""
from __future__ import annotations

import json
import time
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from app.integrations.alpaca.data import SelectedContract
from app.option_stream import OPTION_STREAM
from app.services.market_context import build_market_context
from app.services.option_candidates import (
    _IDENTITY_TTL_S,
    OPTION_CANDIDATES,
    OptionCandidateCoordinator,
)
from app.validator import AccountState, TickerSignal

_CODE = "APLD260814C00030000"


def _contract(code=_CODE, bid="1.00", ask="1.04"):
    return SelectedContract(code, D(bid), D(ask), delta=D("0.48"), delta_source="alpaca")


# ── 协调器侧：current_selection 的有效性判据 ─────────────────────────────────
def _coord_with_state(*, price=D("100"), age_s=0.0, template_version="tpl-1"):
    coord = OptionCandidateCoordinator()
    state = coord._sides("APLD")["long"]
    state.selected = _contract()
    state.shortlist = [state.selected]
    state.anchor_price = price
    state.template_version = template_version
    state.last_discovery_at = time.monotonic() - age_s
    coord._template_version = lambda: "tpl-1"
    return coord


def test_协调器选中新鲜时可复用():
    coord = _coord_with_state()
    assert coord.current_selection("APLD", "long", price=D("100")).option_code == _CODE


def test_协调器选中超龄不可复用(monkeypatch):
    """TTL 现按 option feed 分档（见 tests/test_identity_ttl_by_feed.py）。

    钉住 opra 档：本例验的是「超龄就不复用」这条判据，与档位取值无关。
    """
    monkeypatch.setattr("app.config.ALPACA_OPTION_FEED", "opra")
    coord = _coord_with_state(age_s=_IDENTITY_TTL_S + 1)
    assert coord.current_selection("APLD", "long", price=D("100")) is None


def test_标的价漂移超过阈值不可复用():
    coord = _coord_with_state(price=D("100"))
    # 0.5% 是协调器自己的换档阈值，越过就说明 ATM 档可能已经变了。
    assert coord.current_selection("APLD", "long", price=D("100.6")) is None
    assert coord.current_selection("APLD", "long", price=D("100.1")) is not None


def test_模板换版不可复用():
    coord = _coord_with_state(template_version="tpl-old")
    assert coord.current_selection("APLD", "long", price=D("100")) is None


def test_没有该标的或方向时返回None():
    coord = _coord_with_state()
    assert coord.current_selection("QQQ", "long", price=D("100")) is None
    assert coord.current_selection("APLD", "short", price=D("100")) is None


# ── 点击路径：build_market_context 的复用与回落 ──────────────────────────────
@pytest.fixture()
def captured(monkeypatch) -> list[dict]:
    events: list[dict] = []
    monkeypatch.setattr(
        "app.services.market_context.logger",
        SimpleNamespace(info=lambda p: events.append(json.loads(p)),
                        warning=lambda *_a, **_k: None),
    )
    return events


def _wire(monkeypatch, *, coordinator_hit, ws_quote):
    """接线；返回 (source, chain_calls)。chain_calls 非空 = 走了全量选约。"""
    chain_calls: list[str] = []

    signal = TickerSignal(price=D("100"),
        vwap=D("99"), atr=D("1"), vwap_sigma=D("0.5"),
    )
    account = AccountState(
        daily_pnl=D("0"), weekly_pnl=D("0"), consecutive_losses=0,
        cooldown_remaining_s=0, minutes_since_open=D("60"), buying_power=D("10000"),
    )

    def _chain(*_a, **_k):
        chain_calls.append("select_contract_full")
        return _contract(bid="9.00", ask="9.04")   # 与协调器/WS 的价明显不同，便于分辨

    monkeypatch.setattr("app.services.market_context.market_signal",
                        lambda *_a, **_k: signal)
    monkeypatch.setattr("app.services.market_context.select_contract_full", _chain)
    monkeypatch.setattr("app.services.market_context.build_account_state",
                        lambda *_a, **_k: account)
    monkeypatch.setattr(OPTION_CANDIDATES, "current_selection",
                        lambda *_a, **_k: coordinator_hit)
    monkeypatch.setattr(OPTION_STREAM, "quote_with_age", lambda *_a, **_k: ws_quote)

    src = SimpleNamespace(
        signal_online=lambda: True,
        clock=lambda: SimpleNamespace(minutes_since_open=D("60")),
    )
    return src, chain_calls


def _timing(events):
    hits = [e for e in events if e.get("event") == "entry_context_timing"]
    assert len(hits) == 1
    return hits[0]


def test_协调器命中且WS有新鲜盘口时不拉期权链(monkeypatch, captured):
    src, chain_calls = _wire(
        monkeypatch, coordinator_hit=_contract(), ws_quote=(D("1.10"), D("1.14"), 0.3))

    ctx = build_market_context(src, "APLD", "long")

    assert chain_calls == [], "协调器已经选好并订阅了，不该再拉一次期权链"
    assert ctx.selected.option_code == _CODE
    # 报价必须是 WS 现取的那份，不是协调器 15 秒前的快照。
    assert (ctx.selected.bid, ctx.selected.ask) == (D("1.10"), D("1.14"))
    assert _timing(captured)["contract_source"] == "coordinator"


def test_协调器没命中时回落全量选约(monkeypatch, captured):
    src, chain_calls = _wire(
        monkeypatch, coordinator_hit=None, ws_quote=(D("1.10"), D("1.14"), 0.3))

    ctx = build_market_context(src, "APLD", "long")

    assert chain_calls == ["select_contract_full"]
    assert (ctx.selected.bid, ctx.selected.ask) == (D("9.00"), D("9.04"))
    assert _timing(captured)["contract_source"] == "chain"


def test_协调器命中但WS无新鲜盘口时回落全量选约(monkeypatch, captured):
    """闸门要用 mid 算持仓上限与资金占用，拿不到新鲜价就不能省这一步。"""
    src, chain_calls = _wire(
        monkeypatch, coordinator_hit=_contract(), ws_quote=None)

    build_market_context(src, "APLD", "long")

    assert chain_calls == ["select_contract_full"]
    assert _timing(captured)["contract_source"] == "chain"


def test_协调器查询异常不得冒泡_照常回落(monkeypatch, captured):
    src, chain_calls = _wire(
        monkeypatch, coordinator_hit=None, ws_quote=(D("1.10"), D("1.14"), 0.3))

    def _boom(*_a, **_k):
        raise RuntimeError("协调器挂了")

    monkeypatch.setattr(OPTION_CANDIDATES, "current_selection", _boom)

    ctx = build_market_context(src, "APLD", "long")

    assert chain_calls == ["select_contract_full"]
    assert ctx.selected is not None


# ── 卡片预览走同一条复用路径 ─────────────────────────────────────────────────
# contract_preview 是**前端每张卡片持续调用**的背景负载，打的是同一个正在限流的行情
# API（08-11 三次 alpaca_rate_limited 全来自 cands-adaptive 期权链）。它此前每次缓存
# 失效就 entry_plan(拉链) + option_quote(再一发 REST)，两发都能省。

def test_entry_plan命中协调器时不拉期权链(monkeypatch, captured):
    from app.services.market_context import entry_plan

    src, chain_calls = _wire(
        monkeypatch, coordinator_hit=_contract(), ws_quote=(D("1.10"), D("1.14"), 0.3))

    sel = entry_plan(src, "APLD", "long")

    assert chain_calls == []
    assert sel.option_code == _CODE
    assert (sel.bid, sel.ask) == (D("1.10"), D("1.14"))


def test_预览命中协调器时不再打REST报价(monkeypatch):
    """协调器给的合约已带 WS 现价（≤1s），再打一发 option_quote 纯属浪费。"""
    import app.exec_entry as exec_entry

    src, chain_calls = _wire(
        monkeypatch, coordinator_hit=_contract(), ws_quote=(D("1.10"), D("1.14"), 0.3))
    monkeypatch.setattr("app.services.market_context.logger",
                        SimpleNamespace(info=lambda *_a, **_k: None,
                                        warning=lambda *_a, **_k: None))
    rest_calls: list[str] = []
    src.name = "watcher"
    src.option_quote = lambda code, fresh=False: rest_calls.append(code) or (D("7"), D("8"))
    exec_entry._PREVIEW_CACHE.clear()

    res = exec_entry.contract_preview(src, "APLD", "long")

    assert res["available"] is True
    assert chain_calls == [], "协调器已选好，不该再拉链"
    assert rest_calls == [], "WS 已给出现价，不该再打 REST"
    assert (res["bid"], res["ask"]) == ("1.10", "1.14")


def test_预览未命中协调器时维持原行为(monkeypatch):
    """回落路径必须一字不变：拉链 + REST 刷价。"""
    import app.exec_entry as exec_entry

    src, chain_calls = _wire(monkeypatch, coordinator_hit=None, ws_quote=None)
    monkeypatch.setattr("app.services.market_context.logger",
                        SimpleNamespace(info=lambda *_a, **_k: None,
                                        warning=lambda *_a, **_k: None))
    rest_calls: list[str] = []
    src.name = "watcher"
    src.option_quote = lambda code, fresh=False: rest_calls.append(code) or (D("7"), D("8"))
    exec_entry._PREVIEW_CACHE.clear()

    res = exec_entry.contract_preview(src, "APLD", "long")

    assert chain_calls == ["select_contract_full"]
    assert rest_calls == [_CODE]
    assert (res["bid"], res["ask"]) == ("7", "8")
