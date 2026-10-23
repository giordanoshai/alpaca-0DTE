# -*- coding: utf-8 -*-
"""买入按钮必须反映「这一侧现在真的下得了单吗」，而不只是「闸门 1–7 放行了吗」。

现场（2026-08-14 ASTS）：

    CALL  ASTS C70 · delta ≈0.50 · spread 18.5%   bid 0.93 mid 1.025 ask 1.12
    PUT   ASTS · 选约中… · delta — · spread —      bid — mid — ask —
    [买入 CALL  5张 = $512]   [买入 PUT  5张 = $0]     ← 两个都亮着

两侧都下不了单，却都是可点的：

  · PUT 没有合约。金额走 `Math.round((Number('—')||0)*100*5)` → **`$0`**，
    一个看起来很确定的假数字。
  · CALL 实时点差 18.5% 远超模板上限，规则 9 在下单那一刻必拒。

根因是分层：`gate()` 只读 `gates[方向].blocked`，那是 validator 的**规则 1–7**。
规则 8（资金）与 9（合约点差 / delta）需要 Contract，按设计不在那一帧里 ——
33 票每秒干跑一次，带 Contract 会打爆券商（见 `validate_market` 的 docstring）。
所以按钮压根不知道该侧有没有合约、点差多少。

**与 2026-08-10 那次被整体回退的 fail-closed 不同**：那次是「后端选约失败就拒单」
（当天 9 笔 `trade_order_rejected`）。这里只是如实反映前端**已经拿到**的数据，
且拿不到点差阈值时不判点差 —— 只会比现在少点错，不会多拦。
"""
import re
from pathlib import Path
from frontend_src import app_js

ROOT = Path(__file__).resolve().parents[1]


def _gate_src() -> str:
    js = app_js()
    m = re.search(r"\n  gate\(\) \{.*?\n  \}\n", js, re.S)
    assert m, "app.js 里找不到 gate() 定义"
    return m.group(0)


def test_没有合约的一侧不得放行():
    """`$0` 那个按钮就是这条断言的来历。"""
    src = _gate_src()
    assert "optionCode" in src, "gate() 完全没看合约，无法知道该侧能不能下单"
    assert "callBlocker === null" in src and "putBlocker === null" in src, src


def test_点差超上限的一侧不得放行():
    src = _gate_src()
    assert "max_spread_pct" in src, "gate() 没读点差上限，规则 9 必拒的单照样能点"


def test_拿不到点差阈值时不收紧():
    """08-10 的教训：买入路径 fail-closed 当天打出 9 笔拒单并被整体回退。

    阈值缺失时必须放行（只判有值的情况），否则一次下发缺字段就会全池变灰。
    """
    src = _gate_src()
    assert "isFinite(maxPct) && maxPct > 0" in src, "阈值缺失/非法时必须跳过点差判定"


def test_灰按钮要说得出为什么():
    """用户点了灰按钮得知道是闸门拦的还是合约不具备。"""
    src = _gate_src()
    assert "callWhy" in src and "putWhy" in src, src
    assert "'选约中…'" in src


def test_金额拿不到时不显示假的0():
    js = app_js()
    assert "const amount = (c) => '$' + Math.round((Number(c.mid) || 0) * 100 * uq);" not in js, \
        "旧写法会把「没有合约」渲染成 $0"
    assert "Number(c.mid) > 0" in js


def test_闸门放行判据本身没被改松():
    """只允许在闸门之上再加约束，绝不允许绕过 gates[方向].blocked。"""
    src = _gate_src()
    assert "gl.blocked === false" in src and "gs.blocked === false" in src, src
