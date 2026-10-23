# -*- coding: utf-8 -*-
"""盯盘页手动加票的界面反馈（static/watcher/app.js + index.html）。

修之前 `addTicker()` 只有成功分支：接口返回 400/500 时走 `catch` 或 `data.success`
为假，两条路都只在浏览器控制台打一行 `console.error`，**界面上什么都不显示** ——
用户看到的就是"点了没反应"。加票要同步等后端补完 K 线（单只票几秒），期间也没有
任何在忙的迹象，容易被连点几次、同时开几个预热。

本文件按本仓库既有前端测试的惯例做源码断言（见 test_frontend_kline_gap_fill.py）。
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WATCHER = ROOT / "static" / "watcher"


def _app_js() -> str:
    return (WATCHER / "app.js").read_text(encoding="utf-8")


def _index_html() -> str:
    return (WATCHER / "index.html").read_text(encoding="utf-8")


def _add_ticker_body() -> str:
    """抠出 addTicker() 的函数体。缩进是刻意带上的，换了缩进这里会红而不是静默失效。"""
    m = re.search(r"\n        async addTicker\(\) \{(.*?)\n        \},", _app_js(), re.S)
    assert m, "app.js 里找不到 addTicker() —— 函数名或缩进变了，断言会静默落空"
    return m.group(1)


def test_add_ticker_state_fields_declared():
    """出错文案与"正在补 K 线"两个状态得在组件初始状态里声明，否则 Alpine 不会跟踪。"""
    js = _app_js()
    assert re.search(r"^\s*addTickerError:\s*''", js, re.M), "缺 addTickerError 初始值"
    assert re.search(r"^\s*addingTicker:\s*false", js, re.M), "缺 addingTicker 初始值"


def test_add_ticker_surfaces_backend_error():
    """后端那句具体原因（K 线补不齐 / 代码不合法）必须显示到界面上，不能只进控制台。"""
    body = _add_ticker_body()
    assert "data.error" in body, "得用后端返回的具体原因，不能只显示一句笼统的失败"
    # 两条失败路都要摆到界面上：接口回了 error（success 为假）、以及网络异常走 catch。
    assert re.search(r"\}\s*else\s*\{.*?AddTickerError", body, re.S), \
        "success 为假时没有把原因摆到界面上"
    assert re.search(r"catch\s*\([^)]*\)\s*\{.*?AddTickerError", body, re.S), \
        "catch 分支只打了控制台，界面上仍然没反应"


def test_add_ticker_marks_busy_while_warming_bars():
    """点下去到 K 线补完有几秒，期间要置忙并在结束时复位，防连点开多个预热。"""
    body = _add_ticker_body()
    assert "this.addingTicker = true" in body, "开始加票时没有置忙"
    assert "finally" in body, "得用 finally 复位，异常路径上也不能卡在忙状态"
    assert "this.addingTicker = false" in body, "没有复位 addingTicker"


def test_add_ticker_guards_against_double_submit():
    """置忙之外还要在入口挡一道：忙的时候再点直接返回。"""
    body = _add_ticker_body()
    assert re.search(r"if\s*\(.*this\.addingTicker.*\)\s*return", body), \
        "addTicker 入口没有挡住重复提交"


def test_index_html_shows_error_and_busy_state():
    """界面上要有错误提示元素，输入框与按钮在补 K 线期间禁用。"""
    html = _index_html()
    assert 'x-text="addTickerError"' in html, "没有显示 addTickerError 的元素"
    assert 'x-show="addTickerError"' in html, "错误提示得在有错时才出现"
    assert html.count(':disabled="addingTicker"') >= 2, "输入框和按钮都要在忙时禁用"
