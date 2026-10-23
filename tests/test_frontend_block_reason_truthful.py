# -*- coding: utf-8 -*-
"""卡片上那句拦截理由不许自相矛盾：说「只能买 PUT」时 PUT 必须真能买。

**现场（2026-08-26 09:40 ET，META）**：卡片写「禁止」，理由写「5分钟向下，只能买
PUT」——而那一刻 PUT 侧正被 stall（动能在泄气）拦着。用户照着卡片去点 PUT，
按钮是灰的。GOOGL 同一分钟同样如此。

**2026-09-01：说「只能买 X」的那条规则（方向闸，rule 名 `mtf`）连同 stall 一起
删除了**，闸门里再没有任何一条会说这句话，`pickBlockReason` 也就不需要特判 ——
剩下的规则全部与方向无关，两侧要么都放行、要么撞同一堵墙。本文件保留下来钉住
两件事：函数照旧如实转述后端理由，且渲染处真的调了它。铁律本身不变 ——
「只能买 X 这句话，只有在 X 真能买时才可以说」，将来谁再加方向相关的规则，
得连这一整套一起想清楚。
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


def _pick_fn(js: str) -> str:
    """把 pickBlockReason 整个函数体抠出来，交给 node 真跑。"""
    match = re.search(r"\nfunction pickBlockReason\(.*?\n\}\n", js, re.S)
    assert match, "static/app.js 里找不到 pickBlockReason()"
    return match.group(0)


def _run(gl, gs):
    node = shutil.which("node")
    if node is None:
        pytest.skip("未安装 node，跳过 JS 运行时校验")
    script = (
        _pick_fn(_js())
        + "console.log(JSON.stringify(pickBlockReason("
        + json.dumps(gl) + "," + json.dumps(gs) + ")));"
    )
    out = subprocess.run([node, "-e", script], capture_output=True, text=True,
                         encoding="utf-8", timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def _blocked(rule, reason):
    return {"blocked": True, "rule": rule, "reason": reason}


_PASS = {"blocked": False, "rule": "", "reason": ""}


def test_方向无关的规则两侧同拦_照旧显示():
    """2026-09-01 之后这是常态：两侧撞同一堵墙，原样转述。"""
    got = _run(
        _blocked("afternoon", "午后禁入(≥12:00 ET)"),
        _blocked("afternoon", "午后禁入(≥12:00 ET)"),
    )

    assert got == "午后禁入(≥12:00 ET)"


def test_闸门理由里不许再出现只能买X():
    """守住铁律：现在没有任何一条规则会说「只能买 X」。

    这条红了说明有人加回了方向相关的拦截，同时得把 pickBlockReason 的特判
    （2026-09-01 删掉的那段）一起想清楚 —— 否则又会出现「卡片叫你买 PUT、
    PUT 按钮是灰的」。
    """
    core = (ROOT / "app" / "validator" / "core.py").read_text(encoding="utf-8")
    body = core.split('"""', 2)[-1]      # 去掉文件头 docstring 里的历史记录
    assert "只能买" not in body


def test_只有一侧被拦_就说那一侧():
    """单边被拦（只可能来自那一侧的合约问题）时，函数要给出那一侧的理由。"""
    assert _run(_blocked("contract", "合约点差 18.5% 超过上限"), _PASS)         == "合约点差 18.5% 超过上限"
    assert _run(_PASS, _blocked("contract", "无可用期权合约")) == "无可用期权合约"


def test_两侧都没有闸门结果_返回空串给上层兜底():
    """gates 取不到时（后端账户查询失败会退化成 null）交给调用方的兜底文案。"""
    assert _run(None, None) == ""


def test_渲染处真的用了这个函数():
    """函数写了却没接上等于没修 —— 钉住调用点。"""
    js = _js()
    assert re.search(r"t\.reason\s*=\s*t\.st === 'block'\s*\?\s*\(\s*pickBlockReason\(gl, gs\)", js), \
        "pool 合并处没有改用 pickBlockReason(gl, gs)"
