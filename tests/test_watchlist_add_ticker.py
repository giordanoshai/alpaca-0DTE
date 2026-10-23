# -*- coding: utf-8 -*-
"""盯盘页手动添加/移除 ticker 的接口行为（2026-09-05 修）。

修之前三处都是断的：
1. `POST /api/watchlist` 用的是 `insert`，而表上有 `UNIQUE (ticker)`。库里躺着 140 只
   gear 为空的候选票，加它们必然撞约束 → 500，前端只有 console.error，看着像"点了没反应"。
2. 就算加的是库里没有的新票，写进去的 gear 也是空的，而"在盯名单"接口只认 gear 非空
   → 页面一刷新这票就消失。
3. 接口不看 `add_ticker()` 的返回值 —— K 线预热失败（拿不到分钟线）照样回 success，
   前端摆一张永远算不出指标的空白卡片。

移除侧原先 `delete` 整行，会把本来就在候选仓库里的记录一并抹掉；改为只清 gear。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.watcher.watcher_manager import GLOBAL_WATCHER

client = TestClient(app)

DEFAULT_GEAR = "instant_0.15"


class _FakeTable:
    """记录一次链式调用的动作，execute() 时汇总进 client.calls。"""

    def __init__(self, owner: "_FakeClient", name: str):
        self.owner = owner
        self.name = name
        self.op: str | None = None
        self.payload = None
        self.kwargs: dict = {}
        self.filters: list[tuple[str, object]] = []

    def upsert(self, data, **kwargs):
        self.op, self.payload, self.kwargs = "upsert", data, kwargs
        return self

    def insert(self, data, **kwargs):
        self.op, self.payload, self.kwargs = "insert", data, kwargs
        return self

    def update(self, data):
        self.op, self.payload = "update", data
        return self

    def delete(self):
        self.op = "delete"
        return self

    def eq(self, key, value):
        self.filters.append((key, value))
        return self

    def execute(self):
        if self.owner.fail:
            raise RuntimeError("supabase 挂了")
        self.owner.calls.append({
            "table": self.name, "op": self.op, "payload": self.payload,
            "kwargs": self.kwargs, "filters": self.filters,
        })
        self.owner.events.append("db")
        return SimpleNamespace(data=[])


class _FakeClient:
    def __init__(self, fail: bool = False):
        self.calls: list[dict] = []
        self.events: list[str] = []
        self.fail = fail

    def table(self, name: str) -> _FakeTable:
        return _FakeTable(self, name)


@pytest.fixture
def sb(monkeypatch):
    """替掉 Supabase 客户端，返回记账用的假 client。"""
    fake = _FakeClient()
    monkeypatch.setattr("app.core.sb_client.get_supabase_client", lambda: fake)
    return fake


@pytest.fixture
def pool(monkeypatch, sb):
    """替掉盯盘池：记录 add/remove 调用与相对 db 写入的先后顺序。"""
    state = SimpleNamespace(added=[], removed=[], warmup_ok=True, bars=480)

    async def _add(ticker: str) -> bool:
        state.added.append(ticker)
        sb.events.append("warmup")
        return state.warmup_ok

    async def _remove(ticker: str) -> bool:
        state.removed.append(ticker)
        return True

    monkeypatch.setattr(GLOBAL_WATCHER, "add_ticker", _add)
    monkeypatch.setattr(GLOBAL_WATCHER, "remove_ticker", _remove)
    monkeypatch.setattr(GLOBAL_WATCHER, "bar_count", lambda t: state.bars, raising=False)
    return state


def test_add_ticker_upserts_with_default_gear(sb, pool):
    """加一只库里已存在、gear 为空的候选票：不能再撞唯一约束，且必须补上默认档位。"""
    r = client.post("/api/watchlist?ticker=aapl")

    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["ticker"] == "AAPL"

    writes = [c for c in sb.calls if c["table"] == "user_watchlist"]
    assert len(writes) == 1
    assert writes[0]["op"] == "upsert", "insert 会撞 UNIQUE(ticker)，必须用 upsert"
    assert writes[0]["payload"]["ticker"] == "AAPL"
    assert writes[0]["payload"]["gear"] == DEFAULT_GEAR, "gear 为空的话名单接口读不到，刷新就丢"
    assert "ticker" in str(writes[0]["kwargs"].get("on_conflict", "")), "冲突键得指到 ticker 这一列"


def test_add_ticker_reports_bar_count(sb, pool):
    """返回体要带 K 线根数，前端才能显示补了多少。"""
    pool.bars = 512
    r = client.post("/api/watchlist?ticker=AAPL")

    assert r.status_code == 200
    assert r.json()["bars"] == 512


def test_add_ticker_warms_bars_before_db_write(sb, pool):
    """K 线预热必须排在写库之前 —— 预热不成就不该在名单里留记录。"""
    client.post("/api/watchlist?ticker=AAPL")

    assert sb.events == ["warmup", "db"]


def test_add_ticker_rejects_when_warmup_fails(sb, pool):
    """拿不到分钟线：回 400，一行库都不写，不留空白卡片。"""
    pool.warmup_ok = False

    r = client.post("/api/watchlist?ticker=ZZZZ")

    assert r.status_code == 400
    assert sb.calls == []
    assert "K线" in r.json()["error"] or "K 线" in r.json()["error"]


def test_add_ticker_rolls_back_pool_when_db_write_fails(monkeypatch, sb, pool):
    """写库炸了要把刚加进内存池的票摘掉，不留页面上没有、后台却在跑的幽灵票。"""
    sb.fail = True

    r = client.post("/api/watchlist?ticker=AAPL")

    assert r.status_code == 500
    assert pool.added == ["AAPL"]
    assert pool.removed == ["AAPL"], "写库失败必须回滚内存池"


def test_add_index_ticker_rejected(sb, pool):
    """^ 开头是 ^VIX 这类纯展示指数，走的是另一条路，不能从这里进。"""
    r = client.post("/api/watchlist?ticker=^VIX")

    assert r.status_code == 400
    assert sb.calls == []
    assert pool.added == []


def test_remove_ticker_clears_gear_instead_of_deleting(sb, pool):
    """移除 = 退回候选仓库：只清 gear，保留那一行。"""
    r = client.delete("/api/watchlist/AAPL")

    assert r.status_code == 200
    writes = [c for c in sb.calls if c["table"] == "user_watchlist"]
    assert len(writes) == 1
    assert writes[0]["op"] == "update", "整行删掉会误伤库里原有的候选票记录"
    assert writes[0]["payload"] == {"gear": None}
    assert ("ticker", "AAPL") in writes[0]["filters"]
    assert pool.removed == ["AAPL"]
