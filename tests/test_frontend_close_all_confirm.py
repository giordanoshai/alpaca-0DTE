# -*- coding: utf-8 -*-
"""「全部持仓全平」：取消快捷键，改成点击 + 二次确认（2026-08-21）。

用户报告：`Alt+⇧F` 与他自己在别处设的快捷键撞了，而且这个动作**一按就把账户
清空**，是全系统后果最重的一个按键。用户的决定是「快捷键取消掉，要全平我就去
点击，然后加一个确认」。

两件事分开看：

1. **取消快捷键** —— 不是改成别的字母，是整条通路删掉。留一个可配置的键，等于
   把「按错一下清空账户」这个风险原样保留，只是换了个位置。单仓全平（`Alt+F`）
   不受影响，那是一次一笔、可承受。

2. **点击后必须确认** —— 唯一入口变成按钮，那这个按钮就得挡一下。确认框里要写
   清楚**几笔**，否则「确定要全平吗」看不出这一下是平 1 笔还是 8 笔。

确认走 `window.confirm`：项目里删除配置那处（`deleteConfig`）已经是这个做法，
不另造一套弹窗。它会阻塞页面，但全平本来就是要停下来想一秒的动作。

反面保证最重要：**点了取消就真的一张都不能平**。所以下面用 node 真跑 `closeAll`，
把 `window.confirm` 分别打桩成「取消」和「确定」，数 `_closeOne` 被调用了几次 ——
只断言源码里有 `confirm` 字样是不够的，位置放错（比如放在 `_closeOne` 之后）
同样能通过字面量断言，但账户已经平掉了。
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


def _close_all_source(js: str) -> str:
    match = re.search(r"\n  closeAll\(\) \{.*?\n  \}\n", js, re.S)
    assert match, "app.js 里找不到 closeAll 方法"
    return match.group(0).strip()


def _run_close_all(*, confirm_answer, positions):
    """把 closeAll 单独拎进 node 跑，返回 {confirmCalls, closeOneCalls, busySet}。"""
    node = shutil.which("node")
    if node is None:
        pytest.skip("未安装 node，跳过 JS 运行时校验（源码契约断言仍生效）")
    script = (
        "const calls = { confirmCalls: [], closeOneCalls: 0, busySet: false };\n"
        "global.window = { confirm: (msg) => { calls.confirmCalls.push(msg); return "
        + ("true" if confirm_answer else "false") + "; } };\n"
        "const app = {\n"
        + _close_all_source(_js()) + ",\n"
        "  state: { orderBusy: false, positions: " + json.dumps(positions) + " },\n"
        "  setState(x) {\n"
        "    const patch = typeof x === 'function' ? x(this.state) : x;\n"
        "    if (patch && patch.orderBusy === true) calls.busySet = true;\n"
        "    this.state = Object.assign({}, this.state, patch);\n"
        "  },\n"
        "  toast() {},\n"
        "  _closeOne(p) { calls.closeOneCalls += 1; return Promise.resolve({ ok: true, p }); },\n"
        "};\n"
        "app.closeAll();\n"
        "setTimeout(() => console.log(JSON.stringify(calls)), 0);\n"
    )
    out = subprocess.run([node, "-e", script], capture_output=True, text=True,
                         encoding="utf-8", timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


TWO = [{"id": 1, "sym": "NVDA", "qty": 2}, {"id": 2, "sym": "TSLA", "qty": 1}]


# ---------------------------------------------------------------- 确认框行为


def test_点取消时一张都不平():
    """本文件最重要的一条：确认必须挡在真正的平仓请求前面。"""
    r = _run_close_all(confirm_answer=False, positions=TWO)
    assert r["closeOneCalls"] == 0, "点了取消却仍然发出了平仓请求"


def test_点取消时不把界面锁成下单中():
    """orderBusy 置了不复位，全套买卖按钮会一直灰着，只能刷新页面才能恢复。"""
    r = _run_close_all(confirm_answer=False, positions=TWO)
    assert r["busySet"] is False


def test_点确定时按持仓笔数逐笔平():
    r = _run_close_all(confirm_answer=True, positions=TWO)
    assert r["closeOneCalls"] == 2


def test_确认文案写明几笔():
    """「确定要全平吗」看不出这一下平的是 1 笔还是 8 笔。"""
    r = _run_close_all(confirm_answer=False, positions=TWO)
    assert r["confirmCalls"], "没有弹确认框"
    assert "2" in r["confirmCalls"][0], r["confirmCalls"][0]


def test_没有持仓时不弹确认():
    """空仓点一下弹个框再点取消，是纯粹的骚扰。"""
    r = _run_close_all(confirm_answer=True, positions=[])
    assert r["confirmCalls"] == []
    assert r["closeOneCalls"] == 0


# ---------------------------------------------------------------- 快捷键已删除


def test_全平快捷键通路已删除():
    """close 动作分支里不许再有 Shift 分流 —— 那就是 Alt+⇧F 这条通路本身。"""
    js = _js()
    match = re.search(r"action === 'close'\) \{(.*?)\} else if", js, re.S)
    assert match, "找不到 close 动作分支"
    assert "shiftKey" not in match.group(1), match.group(1)
    assert "closeAll" not in match.group(1), match.group(1)


def test_按钮上不再显示快捷键():
    """键删了文案还挂着，用户会照着按，按了没反应还以为是坏了。"""
    html = _html()
    assert "{{ closeAllKey }}" not in html
    assert "Alt+⇧" not in html
    assert "{{ closeAll }}" in html, "按钮本身必须留着——现在它是唯一入口"


def test_快捷键绑定已从渲染数据里摘掉():
    js = _js()
    assert "closeAllKey" not in js, "closeAllKey 绑定应整体删除，不是留着不用"


def test_单仓全平不受影响():
    """撞键的是全平，单仓全平（一次一笔）仍然要能按。"""
    js = _js()
    assert "this.closePos(i)" in js
    match = re.search(r"const HOTKEY_DEFAULTS = Object\.freeze\(\{(.*?)\}\)", js, re.S)
    assert match and re.search(r"\bclose\s*:", re.sub(r"//.*", "", match.group(1)))
