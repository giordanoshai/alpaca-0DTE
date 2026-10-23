"""前端买入权限必须与后端双向闸门同源（趋势分降级第二期）。

规则 6 反向硬拦截删除后，后端对 CALL/PUT 分别判可否。前端若还留着
「一边 true 另一边必 false」的旧结构，用户就永远只看得到一个方向的按钮 ——
后端放宽了、界面没放宽，第一期等于白做。

同源要求是双向的：前端也不得比后端更宽。买入按钮的可用性只能由
`t.gates[方向].blocked` 决定，不能自己按趋势分方向再猜一遍。
"""
import re
from pathlib import Path
from frontend_src import app_js

ROOT = Path(__file__).resolve().parents[1]


def _js() -> str:
    return app_js()


def _html() -> str:
    return (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def _gate_method(js: str) -> str:
    match = re.search(r"\n  gate\(\) \{(.*?)\n  \}\n", js, re.S)
    assert match, "static/app.js 里找不到 gate() 方法"
    return match.group(1)


def test_gate_no_longer_mutually_exclusive():
    """gate() 不得再出现「一边 true 另一边必 false」的硬编码互斥。"""
    body = _gate_method(_js())

    assert "call: true, put: false" not in body
    assert "call: false, put: true" not in body


def test_gate_reads_both_sides_from_backend():
    """两侧可否必须各读各的后端闸门结果。"""
    body = _gate_method(_js())

    assert "gates.long" in body
    assert "gates.short" in body


def test_reverse_direction_wording_removed():
    """规则 6 已删，前端不该再说「反向做多/做空已拦截」。"""
    js = _js()

    assert "反向做空已拦截" not in js
    assert "反向做多已拦截" not in js


def test_light_has_both_state():
    """两侧都可买是常态（方向无关的规则占大多数），红绿灯必须表达得出来。"""
    js = _js()

    assert "'both'" in js
    assert "可买入" in js


def test_tick_stores_both_gates():
    js = _js()

    assert "t.gates = tk.gates" in js


# 2026-08-21：原有的 test_card_sorting_counts_any_tradable_side 已删除。
# 它守的是「排序的可交易判据要认 both，否则 both 会被排到最后」，而卡片排序当天
# 被整体删掉（用户要求，见 tests/test_frontend_no_card_sorting.py），没有排序就
# 没有「排到最后」这回事。`st` 的 both 态仍受 test_light_has_both_state 保护。


def test_order_entry_renders_both_contracts():
    """订单区必须并列 CALL 与 PUT 两套**合约明细**。

    2026-08-21 起买入按钮合并成一个（规则 6 方向闸决定买哪边，见
    test_frontend_single_buy_button.py），但合约明细仍是两行 —— 它是**信息**
    不是**动作**：另一边什么价位要看得见，才能一眼核对闸门选的方向对不对。
    """
    html = _html()

    for key in ("order.callContract", "order.putContract",
                "order.callMid", "order.putMid"):
        assert "{{ " + key + " }}" in html, f"index.html 缺少绑定 {key}"


def test_every_order_binding_is_provided_by_app_js():
    """模板里每个 {{ order.X }} 都必须在 app.js 的 order 对象里有对应键。

    绑定名打错不会报错，只会静默渲染成空白 —— 订单区正好是「合约代码、买入按钮」
    这些绝不能悄悄变空的地方。双边改造一次性新增了 20+ 个绑定，靠肉眼核对不可靠。
    """
    used = set(re.findall(r"\{\{\s*order\.(\w+)\s*\}\}", _html()))
    match = re.search(r"const order = needOrderEntry \? \{(.*?)\n    \} : \{\};", _js(), re.S)
    assert match, "app.js 里找不到 order 绑定对象"
    provided = set(re.findall(r"(?:^|[\s{,])(\w+)\s*:", match.group(1), re.M))

    assert used - provided == set()


def test_previews_are_read_per_side():
    """合约预览按 sides 拆开写入，不再是每票一份。"""
    js = _js()

    assert "preview.sides" in js
    assert "t.cLong" in js and "t.cShort" in js
