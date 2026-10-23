"""动态白名单 = watcher 内存票池（不再每次 gate 打 DB）。

白名单 = GLOBAL_WATCHER 正在盯的标的，排除 ^ 前缀指数；空/取不到 → 空 tuple（fail-closed）。
"""
from types import SimpleNamespace

import app.watcher.watcher_manager as wm
from app.order_gate import _get_dynamic_whitelist, active_config


def _fake_watcher(states: dict):
    return SimpleNamespace(get_all_states=lambda: states)


def test_whitelist_from_memory_pool(monkeypatch):
    monkeypatch.setattr(wm, "GLOBAL_WATCHER", _fake_watcher({"qqq": {}, "NVDA": {}, "^VIX": {}}))
    wl = _get_dynamic_whitelist()
    assert set(wl) == {"QQQ", "NVDA"}  # 大写归一 + 排除 ^ 指数


def test_whitelist_empty_pool_fails_closed(monkeypatch):
    monkeypatch.setattr(wm, "GLOBAL_WATCHER", _fake_watcher({}))
    assert _get_dynamic_whitelist() == ()


def test_whitelist_unavailable_fails_closed(monkeypatch):
    def boom():
        raise RuntimeError("watcher 未就绪")
    monkeypatch.setattr(wm, "GLOBAL_WATCHER", SimpleNamespace(get_all_states=boom))
    assert _get_dynamic_whitelist() == ()


def test_active_config_uses_only_watcher_pool(monkeypatch):
    monkeypatch.setattr(wm, "GLOBAL_WATCHER", _fake_watcher({"nvda": {}, "SPX": {}, "^VIX": {}}))
    assert active_config().whitelist == ("NVDA", "SPX")


def test_active_config_empty_watcher_pool_fails_closed(monkeypatch):
    monkeypatch.setattr(wm, "GLOBAL_WATCHER", _fake_watcher({}))
    assert active_config().whitelist == ()
