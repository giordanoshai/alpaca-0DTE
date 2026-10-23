# -*- coding: utf-8 -*-
"""卡片上的「±GEX」标签为什么现在不显示（2026-08-25 重写删除理由）。

**当初删它的理由是错的，但现在仍然不恢复 —— 理由换了。**

## 2026-08-21 的原始判决（已作废）

当时的做法：拿实时价跟 UW `gex-levels` 返回的 `gamma_flip` 比大小，价格在上面标
`+GEX`、下面标 `-GEX`。拉 10 只票跟「真实净 Gamma」逐点对照后 7 错 1 对，判定
「gamma_flip 不是零 Gamma 线，前提从根上不成立」，标签删除。

## 2026-08-25 实测：那次是口径错配

UW 的净 Gamma 不是一个数。`spot-exposures` 每行给**三个**，对应三种不同的做市商
持仓假设：

  `..._oi`   按未平仓合约算（还没平掉的存量期权）。这是「全市场日频汇总模型」的
             口径，**日内基本不翻号** —— 实测 6 只票里 5 只整天符号一动不动
             （SPY/QQQ 恒负，NVDA/TSLA/META 恒正）。
  `..._vol`  按当日成交量算，不区分主动买还是主动卖。
  `..._dir`  按成交的主动方向算：客户主动买入 = 做市商被动卖出 = 做市商负 Gamma。
             **这是盘中实时判断该用的口径**，日内会翻（SPY 2026-08-24 翻了 15 次）。

而 `gex-levels` 的返回自带一个 `source` 字段，实拉是 **`"vol"`** —— `gamma_flip`
是用 vol 口径算出来的分界线。

2026-08-21 那次对照用的净 Gamma 是 **oi 口径**（证据：那次记录的 SPY −8700M、
NVDA +939M、TSLA +632M、META +286M，与 2026-08-25 实测的 oi 口径在符号和量级上
逐一吻合）。**拿 vol 口径算出的线，去对 oi 口径的数，对不上是必然的。**

同一天（SPY 2026-08-24 常规时段 391 个采样点）、同一条 flip 线，按口径分开重算
「价在 flip 上方 ⇔ 净 Gamma 为正」的符合率：

    vol   40.9%
    dir   86.7%      <- 盘中该用的口径
    oi    11.0%      <- 2026-08-21 用的就是它，几乎全错

86.7% 带基线水分（那天 89% 的时间价格在 flip 上方、86% 的时间 dir 为正），拆开
看才干净：

    价格在 flip 上方时，净 Gamma 为正 = 90.8%（348 点中 316）
    价格在 flip 下方时，净 Gamma 为正 = 46.5%（ 43 点中  20）

差 44 个百分点。**gamma_flip 在 dir 口径下是有实质区分力的分界线。**

顺带，「负 Gamma 放大波动」这个说法在同一份数据上也成立：dir 判为 -GEX 的时段
每分钟波动 2.38bp，+GEX 时段 1.25bp，**1.90 倍**。但两组的有符号平均收益都约等于
零（+0.09bp / −0.02bp）—— **它放大的是波动幅度，不指方向。**

## 2026-08-25 恢复标签，但立了一条规矩

标签**已经加回来**了，正负号改为**直读后端下发的 `net_gex`**（后端读
`gamma_per_one_percent_move_dir` 真值，见 `UwClient.spot_gamma_dir`）。

**禁止再从价位推**：拿现价跟 `gamma_flip` 比大小这套，实测符合率其实不低
（上方 90.8% / 下方 46.5%），但它有个要命的不对称 —— 「上方 = +GEX」有 90.8% 准，
而「下方 = −GEX」只有 53.5% 准（≈ 抛硬币），偏偏 −GEX 才是关心的那个状态。
真值取得到（每票每轮 1 个请求，用户的 UW 档位 REST 请求无限），没有理由用推的。
本文件名里的 no_derived 说的就是这条，它依然成立。

**标签的含义不是「可以追」**：−GEX = 做市商放大波动（实测 1.46 倍），
**不指方向**。破墙后追进去实测是零到略负期望，所以破墙横幅同期改成了警示文案
（见 `gexBreachToast`）。完整验证见
`docs/reports/2026-08-25-GEX破墙策略验证.md` 与 [[gex-breach-no-direction]]。

"""
import re
from pathlib import Path
from frontend_src import app_js

