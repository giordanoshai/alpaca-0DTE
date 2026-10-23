# -*- coding: utf-8 -*-
"""下单自查弹窗 —— 只有「打架」硬拦，另外两项只记录（2026-09-02）。

**这是什么**：2026-09-01 删掉震荡 / 均线共振 / MACD 动能 / 追高四条自动闸之后，那
三个判断并没有消失，只是不再由机器回答。买入前弹一个窗把它们交还给人：三个开关
默认全停在「是」（有风险），人自己逐条拨。

**2026-09-02 改掉了放行规则**：原来三项都拨到「否」提交按钮才亮，现在只有第 2 条
「打架」必须答否，震荡和追高拨到哪边都能提交、只记录。

改这条的理由**不是**「要求放松了」，而是原来那样**采不到对照样本**：三项全否才
能提交的话，「有一项是红的」那一组在数据库里永远是 0 笔，而用户想问的正是「三项
全绿到底比有一项红赚不赚钱」—— 要比，红的那一组就必须也能下单。留下「打架」这
一道是用户决定：多周期共振是这套打法的核心参数，1min / 5min 的 MACD 与 EMA 互相
打架时不许开仓。

本文件钉住七件事：
  1. 顺序是漏斗 —— 震荡（该不该做）→ 打架（做哪边）→ 追高（现在还是等回踩）
  2. 默认全在「有风险」那一侧（默认「否」会让弹窗退化成「再点一下确认」）
  3. 只有**带 blocking 标记**的那条要求显式 false；缺键仍然不算答过
  4. 提示行是静态文字，不含任何实时数字（那些数的计算代码 2026-09-01 删了）
  5. 只有买入路径会弹 —— 平仓 / 止损 / 加仓不弹
  6. 后端**只记录不强制**，答案落进 ttt_trades.template_snapshot 那个 jsonb 列
  7. 判据文字是红的，硬拦那条另有标记 —— 三条一样红等于没有重点
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from app import exec_entry
from app.routers.orders import OrderRequest
from frontend_src import app_js

ROOT = Path(__file__).resolve().parents[1]


# ── 抠 JS 声明交给 node 真跑 ──────────────────────────────────────────────────
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
    """抠出 `const NAME = ...;`：按括号配平找结尾，单行多行都吃得下。"""
    js = _js()
    marker = f"\nconst {name} = "
    assert marker in js, f"static/app.js 里找不到 const {name}"
    start = js.index(marker) + 1
    depth, i = 0, start + len(marker) - 1
    while i < len(js):
        char = js[i]
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        elif char == ";" and depth == 0:
            return js[start:i + 1]
        i += 1
    raise AssertionError(f"const {name} 没有找到结尾分号")


def _func(name: str) -> str:
    """抠出 `function NAME(...) {...}`：按花括号配平找结尾。"""
    js = _js()
    marker = f"\nfunction {name}("
    assert marker in js, f"static/app.js 里找不到 function {name}"
    start = js.index(marker) + 1
    depth, i = 0, js.index("{", start)
    while i < len(js):
        if js[i] == "{":
            depth += 1
        elif js[i] == "}":
            depth -= 1
            if depth == 0:
                return js[start:i + 1]
        i += 1
    raise AssertionError(f"function {name} 没有找到结尾花括号")


def _js_code_only() -> str:
    """去掉 // 行注释 —— 注释里提到某个名字不算「真的在用它」。"""
    return re.sub(r"^\s*//.*$", "", _js(), flags=re.M)


# ── 1. 顺序 ──────────────────────────────────────────────────────────────────
def test_顺序是漏斗_震荡第一追高最后():
    """该不该做 → 做哪一边 → 现在做还是等回踩。

    震荡排第一：它答「是」就意味着整笔不做，后两个问了也白问（0DTE 在横盘里买
    CALL 买 PUT 都在亏时间价值）。追高排最后：答错只是进晚了、方向还在。
    """
    items = _run_node(_const("SELF_CHECK_ITEMS")
                      + "console.log(JSON.stringify(SELF_CHECK_ITEMS));")

    assert [it["key"] for it in items] == ["chop", "conflict", "chase"]
    assert [it["q"] for it in items] == ["震荡吗？", "打架吗？", "追高吗？"]


