# -*- coding: utf-8 -*-
"""点击路径没能复用协调器选中合约时，把**具体原因**记进埋点。

背景（2026-08-14 复盘 docs/reports/2026-08-13-夜盘交易复盘.md）：
08-13 夜盘 19 次点击里只有 2 次命中协调器（11%），其余 17 次全部回落去拉期权链 ——
而 `contract_ms` 的全部开销都来自这条回落路径。但埋点只有
`contract_source: chain | coordinator`，**说不出为什么没命中**。

候选原因至少四类，修法完全不同：
  - `untracked`        协调器根本没跟踪这个 (ticker, direction) → 要看盯盘池覆盖
  - `price_moved`      标的漂过 `_PRICE_MOVE_RATIO`(0.5%) → 阈值本身要不要放宽
  - `identity_stale`   超过 `_IDENTITY_TTL_S`(15s) → 扫描周期跟 TTL 贴太近了
  - `no_ws_quote`      身份有效但 Option WS 没有 ≤1s 的已确认盘口 → 要看订阅链路

盲调阈值是最坏的做法：0.5% 和 15s 都直接决定「会不会拿旧合约去下单」。
先把原因分布测出来，再决定动哪个旋钮。

**只加读，不改判据**：`_refresh_reason()` 是从 `_needs_refresh()` 拆出来的，
后者改为委托前者，行为一字不变 —— 由 `test_entry_contract_reuse.py` 里那五条
既有用例守住。
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


def _coord(*, price=D("100"), age_s=0.0, template_version="tpl-1", selected=True):
    coord = OptionCandidateCoordinator()
    state = coord._sides("APLD")["long"]
    if selected:
        state.selected = _contract()
        state.shortlist = [state.selected]
    state.anchor_price = price
    state.template_version = template_version
    state.last_discovery_at = time.monotonic() - age_s
    coord._template_version = lambda: "tpl-1"
    return coord


# ── 协调器侧：原因分支 ────────────────────────────────────────────────────────
def test_命中时报hit():
    coord = _coord()
    assert coord.selection_miss_reason("APLD", "long", price=D("100")) == "hit"


def test_未跟踪的标的报untracked():
    coord = _coord()
    assert coord.selection_miss_reason("QQQ", "long", price=D("100")) == "untracked"


def test_已跟踪但反向还没选出合约报no_selection():
    """`_sides()` 建 state 时两个方向一起建，所以反向不是 untracked 而是还没 discover。

    这个区分是有用的：untracked 要去查盯盘池覆盖，no_selection 要去查 discovery
    有没有跑完/报错，两条路完全不同。
    """
    coord = _coord()
    assert coord.selection_miss_reason("APLD", "short", price=D("100")) == "no_selection"


def test_还没选出合约报no_selection():
    coord = _coord(selected=False)
    assert coord.selection_miss_reason("APLD", "long", price=D("100")) == "no_selection"


def test_模板换版报template_changed():
    coord = _coord(template_version="tpl-old")
    assert coord.selection_miss_reason("APLD", "long", price=D("100")) == "template_changed"


def test_标的漂移超阈值报price_moved():
    coord = _coord(price=D("100"))
    assert coord.selection_miss_reason("APLD", "long", price=D("100.6")) == "price_moved"


def test_身份超龄报identity_stale(monkeypatch):
    """TTL 现按 option feed 分档（见 tests/test_identity_ttl_by_feed.py）。

    这里钉住 opra 档：本例验的是「超龄触发这条判据本身」，与档位取多少无关。
    indicative 档（delta 拿不到、定时重拉买不到东西）的放宽行为由那个文件覆盖。
    """
    monkeypatch.setattr("app.config.ALPACA_OPTION_FEED", "opra")
    coord = _coord(age_s=_IDENTITY_TTL_S + 1)
    assert coord.selection_miss_reason("APLD", "long", price=D("100")) == "identity_stale"


def test_未跟踪时不去碰模板仓库():
    """`current_selection` 在 state 为 None 时就返回了，从不读模板；本方法必须同款。"""
    coord = _coord()

    def _boom():
        raise AssertionError("未跟踪的标的不该触发模板仓库读取")

    coord._template_version = _boom

    assert coord.selection_miss_reason("QQQ", "long", price=D("100")) == "untracked"


@pytest.mark.parametrize("kwargs", [
    {},
    {"template_version": "tpl-old"},
    {"age_s": _IDENTITY_TTL_S + 1},
    {"selected": False},
])
def test_原因与实际可用性始终一致(kwargs):
    """两个方法必须同源：报 hit 就一定拿得到，报别的就一定拿不到。"""
    coord = _coord(**kwargs)

    reason = coord.selection_miss_reason("APLD", "long", price=D("100"))
    selection = coord.current_selection("APLD", "long", price=D("100"))

    assert (reason == "hit") is (selection is not None)


# ── 点击路径：原因进埋点 ──────────────────────────────────────────────────────
@pytest.fixture()
def captured(monkeypatch) -> list[dict]:
    events: list[dict] = []
    monkeypatch.setattr(
        "app.services.market_context.logger",
        SimpleNamespace(info=lambda p: events.append(json.loads(p)),
                        warning=lambda *_a, **_k: None),
    )
    return events


def _wire(monkeypatch, *, coordinator_hit, ws_quote, miss_reason="untracked"):
    signal = TickerSignal(price=D("100"),
        vwap=D("99"), atr=D("1"), vwap_sigma=D("0.5"),
    )
    account = AccountState(
        daily_pnl=D("0"), weekly_pnl=D("0"), consecutive_losses=0,
        cooldown_remaining_s=0, minutes_since_open=D("60"), buying_power=D("10000"),
    )
    monkeypatch.setattr("app.services.market_context.market_signal",
                        lambda *_a, **_k: signal)
    monkeypatch.setattr("app.services.market_context.select_contract_full",
                        lambda *_a, **_k: _contract(bid="9.00", ask="9.04"))
    monkeypatch.setattr("app.services.market_context.build_account_state",
                        lambda *_a, **_k: account)
    monkeypatch.setattr(OPTION_CANDIDATES, "current_selection",
                        lambda *_a, **_k: coordinator_hit)
    monkeypatch.setattr(OPTION_CANDIDATES, "selection_miss_reason",
                        lambda *_a, **_k: miss_reason)
    monkeypatch.setattr(OPTION_STREAM, "quote_with_age", lambda *_a, **_k: ws_quote)
    return SimpleNamespace(
        signal_online=lambda: True,
        clock=lambda: SimpleNamespace(minutes_since_open=D("60")),
    )


def _timing(events):
    hits = [e for e in events if e.get("event") == "entry_context_timing"]
    assert len(hits) == 1
    return hits[0]


def test_回落时埋点带上协调器给的原因(monkeypatch, captured):
    src = _wire(monkeypatch, coordinator_hit=None,
                ws_quote=(D("1.10"), D("1.14"), 0.3), miss_reason="price_moved")

    build_market_context(src, "APLD", "long")

    timing = _timing(captured)
    assert timing["contract_source"] == "chain"
    assert timing["reuse_miss"] == "price_moved"


def test_身份有效但WS无盘口时原因是no_ws_quote(monkeypatch, captured):
    """这一类跟阈值无关，调 0.5%/15s 是治不好的 —— 必须能跟其他原因分开。"""
    src = _wire(monkeypatch, coordinator_hit=_contract(), ws_quote=None)

    build_market_context(src, "APLD", "long")

    assert _timing(captured)["reuse_miss"] == "no_ws_quote"


def test_命中时不带未命中原因(monkeypatch, captured):
    src = _wire(monkeypatch, coordinator_hit=_contract(),
                ws_quote=(D("1.10"), D("1.14"), 0.3))

    build_market_context(src, "APLD", "long")

    timing = _timing(captured)
    assert timing["contract_source"] == "coordinator"
    assert timing.get("reuse_miss") is None


def test_协调器抛异常时原因是error且照常回落(monkeypatch, captured):
    """fail-open 铁律：复用只是加速层，出问题绝不能因此拒单。"""
    src = _wire(monkeypatch, coordinator_hit=None,
                ws_quote=(D("1.10"), D("1.14"), 0.3))

    def _boom(*_a, **_k):
        raise RuntimeError("协调器挂了")

    monkeypatch.setattr(OPTION_CANDIDATES, "current_selection", _boom)

    ctx = build_market_context(src, "APLD", "long")

    assert ctx.selected is not None, "回落必须照常出合约"
    assert _timing(captured)["reuse_miss"] == "error"


def test_取原因失败也不得冒泡(monkeypatch, captured):
    """原因只是诊断信息，它自己出问题不能反过来影响下单。"""
    src = _wire(monkeypatch, coordinator_hit=None,
                ws_quote=(D("1.10"), D("1.14"), 0.3))

    def _boom(*_a, **_k):
        raise RuntimeError("取原因挂了")

    monkeypatch.setattr(OPTION_CANDIDATES, "selection_miss_reason", _boom)

    ctx = build_market_context(src, "APLD", "long")

    assert ctx.selected is not None
    assert _timing(captured)["reuse_miss"] == "error"
