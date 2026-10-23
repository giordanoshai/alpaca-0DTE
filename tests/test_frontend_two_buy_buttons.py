# -*- coding: utf-8 -*-
"""买入面板必须是 CALL / PUT 两个按钮，各自一个快捷键（2026-09-01）。

**为什么是两个**：2026-08-21 到 2026-09-01 之间这里只有一个按钮，理由是规则 6
方向闸保证「CALL/PUT 最多一边能过」，方向由后端给、用户不用自己选边。那条闸
（连同震荡闸、追高闸、动能闸）2026-09-01 删除后前提没了 —— 系统不再判方向，
买哪一边是用户的决定，前端**不许**再替他选一个。

本文件钉住三件事：
  1. 两个方向各有自己的按钮与快捷键，且两个键不重号
  2. index.html 真的把两个按钮都接上了（写了函数没接上等于没做）
  3. `buySide()` 那个「替用户选边」的旧函数确实没了

守卫的镜像面在后端：tests/test_validator_market_only.py::test_闸门与方向无关_两侧结论恒等。
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from frontend_src import app_js

ROOT = Path(__file__).resolve().parents[1]


def _js() -> str:
    return app_js()


def _html() -> str:
    return (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def _run_node(snippet: str):
    node = shutil.which("node")
    if node is None:
        pytest.skip("未安装 node，跳过 JS 运行时校验")
    out = subprocess.run([node, "-e", snippet], capture_output=True, text=True,
                         encoding="utf-8", timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def _const(name: str) -> str:
    """把一个模块级 `const X = ...;` 整段抠出来交给 node 真跑。

    结尾按「行首就收口」找：`]);` / `});` / `);` 三种都吃得下。
    """
    match = re.search(r"\nconst " + name + r" = .*?\n[\]\}]?\)\;\n", _js(), re.S)
    assert match, f"static/app.js 里找不到 {name}"
    return match.group(0)


def _js_code_only() -> str:
    """去掉 // 行注释 —— 注释里提到已删函数的名字不算「还在用」。"""
    return re.sub(r"^\s*//.*$", "", _js(), flags=re.M)


def test_两个方向各一个按钮定义():
    sides = _run_node(_const("BUY_SIDES") + "console.log(JSON.stringify(BUY_SIDES));")

    assert [s["dir"] for s in sides] == ["long", "short"]
    assert [s["label"] for s in sides] == ["买入 CALL", "买入 PUT"]
    assert [s["hotkey"] for s in sides] == ["buy", "buyPut"]


def test_两个快捷键都有默认值且不重号():
    defaults = _run_node(
        _const("HOTKEY_DEFAULTS") + "console.log(JSON.stringify(HOTKEY_DEFAULTS));")

    assert defaults["buy"] == "B"          # 沿用旧键，肌肉记忆不变
    assert defaults["buyPut"] == "N"
    letters = list(defaults.values())
    assert len(letters) == len(set(letters)), f"快捷键重号：{defaults}"


def test_两个按钮都接进了页面():
    html = _html()
    assert "{{ order.onBuyCall }}" in html, "CALL 按钮没接上 onBuyCall"
    assert "{{ order.onBuyPut }}" in html, "PUT 按钮没接上 onBuyPut"
    # 各自显示自己那一侧的金额 —— 两边合约不同价，共用一个数字会误导。
    assert "{{ order.callAmount }}" in html
    assert "{{ order.putAmount }}" in html


def test_按钮亮灭各认各的方向():
    """CALL 按钮认 g.call、PUT 按钮认 g.put，都是后端对该方向干跑的结果。"""
    js = _js()
    assert re.search(r"mkBuyBtn\(g\.call,\s*callSide\.label", js), \
        "CALL 按钮没有按 g.call 判亮灭"
    assert re.search(r"mkBuyBtn\(g\.put,\s*putSide\.label", js), \
        "PUT 按钮没有按 g.put 判亮灭"


def test_替用户选边的旧函数已删除():
    """`buySide()` 是「一个按钮时代」的产物：它在两边都能买时挑一个方向下单。

    留着它就会有人接回去，用户又变成「系统替我选」。注释里提到这个名字没关系，
    这里只禁函数定义与调用。
    """
    code = _js_code_only()
    assert "const buySide = " not in code
    assert "buySide(" not in code
