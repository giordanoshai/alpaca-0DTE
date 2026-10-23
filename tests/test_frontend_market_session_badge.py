"""顶栏「休市」徽章。

休市时行情循环整体挂起（后端 `app/core/market_gate.py`），卡片会冻在收盘时的
值。徽章的作用就是让人一眼看出这是残影而不是实时价。
"""
from __future__ import annotations

from tests.frontend_src import app_js, STATIC


def _index_html() -> str:
    return (STATIC / "index.html").read_text(encoding="utf-8")


def test_header_has_session_badge_nodes():
    html = _index_html()
    assert 'id="header-session"' in html
    assert 'id="header-session-label"' in html


def test_session_badge_hidden_when_open():
    js = app_js()
    start = js.index("  sessionView() {")
    body = js[start:js.index("\n  }", start)]
    # 盘中不显示：一天里绝大多数时间都是休市，盘中还挂个徽章反而是噪音
    assert "'none'" in body
    assert "closed" in body


def test_market_session_handled_before_the_no_feed_return():
    """顺序钉死：`market_session` 必须处理在 `!hasFeed` 那条 return **之上**。

    收盘后 feed 内容不再变化、revision 不涨，放在 return 下面就永远收不到
    「休市」—— 而那正是它最该显示的时候。资金潮徽章已经踩过同一个坑，
    runtime.js 里留着注释。
    """
    js = app_js()
    handled = js.index("hasOwn(msg, 'market_session')")
    no_feed_return = js.index("if (!hasFeed || !app.acceptSectionRevision('feed'")
    assert handled < no_feed_return


def test_refresh_session_does_not_schedule_a_render():
    """顶栏走定点更新，不排渲染 —— 硬渲染会重建整棵 DOM，那是
    2026-08-17「设置页中文打不进字」的成因（refreshTide 同款约束）。"""
    js = app_js()
    start = js.index("  refreshSession() {")
    body = js[start:js.index("\n  }", start)]
    assert "setState" not in body
    assert "render" not in body