ROOT = Path(__file__).resolve().parents[1]

# U+2212 减号（代码里标签用的是它，不是 ASCII 连字符）。
MINUS = "\u2212"


def _js() -> str:
    return app_js()


def _html() -> str:
    return (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def _js_code_only() -> str:
    """去掉整行 `//` 注释。

    只剔整行的，不碰行尾注释 —— 后者要真做得先分清 `//` 是注释还是字符串里的
    `https://`，不值得为一条断言写一个 JS 词法分析器。本文件要防的是「标签又被
    渲染出来」，那种代码不会藏在行尾注释里。
    """
    return "\n".join(line for line in _js().split("\n")
                     if not line.lstrip().startswith("//"))


def _html_markup_only() -> str:
    return re.sub(r"<!--.*?-->", "", _html(), flags=re.S)


# ------------------------------------------------------------ 标签回来了，但必须用真值


def test_卡片渲染正负GEX标签():
    """`+GEX` / `−GEX` 要出现在代码与模板里 —— 2026-08-25 恢复。"""
    js, html = _js_code_only(), _html_markup_only()
    assert "netGexLabel" in js, "卡片没有下发 ±GEX 标签字段"
    assert "{{ t.netGexLabel }}" in html, "模板没有渲染 ±GEX 标签"


def test_正负号来自后端net_gex字段():
    """标签只许读后端下发的 net_gex（那是 dir 口径真值），不许自己算。"""
    assert "net_gex" in _js(), "标签必须来自后端下发的 net_gex 字段"


def test_不得再由价位推正负号():
    """08-21 那个从价位推符号的函数不许回来。

    不是因为它不准（实测上方 90.8% 是准的），而是因为它在 −GEX 那一侧只有
    53.5% 准 —— 接近抛硬币，偏偏那是关心的那个状态。真值取得到就别推。
    """
    assert "netGexLabelOf" not in _js()


def test_标签的计算不碰gamma_flip():
    """结构性守卫：算标签那一行里不许出现 flip，否则就是又在拿价位推了。"""
    js = _js_code_only()
    for m in re.finditer(r"^.*netGexLabel\s*:.*$", js, flags=re.M):
        line = m.group(0)
        assert "flip" not in line.lower(), f"标签又从 flip 推出来了: {line.strip()}"


def test_标签随stale一起灰显():
    """墙位与 ±GEX 是同一份快照的两半，stale 时不该一半亮一半灰。"""
    assert "netGexColor" in _js()


# ------------------------------------------------------------ 破墙横幅：警示，不是鼓励


def test_破墙横幅带别追警示():
    """实测破墙后追进去是零到略负期望（30min: −GEX −0.02bp / +GEX −1.31bp），
    横幅必须说清楚，不能只报事实让人以为该追。"""
    match = re.search(r"const gexBreachToast = [\s\S]*?\n};", _js())
    assert match, "找不到 gexBreachToast"
    body = match.group(0)
    assert "别追" in body and "负期望" in body


def test_破墙横幅不按正负GEX分情况措辞():
    """两种环境下追突破的实测差别只有 0.4~2.3pp，落在「一辈子验不出来」的区间，
    按 ±GEX 给不同建议等于解读噪音。文案统一。"""
    match = re.search(r"const gexBreachToast = [\s\S]*?\n};", _js())
    body = match.group(0)
    assert "netGex" not in body and "net_gex" not in body
    assert "gammaFlip" not in body, "gamma_flip 不该传进文案函数"


# ---------------------------------------------------------------- 不该被误伤


def test_Flip价位仍然显示():
    """flip 本身是 UW 的真实数据，当普通参考线留着。"""
    assert "gammaFlipPx" in _js()
    assert "{{ t.gammaFlipPx }}" in _html()


def test_破墙提醒判据不受影响():
    js = _js()
    assert "gexBreachStep" in js
    assert "'below', 'above'" in js and "'above', 'below'" in js,         "涨破 call wall / 跌破 put wall 的方向判据不该被这次改动碰到"
    assert "涨破" in js and "跌破" in js


def test_横幅文案能真的换行():
    """文案是两行；toast 用 textContent，不设 white-space 的话 \n 会被折叠成空格。"""
    assert "pre-line" in _js(), "toast 的文本节点需要 white-space:pre-line"
