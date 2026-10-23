# -*- coding: utf-8 -*-
"""盯盘页快讯滚动条的前端（static/watcher/）。

spec: docs/superpowers/specs/2026-09-05-盘中快讯滚动条-design.md

用户的要求原话是「盘中不想分心太多精力去看这些东西，只要重要的就行」，位置指定在
0DTE Tide 右边那条长横条上。所以这里钉三件事：只显示后端送来的最高档、位置和宽度
不会挤坏既有徽章、以及未翻译的条目要看得出来是未翻译。

断言按本仓库盯盘页测试的惯例读源码（见 test_frontend_kline_gap_fill.py）。
"""
import re
from pathlib import Path

WATCHER = Path(__file__).resolve().parents[1] / "static" / "watcher"


def _app_js() -> str:
    return (WATCHER / "app.js").read_text(encoding="utf-8")


def _index_html() -> str:
    return (WATCHER / "index.html").read_text(encoding="utf-8")


def _css() -> str:
    return (WATCHER / "styles.css").read_text(encoding="utf-8")


def test_news_state_declared():
    js = _app_js()
    assert re.search(r"^\s*newsItems:\s*\[\]", js, re.M), "缺 newsItems 初始状态"


def test_handle_message_routes_news():
    js = _app_js()
    assert "data.type === 'news'" in js, "handleMessage 没有 news 分支"
    assert re.search(r"handleNews\s*\(", js), "缺 handleNews 处理函数"


def _handle_news_body() -> str:
    m = re.search(r"\n        handleNews\(data\) \{(.*?)\n        \},", _app_js(), re.S)
    assert m, "找不到 handleNews() —— 函数名或缩进变了，断言会静默落空"
    return m.group(1)


def test_news_plays_alert_sound():
    """用户选了「加声音，跟信号提醒一样」，声音要走既有开关，不另起一套。"""
    body = _handle_news_body()
    assert "soundEnabled" in body, "没有尊重声音总开关"
    assert "playAlert" in body, "没有复用既有的提醒声"


def test_news_queue_is_capped_in_frontend():
    """后端只留 20 条，前端也不能无限堆 —— 页面一开一整天会越滚越长。"""
    body = _handle_news_body()
    assert re.search(r"newsItems\.(splice|slice)|length\s*>\s*\d+", body), \
        "前端没有给 newsItems 设上限"


def test_untranslated_items_are_distinguishable():
    """模型挂掉时显示英文原文，必须让用户看出来这条没翻出来。"""
    js, html = _app_js(), _index_html()
    assert "translated" in js or "translated" in html, \
        "前端完全没消费 translated 标记，模型挂掉时用户分不清"


def test_ticker_bar_sits_after_tide_and_flexes():
    """位置：0DTE Tide 右边；宽度自适应，右边以后加任何元素都不会被挤坏。"""
    html = _index_html()
    assert "news-ticker" in html, "index.html 里没有滚动条元素"
    assert html.index("0DTE Tide") < html.index("news-ticker"), \
        "滚动条必须排在 0DTE Tide 之后"
    # 必须在市场状态条内部，不能自己另起一行
    bar_start = html.index("market-status-bar")
    bar_end = html.index("</nav>")
    assert bar_start < html.index("news-ticker") < bar_end, \
        "滚动条跑到市场状态条外面去了"

    css = _css()
    m = re.search(r"\.news-ticker\s*\{([^}]*)\}", css)
    assert m, "styles.css 里没有 .news-ticker"
    assert "flex" in m.group(1), ".news-ticker 要用 flex 占据剩余宽度，不能写死宽度"


def test_direction_does_not_drive_color():
    """方向**不驱动配色**（2026-09-05 用户拍板去掉）。

    实测：把 9/3 当天真实的「美联储沃勒倾向 9 月维持利率不变」判成了 bear，而当天
    股市正因这条消息上涨 —— 模型按「不降息=偏空」的常规逻辑判，不知道当时市场预期
    的是**加息**，所以「不加息」其实是利好。方向判错时整条变红，比不给颜色更误导。

    后端仍然返回 `dir` 字段（改口径不用动服务端），前端只是不拿它上色。看到那个字段
    闲着就想给加回配色的话，先回来读这段。
    """
    css, html = _css(), _index_html()
    assert ".news-bull" not in css and ".news-bear" not in css, \
        "方向配色已废弃：模型判反过，见本用例 docstring"
    assert "news-bull" not in html and "news-bear" not in html, \
        "index.html 还在按 dir 上色"


# ─────────────── 横条上留多久 ───────────────

def test_前端也要剔掉超龄条目():
    """**后端那一半挡不住这里。**

    后端只管新连接首帧的快照，而盯盘页是挂着不刷新的——不在前端也剔一遍的话，
    上午那批会一直在横条上轮播。用户 2026-09-10 报的正是这个。
    """
    js = _app_js()
    assert "_pruneNews" in js, "缺剔除超龄条目的函数"
    assert re.search(r"_pruneNews\(\)\s*\{", js), "_pruneNews 要是个方法"


def test_轮播每一拍都剔一遍():
    """**只在收到新消息时剔等于永远不剔。**

    盘中常常十几分钟没有一条新消息，那期间没有任何东西会触发清理，30 分钟前那条
    就一直轮播下去。
    """
    js = _app_js()
    m = re.search(r"_startNewsRotation\(\)\s*\{.*?\n        \},", js, re.S)
    assert m, "找不到 _startNewsRotation()"
    assert "_pruneNews()" in m.group(0), "轮播定时器每一拍要先剔一遍超龄的"


def test_前后端的展示窗口逐字相等():
    """两边不等的症状都是无声的：前端大了，刷新一下旧消息就没了、不刷新还在；
    前端小了，后端刚推来的条目立刻被自己剔掉，表现成「有新闻却什么都不显示」。"""
    from app.services import news_feed_service as svc

    m = re.search(r"const NEWS_SHOW_MAX_AGE_MS\s*=\s*([0-9*\s]+);", _app_js())
    assert m, "app.js 里找不到 NEWS_SHOW_MAX_AGE_MS"
    assert eval(m.group(1).strip()) == svc.NEWS_SHOW_MAX_AGE_S * 1000, \
        "前端的展示窗口必须跟后端 NEWS_SHOW_MAX_AGE_S 相等"


def test_时间戳解析不了的不剔掉():
    """跟后端 _age_of() 同一个取舍：漏掉一条重要消息，比多挂一条旧的糟得多。"""
    js = _app_js()
    m = re.search(r"_pruneNews\(\)\s*\{.*?\n        \},", js, re.S)
    assert m, "找不到 _pruneNews()"
    assert "isFinite" in m.group(0), \
        "解析不出时间的条目要留下，不能当成超龄剔掉"
