# -*- coding: utf-8 -*-
"""盯盘页（static/watcher/）顶栏的 0DTE Tide 徽章。

**这个徽章此前一直是死的**（2026-09-05 发现）：`marketInfo` 数组只有 4 个元素
（SPY / QQQ / ^VIX / 0DTE Tide，下标 0~3），而模板和 app.js 读的是 `marketInfo[4]`
——恒为 `undefined`。后果不只是配色不生效，`x-text="marketInfo[4]?.text ?? '--'"`
让**数值本身一直显示 `--`**：后端在推、`handleMarketTide()` 也确实按 label 找到对象
写了值，只有显示这一环读错了位置。

注意 `tests/test_frontend_0dte_tide_badge.py` 测的是**交易终端**那个同名徽章
（`static/index.html` + `static/js/`，走 `{{ tide.* }}` 绑定，是好的）。两个前端各有
一个 0DTE Tide 徽章，那份测试覆盖不到这里，所以这个 bug 一直没被抓住。
"""
import re
from pathlib import Path

WATCHER = Path(__file__).resolve().parents[1] / "static" / "watcher"


def _app_js() -> str:
    return (WATCHER / "app.js").read_text(encoding="utf-8")


def _index_html() -> str:
    return (WATCHER / "index.html").read_text(encoding="utf-8")


def test_no_hardcoded_index_into_market_info():
    """禁止按下标取 marketInfo 的成员 —— 数组一动就静默错位，而且不报错。

    `marketInfo.slice(0, 3)`（渲染前三个行情项）是允许的，那是切片不是取成员。
    """
    bad = []
    for name, src in (("app.js", _app_js()), ("index.html", _index_html())):
        for lineno, line in enumerate(src.splitlines(), 1):
            # 跳过注释：讲这个 bug 的说明文字里就写着 marketInfo[4]
            stripped = line.strip()
            if stripped.startswith(("//", "*", "<!--")):
                continue
            if re.search(r"marketInfo\[\s*\d+\s*\]", line):
                bad.append(f"{name}:{lineno} {stripped[:70]}")
    assert not bad, f"改用 tideInfo() 按 label 取，别按下标：{bad}"


def test_tide_accessor_exists_and_matches_by_label():
    js = _app_js()
    m = re.search(r"\n        tideInfo\(\)\s*\{(.*?)\n        \},", js, re.S)
    assert m, "缺 tideInfo() 访问器"
    body = m.group(1)
    assert "'0DTE Tide'" in body or '"0DTE Tide"' in body, "tideInfo() 应按 label 匹配"
    assert "find" in body, "按 label 查找才不怕数组顺序变动"


def test_badge_reads_through_the_accessor():
    """模板里徽章的三处绑定都要走访问器。"""
    html = _index_html()
    assert "tideInfo()?.text" in html or "tideInfo()?.text ?? '--'" in html, \
        "徽章数值没走访问器 —— 这正是它一直显示 `--` 的原因"
    assert html.count("tideInfo()?.direction") >= 2, "配色绑定没走访问器"


def test_session_helpers_use_accessor():
    """tideStale()/tideSessionNote() 这两个也读的是同一个对象。"""
    js = _app_js()
    for fn in ("tideStale", "tideSessionNote"):
        m = re.search(rf"\n        {fn}\(\)\s*\{{(.*?)\n        \}},", js, re.S)
        assert m, f"找不到 {fn}()"
        assert "tideInfo()" in m.group(1), f"{fn}() 仍在按下标取 marketInfo"