# ── 2. 默认位置 ──────────────────────────────────────────────────────────────
def test_三个开关默认全在有风险那一侧():
    """默认「否」的话弹窗会退化成「再点一下确认」，两周内就变成闭眼点。

    懒的路径必须是「提交不了」，不是「提交了」。
    """
    default = _run_node(_const("SELF_CHECK_DEFAULT")
                        + "console.log(JSON.stringify(SELF_CHECK_DEFAULT));")

    assert default == {"chop": True, "conflict": True, "chase": True}


def test_每次开仓都从头拨_不记上一次():
    """openSelfCheck 必须拿 SELF_CHECK_DEFAULT 起手，不许从上一次的答案接着来。"""
    code = _js_code_only()
    assert "answers: { ...SELF_CHECK_DEFAULT }" in code


# ── 3. 通过判据 ──────────────────────────────────────────────────────────────
def test_硬拦的只有打架一条():
    """放行规则和弹窗上的红色标记必须同源 —— 都从 SELF_CHECK_ITEMS 的 blocking 来。

    分成两处写的话，早晚有一天改了放行规则忘了改标记，弹窗上标着「必须答否」的
    那条其实拦不住人，比不标还糟。
    """
    items = _run_node(_const("SELF_CHECK_ITEMS")
                      + "console.log(JSON.stringify(SELF_CHECK_ITEMS));")

    assert [it["key"] for it in items if it.get("blocking")] == ["conflict"]


def test_震荡和追高只记录_打架才拦():
    """缺键 / undefined 一律不算答过 —— 少问一个不等于答了「否」。

    震荡、追高拨在哪边都放行：不放行就永远采不到「红着也做了」那一组样本，
    用户想做的对照（三项全绿 vs 有一项红，哪个赚钱）就无从做起。
    """
    snippet = _const("SELF_CHECK_ITEMS") + "\n" + _func("selfCheckPassed") + """
const cases = [
  [{ chop: false, conflict: false, chase: false }, true],
  [{ chop: true,  conflict: false, chase: false }, true],   // 震荡答「是」：只记录，放行
  [{ chop: false, conflict: false, chase: true  }, true],   // 追高答「是」：只记录，放行
  [{ chop: true,  conflict: false, chase: true  }, true],   // 两条都红也放行
  [{ chop: false, conflict: true,  chase: false }, false],  // 打架答「是」：硬拦
  [{ chop: true,  conflict: true,  chase: true  }, false],
  [{ chop: false, chase: false }, false],                   // conflict 缺键：不算答过
  [{ chop: false, conflict: undefined, chase: false }, false],
  [{}, false],
  [null, false],
];
console.log(JSON.stringify(cases.map(([a, want]) => selfCheckPassed(a) === want)));
"""
    assert all(_run_node(snippet))


# ── 4. 提示行是静态文字 ──────────────────────────────────────────────────────
def test_提示行不含实时数字():
    """|4in1| 分数、效率比、5min MACD 柱、EMA 方向的计算代码 2026-09-01 一并删了。

    硬凑几个数字上去只会让人以为系统还在替他判断 —— 这里要的恰恰相反。
    """
    items = _run_node(_const("SELF_CHECK_ITEMS")
                      + "console.log(JSON.stringify(SELF_CHECK_ITEMS));")

    for it in items:
        assert "{" not in it["hint"], f"提示行混进了模板占位：{it['hint']}"
        assert not re.search(r"\d+\.\d", it["hint"]), f"提示行混进了阈值数字：{it['hint']}"


# ── 5. 接线 & 只拦开仓 ───────────────────────────────────────────────────────
def test_弹窗接进了页面():
    html = _html()
    assert '<sc-if value="{{ selfCheck.open }}"' in html, "弹窗没接上开关"
    assert '<sc-for list="{{ selfCheck.items }}"' in html, "三个问题没接上"
    assert "{{ c.onToggle }}" in html, "开关点不动"
    assert "{{ selfCheck.onOk }}" in html and "{{ selfCheck.onCancel }}" in html


