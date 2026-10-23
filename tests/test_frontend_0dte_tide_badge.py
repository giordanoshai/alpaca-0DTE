# -*- coding: utf-8 -*-
"""交易页顶栏 0DTE 资金潮徽章（2026-08-20 加）。

钉的不是"长得对不对"（源码文本断言做不到），而是这次设计里两处**想当然就会错**
的接线：徽章在 WS 分发里的位置、以及它绝不能去排渲染。
"""
from pathlib import Path
import re
from frontend_src import app_js

ROOT = Path(__file__).resolve().parents[1]


def _sources():
    return (
        app_js(),
        (ROOT / "static" / "index.html").read_text(encoding="utf-8"),
    )


def _refresh_tide_body(js: str) -> str:
    start = js.index("  refreshTide() {")
    return js[start:js.index("\n  }", start)]


def test_模板id与refreshTide的选择器双向对齐():
    """模板里挂的 id 和 JS 里 querySelector 的必须一一对上。

    这是最容易单边改漏的地方：改了模板 id 而没改选择器，`refreshTide()` 会静静
    地什么都不做 —— 不报错、不打日志，徽章就永远停在首帧的值上。
    """
    js, html = _sources()
    body = _refresh_tide_body(js)
    for elem_id in ("header-tide", "header-tide-value", "header-tide-note"):
        assert f'id="{elem_id}"' in html, f"模板缺 id={elem_id}"
        assert f"'#{elem_id}'" in body, f"refreshTide 没有取 #{elem_id}"


def test_模板用到的tide字段tideView全都给了():
    js, html = _sources()
    start = js.index("  tideView() {")
    view = js[start:js.index("\n  }", start)]
    used = set(re.findall(r"\{\{\s*tide\.(\w+)\s*\}\}", html))
    assert used, "模板里没有任何 {{ tide.* }} 绑定"
    for field in used:
        assert re.search(rf"\b{field}\b", view), f"模板用了 tide.{field}，但 tideView() 没给"


def test_tide分支在hasFeed提前return之上():
    """**这是 walls 踩过的坑**（2026-08-15「只带 walls 的帧补一次渲染」那次）。

    `!hasFeed` 那条 return 之下的代码，在盘前/收盘后 feed 静默时根本执行不到。
    资金潮每分钟才变一次、且盘前握着昨收值，放到 return 下面等于整晚不更新。
    """
    js, _ = _sources()
    tide_at = js.index("if (hasOwn(msg, 'tide')) {")
    return_at = js.index("if (!hasFeed || !app.acceptSectionRevision('feed', revisions.feed)) {")
    assert tide_at < return_at, "tide 分支掉到 !hasFeed 的提前 return 下面去了"


def test_refreshTide不排渲染():
    """顶栏不在任何 data-render-region 内，软渲染够不着；而硬渲染会重建整棵 DOM
    —— 那正是 2026-08-17「设置页中文输入法打不进字」的成因。徽章每分钟变一次，
    绝不能为它触发整页重建。
    """
    js, _ = _sources()
    body = _refresh_tide_body(js)
    assert "scheduleRender" not in body
    assert "renderApp" not in body

    # WS 分发里的 tide 分支同样不许排渲染（walls 那条是要排的，别照抄过来）
    start = js.index("if (hasOwn(msg, 'tide')) {")
    assert "scheduleRender" not in js[start:start + 260]


def test_没数据时显示破折号而不是持平0():
    """`seeded=false` 时后端给的 label 是「持平 $0.0M」，那和「盘中真的多空平衡」
    在徽章上完全分不清。必须显式降级成 '--'。
    """
    js, _ = _sources()
    start = js.index("  tideView() {")
    view = js[start:js.index("\n  }", start)]
    assert "!t.seeded" in view
    assert "t.diff == null" in view
    assert "text: '--'" in view


def test_非实时场次带标注且置灰():
    """盘前握着的是上一交易日终值，不标注的话和盘中实时值长得一模一样。"""
    js, _ = _sources()
    start = js.index("  tideView() {")
    view = js[start:js.index("\n  }", start)]
    assert "'prev'" in view and "昨收" in view
    assert "'closed'" in view and "已收盘" in view
    assert "opacity" in view


def test_徽章紧跟在PAPER之后():
    """用户要的位置：PAPER 右边。"""
    _, html = _sources()
    paper_at = html.index("{{ mode.label }}")
    tide_at = html.index('id="header-tide"')
    right_group_at = html.index("margin-left:auto")
    assert paper_at < tide_at < right_group_at
