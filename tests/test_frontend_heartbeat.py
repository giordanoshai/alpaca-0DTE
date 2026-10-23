"""交易页头部分别显示浏览器 Ping 与 Alpaca Pong。"""
from pathlib import Path
from frontend_src import app_js


ROOT = Path(__file__).resolve().parents[1]


def test_header_has_stable_ping_and_alpaca_pong_nodes():
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")

    assert html.count('id="header-heartbeat"') == 1
    assert html.count('id="header-heartbeat-label"') == 1
    assert html.count('id="header-alpaca-pong"') == 1
    assert html.count('id="header-alpaca-pong-label"') == 1


def test_clock_timer_no_longer_searches_and_rewrites_first_heartbeat():
    js = app_js()

    assert "querySelectorAll('span')).find" not in js
    assert "'#header-heartbeat-label'" in js
    assert "'#header-alpaca-pong-label'" in js
    assert "app.refreshHeartbeat();" in js


def test_form_refresh_is_deferred_and_replayed_after_focusout():
    js = app_js()

    assert "formRenderHeld = true" in js
    assert "window.addEventListener('focusout'" in js
    assert "scheduleRender({ soft: true })" in js


def test_production_header_has_no_demo_or_data_source_switch():
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    js = app_js()

    assert "demoStates" not in html
    assert "REAL 库真实" not in js
    assert "mountDataSourceToggle" not in js
    assert "/api/config/data-source" not in js
    assert "dataMode" not in js
    assert "real 练手模式" not in js
    assert "_reduceLocal" not in js
    assert "fetch('/api/order'" not in js
    assert "realized: 0" not in js
    assert "realized: Number(p.realized_pnl || 0)" in js


def test_risk_cards_show_only_real_data_sources():
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")

    assert "模拟 · 暂无实际作用" not in html
    assert html.count("真实 · 风控口径") == 1
    assert html.count("Alpaca · 本程序成交") == 2
    assert html.count("真实 · 入场成本") == 1
    assert "今日本程序盈亏 / 止盈·止损" in html
    assert "本周本程序盈亏 / 周亏线" in html
    assert "当前持仓资金" in html