def test_判据文字是红的_硬拦那条另有标记():
    """用户 2026-09-02 明确要求提示文字改成显眼的红色。

    但三条一样红等于没有重点 —— 唯一能拦住人的是「打架」那条，它得再抢眼一档，
    所以额外给它一根红竖条和一个「必须答否」标签。标签文字由 blocking 决定而不是
    硬编码 key，这样放行规则一改，标记跟着变，不会出现「标着必须答否其实拦不住」。
    """
    html = _html()
    assert "#f87171" in html, "判据文字不再是红色"
    assert "{{ c.barColor }}" in html, "硬拦那条的红竖条没接上"
    assert "{{ c.mustText }}" in html, "「必须答否」标签没接上"

    code = _js_code_only()
    assert "blocking ? '必须答否' : ''" in code, "标签文字必须由 blocking 决定，不许硬编码 key"


def test_买入一律先过自查():
    """卡放在 doOrder 入口而不是按钮上：将来新增的调用方也自动落在卡后面。"""
    code = _js_code_only()
    assert "async doOrder(dir, selfCheck) {" in code
    assert "if (!selfCheck) { this.openSelfCheck(dir); return; }" in code
    assert "self_check: selfCheck," in code, "自查答案没发给后端"


def test_平仓加仓不弹自查():
    """想跑的时候被弹窗挡一下是要命的 —— openSelfCheck 只许 doOrder 一个调用方。"""
    code = _js_code_only()
    assert len(re.findall(r"this\.openSelfCheck\(", code)) == 1


# ── 6. 后端：只记录不强制 ────────────────────────────────────────────────────
def test_请求可以带自查答案():
    req = OrderRequest(ticker="QQQ", direction="long", qty=1,
                       self_check={"chop": False, "conflict": False, "chase": False,
                                   "dwell_ms": 3200, "flips": 4})
    assert req.self_check.dwell_ms == 3200 and req.self_check.flips == 4


def test_不带自查答案仍然可以下单():
    """脚本、/api/execute 同步端点、旧前端都不带这个字段，不能因此 422。"""
    assert OrderRequest(ticker="QQQ", direction="long").self_check is None


def test_后端不强制_三个都有风险照样受理():
    """堵死不如留档：堵死了人只会绕过前端直接开券商 App 下单，那才什么都记不到。"""
    req = OrderRequest(ticker="QQQ", direction="long",
                       self_check={"chop": True, "conflict": True, "chase": True})
    assert req.self_check.chop is True


def test_自查字段拼错会被拒():
    with pytest.raises(ValidationError):
        OrderRequest(ticker="QQQ", direction="long",
                     self_check={"chop": False, "conflict": False, "chase": False, "typo": 1})


def _snapshot_with(monkeypatch, template, **kw):
    monkeypatch.setattr(exec_entry.trade_recorder, "template_snapshot", lambda: template)
    return exec_entry._entry_snapshot(
        OrderRequest(ticker="QQQ", direction="long", **kw))


def test_落库快照带上自查答案(monkeypatch):
    """塞进 ttt_trades.template_snapshot 这个已有的 jsonb 列，不新开表也不加列。"""
    answers = {"chop": False, "conflict": False, "chase": False,
               "dwell_ms": 5100, "flips": 3}
    snap = _snapshot_with(monkeypatch, {"name": "默认", "params": {}}, self_check=answers)

    assert snap["name"] == "默认"
    assert snap["self_check"] == answers


def test_没带自查时不伪造一条(monkeypatch):
    """缺失就是缺失，不能写一条「三个都答否」的假记录进去。"""
    snap = _snapshot_with(monkeypatch, {"name": "默认", "params": {}})
    assert "self_check" not in snap


def test_模板快照取不到也不丢自查(monkeypatch):
    """template_snapshot() 取数失败返回 None（不阻塞调用方），自查答案仍要落下去。"""
    answers = {"chop": False, "conflict": False, "chase": False,
               "dwell_ms": 900, "flips": 3}
    snap = _snapshot_with(monkeypatch, None, self_check=answers)

    assert snap == {"self_check": answers}
