# -*- coding: utf-8 -*-
"""GEX 破墙提醒：价格涨破 call wall / 跌破 put wall 时弹横幅 + 提示音（2026-08-21）。

用户要求：「那个 -GEX 然后涨破或者跌破 GEX WALL，我们是不是可以做个提醒」。
确认后定的口径是**不分正负 GEX 都响、全池 30 多只都响、toast + 提示音**。
±GEX 曾写进文案当「这次破墙可信几分」的参考，**2026-08-21 当天删除** —— 那个
标签是拿现价跟 gamma_flip 比大小推的，实测与事实接近反相关，详见
tests/test_frontend_no_derived_net_gex.py。触发判据从来不含 ±GEX，所以不受影响。

**零后端改动**：墙位本来就在推（`gex_wall_service` 30s 一轮），实时价本来就在推
（feed 每 tick），判断全在前端已有的数据里做，不新增任何 UW 请求。

**这是提醒不是闸门**：不拦单、不改单、不进 validator。`gex_wall_service.py` 文件头
第 2 条解耦约束（不进任何交易判定）仍然成立——规则 7b 距墙闸门进了闸门却从没真
拦过单、最后留下一地死代码，那个教训针对的是闸门，不针对提醒。

**四种情况绝不响**（每一条都对应一种假警报）：
  1. 墙位 stale（这轮没拉到，显示的是上一轮的旧值）——拿旧墙位报警就是假警。
  2. 墙位数字本身变了——call_wall 从 605 跳到 598 而价格没动，那是数据更新不是突破。
  3. 冷却期内——每票每道墙 5 分钟只响一次。
  4. 第一次拿到这只票的墙位——只记基准。否则开盘瞬间 30 多只票一起误报。

提示音复用项目既有的音效系统（`static/sounds/` + `initSfx`/`playSfx`），
因此自动受设置页「提示音」总开关门控；另有一个独立开关可单独关掉破墙提醒。

缓冲带 0.1% 是**迟滞带**：破要超过 +0.1% 才算破，复位要退回 -0.1% 才算回来。
中间那段保持上一次的判定，价格贴着墙抖不会来回触发。
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


def _step_source(js: str) -> str:
    """把纯函数连同它依赖的两个常量一起抠出来，好在 node 里直接跑。"""
    parts = []
    for pattern in (r"const GEX_BREACH_BUFFER = [^;]+;",
                    r"const GEX_BREACH_COOLDOWN_MS = [^;]+;",
                    r"const gexBreachStep = \(prev, cur, nowMs\) => \{.*?\n\};"):
        match = re.search(pattern, js, re.S)
        assert match, f"app.js 里找不到 {pattern}"
        parts.append(match.group(0))
    return "\n".join(parts)


def _run(steps):
    """steps: [[cur, nowMs], ...] —— 串行喂进去，返回每步的 {breaches, next}。"""
    node = shutil.which("node")
    if node is None:
        pytest.skip("未安装 node，跳过 JS 运行时校验（源码契约断言仍生效）")
    script = (
        _step_source(_js())
        + "\nlet prev = null; const out = [];"
        + "\nfor (const [cur, now] of " + json.dumps(steps) + ") {"
        + "\n  const r = gexBreachStep(prev, cur, now);"
        + "\n  prev = r.next; out.push(r);"
        + "\n}"
        + "\nconsole.log(JSON.stringify(out));"
    )
    out = subprocess.run([node, "-e", script], capture_output=True, text=True,
                         encoding="utf-8", timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def _wall(px, *, call=600.0, put=580.0, stale=False):
    return {"px": px, "callWall": call, "putWall": put, "stale": stale}


MIN = 60_000


# ---------------------------------------------------------------- 会响


def test_涨破call_wall():
    res = _run([[_wall(595.0), 0], [_wall(601.0), 1000]])
    assert res[0]["breaches"] == []
    assert res[1]["breaches"] == ["call"]


def test_跌破put_wall():
    res = _run([[_wall(585.0), 0], [_wall(578.0), 1000]])
    assert res[1]["breaches"] == ["put"]


# ---------------------------------------------------------------- 不响


def test_首次观测只记基准不响():
    """开盘第一帧 30 多只票状态从「未知」变成「在墙上/下」，一条都不许响。"""
    res = _run([[_wall(650.0), 0]])
    assert res[0]["breaches"] == []
    assert res[0]["next"]["callSide"] == "above"


def test_缓冲带内不响():
    """600 → 600.3 是 +0.05%，没超过 0.1%，不算破。"""
    res = _run([[_wall(595.0), 0], [_wall(600.3), 1000]])
    assert res[1]["breaches"] == []
    assert res[1]["next"]["callSide"] == "below", "缓冲带内应保持上一次判定"


def test_连续在墙上只响一次():
    res = _run([[_wall(595.0), 0], [_wall(601.0), 1000], [_wall(605.0), 2000]])
    assert res[1]["breaches"] == ["call"]
    assert res[2]["breaches"] == []


def test_墙位自己变了不响():
    """价格没动、墙从 600 掉到 590，价格「突然在墙上面」——这是数据更新不是突破。"""
    res = _run([
        [_wall(595.0, call=600.0), 0],
        [_wall(595.0, call=590.0), 1000],
    ])
    assert res[1]["breaches"] == []
    assert res[1]["next"]["callSide"] == "above", "应按新墙位重新记基准"


def test_stale时不响():
    res = _run([[_wall(595.0), 0], [_wall(601.0, stale=True), 1000]])
    assert res[1]["breaches"] == []


def test_stale之后重新记基准而不是补响():
    """stale 那一帧把基准清掉，恢复后第一帧只记基准——不能把 stale 期间的移动补报出来。"""
    res = _run([
        [_wall(595.0), 0],
        [_wall(601.0, stale=True), 1000],
        [_wall(602.0), 2000],
    ])
    assert res[2]["breaches"] == []


def test_价格缺失时不响():
    res = _run([[_wall(595.0), 0], [_wall(None), 1000]])
    assert res[1]["breaches"] == []


def test_墙位缺失时不响():
    res = _run([[_wall(595.0), 0], [{"px": 601.0, "callWall": None,
                                     "putWall": 580.0, "stale": False}, 1000]])
    assert res[1]["breaches"] == []


# ---------------------------------------------------------------- 冷却与迟滞


def test_五分钟内不重复响():
    res = _run([
        [_wall(595.0), 0],
        [_wall(601.0), 1000],          # 响
        [_wall(595.0), 2 * MIN],       # 退回墙下
        [_wall(601.0), 3 * MIN],       # 3 分钟内再破 —— 冷却中
    ])
    assert res[1]["breaches"] == ["call"]
    assert res[3]["breaches"] == []


def test_冷却过后可以再响():
    res = _run([
        [_wall(595.0), 0],
        [_wall(601.0), 1000],
        [_wall(595.0), 2 * MIN],
        [_wall(601.0), 6 * MIN],
    ])
    assert res[3]["breaches"] == ["call"]


def test_复位也要退出缓冲带():
    """破墙后跌回 599.9（只低了 0.017%）不算回到墙下，否则贴着墙抖会来回触发。"""
    res = _run([
        [_wall(595.0), 0],
        [_wall(601.0), 1000],
        [_wall(599.9), 2000],
    ])
    assert res[2]["next"]["callSide"] == "above"


# ---------------------------------------------------------------- 接线与形式


def test_文案带出方向与墙位价():
    js = _js()
    assert "涨破" in js and "跌破" in js
    assert "GEX 突破" in js


def test_复用既有音效系统():
    """项目本来就有一套音效（static/sounds/ + initSfx/playSfx + 设置页总开关），
    提示音必须走它，不得自己 new Audio 或现场合成 —— 那样会绕过「提示音」总开关。
    """
    js = _js()
    assert "gexBreach: 'wall.mp3'" in js, "音效未登记进 SFX_FILES"
    assert "playSfx('gexBreach')" in js, "没走 playSfx = 绕过了提示音总开关"
    assert (ROOT / "static" / "sounds" / "wall.mp3").exists()


def test_提示音有全局冷却():
    """五只票同时破墙要弹五条横幅，但只能响一声，否则叠成噪音。"""
    js = _js()
    assert "GEX_SFX_COOLDOWN_MS" in js
    match = re.search(r"function playGexBreachSfx\(\)[\s\S]*?^\}", js, re.M)
    assert match, "找不到 playGexBreachSfx"
    assert "lastGexSfxAt" in match.group(0), "冷却必须是全局的，不是每票各算一份"


def test_设置页有开关且默认关():
    """2026-09-01 起默认关（用户决策），但开关本身要留着、且刷新后不许弹回来。

    关掉的理由：提醒把「涨破 call wall」和「跌破 put wall」当同一件事报，而这两道墙
    在净 Gamma 为正 / 为负时不是一回事 —— 正 Gamma 时墙是真阻力，负 Gamma 时墙压根
    不挡。判据没分正负，等于在负 Gamma 那半边一直发假警报。
    """
    js, html = _js(), _html()
    assert "toggleGexAlert" in html, "设置页缺少开关绑定"
    assert "GEX_ALERT_STORAGE_KEY" in js, "开关必须持久化，刷新后不能丢"
    match = re.search(r"function loadGexAlertEnabled\(\)[\s\S]*?^\}", js, re.M)
    assert match, "找不到 loadGexAlertEnabled"
    # 只有显式存过 '1' 才算开：读不到（无痕窗口/禁用存储）也当关着。
    assert "=== '1'" in match.group(0)
    assert "!== '0'" not in match.group(0), "还是老的默认开写法"


def test_存储键升到v2作废旧的手动开():
    """老浏览器里存着的 '1'（曾手动开过）必须一起作废。

    不换 key 的话「默认关」对已经开过的人不生效 —— 而那正是要关掉的那些人。
    """
    assert "'tt-trading.gexAlert.v2'" in _js()
    assert "'tt-trading.gexAlert.v1'" not in _js()


def test_提醒不参与下单判定():
    """提醒就是提醒：不得调用下单/闸门相关入口。"""
    js = _js()
    match = re.search(r"checkGexBreach\(\)\s*\{[\s\S]*?\n  \}", js)
    assert match, "找不到 checkGexBreach"
    body = match.group(0)
    for forbidden in ("submitOrder", "gate()", "placeOrder", "setState"):
        assert forbidden not in body, f"破墙提醒里不该出现 {forbidden}"


def test_后端未被拉进交易判定():
    """解耦约束第 2 条：gex 墙位不得进 validator。提醒做在前端，这条依然成立。

    查的是 **import**，不是字面量 —— validator 的注释里本来就写着「距墙阈值随 gex
    于 2026-08-11 整体下线」，那是历史说明不是引用，按字面量查会误报。
    """
    for path in (ROOT / "app" / "validator").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"^\s*(from|import)\s+\S*gex", text, re.M | re.I), path.name
