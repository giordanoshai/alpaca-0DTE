# -*- coding: utf-8 -*-
"""票池卡片的状态标签不得替用户选方向 —— 只有「可买入 / 禁止」两态。

为什么这是个契约而不是审美：

规则 6「反向硬拦截」已于 2026-08-08 删除（趋势方向没有前瞻 alpha，25.7 万分钟
审计实测顺方向 fwd15 胜率 49.2%），规则 7 追高又于 2026-08-14 改为两侧同拦。
至此 `validate_market` 的规则 1–7 **全部与 `request.direction` 无关**。

于是四态标签「可买入 / 可做多 / 可做空 / 禁止」里的中间两态已无对应的后端状态：
它们读起来像系统在推荐方向，正是被删掉的那条规则的语气。票池列表要回答的是
"这票能不能入场"。（本文件写于两态改造当天，彼时规则 7 还是单侧、中间两态尚有
"另一侧被追高闸拦着"这层含义；两侧同拦之后连这层也没了。）

`st` 的四态本身保留：红绿灯颜色与文案要用，按钮放行另走 `gate()`
直接读 `gates[方向].blocked`，与本标签无关——所以改标签不改任何放行行为。
（2026-08-21 卡片排序整体删除后，`st` 不再有排序这层用途。）
（与 test_frontend_candidate_note.py 同法：源码契约断言 + 最小 JS 运行时校验。）
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from frontend_src import app_js

ROOT = Path(__file__).resolve().parents[1]


def _app_js() -> str:
    return app_js()


def _ll_source(js: str) -> str:
    match = re.search(r"const ll = \(st\) => .*?;", js)
    assert match, "app.js 里找不到状态标签函数 ll"
    return match.group(0)


def test_标签不再按方向分档():
    """旧的四态写法不得残留。断言打在**代码**上，注释里提这段历史是允许的。"""
    src = _ll_source(_app_js())
    assert "'可做多'" not in src, src
    assert "'可做空'" not in src, src
    assert "'both'" not in src, "两态标签不该再区分 both/long/short"


def test_标签只认block一个判据():
    src = _ll_source(_app_js())
    assert "st === 'block'" in src, src
    assert "'禁止'" in src and "'可买入'" in src, src


def test_四种st只产出两种标签():
    """运行时校验：long / short / both 必须给出完全相同的一句话。"""
    node = shutil.which("node")
    if node is None:
        pytest.skip("未安装 node，跳过 JS 运行时校验（源码契约断言仍生效）")
    script = (
        _ll_source(_app_js())
        + "\nconsole.log(JSON.stringify(['both','long','short','block'].map(ll)));"
    )
    out = subprocess.run([node, "-e", script], capture_output=True, text=True,
                         encoding="utf-8", timeout=30)
    assert out.returncode == 0, out.stderr
    both, long_, short, block = json.loads(out.stdout)
    assert both == long_ == short, f"单边可买不得说成另一句话：{both} / {long_} / {short}"
    assert block != both
    assert len({both, block}) == 2


def test_按钮放行与本标签无关():
    """守住"改标签不改放行"：CALL/PUT 各读各的 `gates[方向].blocked`，不读 `st`。

    断言打在**意图**上而不是逐字匹配 —— 2026-08-14 给 `gate()` 加了合约就绪自查
    （`tests/test_frontend_buy_button_readiness.py`），逐字版当场误报了一次。
    允许在闸门之上再加约束，不允许绕过闸门或改读 `st`。
    """
    js = _app_js()
    m = re.search(r"\n  gate\(\) \{.*?\n  \}\n", js, re.S)
    assert m, "app.js 里找不到 gate() 定义"
    src = m.group(0)
    assert "gl.blocked === false" in src and "gs.blocked === false" in src, src
    # 词边界：裸 `.st` 会误命中 `this.state.focus`（写这条时当场踩了一次）。
    assert not re.search(r"\.st\b", src), "按钮放行读了 st —— 标签口径不得反向影响下单权限"
    # 2026-08-21：原来这里还搭车断言「排序仍依赖 st 的四态」。卡片排序已整体删除
    # （tests/test_frontend_no_card_sorting.py），那条断言随之作废。`st` 四态本身
    # 仍然保留——红绿灯颜色与文案还读它，见 test_frontend_dual_side_gate.py 的
    # test_light_has_both_state。
