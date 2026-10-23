# -*- coding: utf-8 -*-
"""卡片顺序固定：前端不再对 TICKERS 做任何重排（2026-08-21）。

用户要求「把 trendscore 自动排序删了」，并在确认过代价后选了**全删**。

**为什么不是只删趋势分那一层**：原排序有四层——最新信号置顶 / 可交易在前 /
趋势分绝对值降序 / 池子原序。数字键 1~9、0 是**按卡片位置**绑定的
（`loadTickers()` 里按下标发 hot，渲染时再按下标发一次），位置一变，按 3
买到的就不是刚才看的那只票。只删趋势分那层的话，「可交易在前」照样会跳——
规则 6 方向闸上线后 1 分与 5 分不同向就 block，一只票一分钟能进出好几次。

**「最新信号置顶」不必保留**：信号来时另有一套机制把焦点跳到该票并滚动到
可视区（`_scrollToSym`），卡片不必再挪到第一位，两者是重复的。

**后端 pool 顺序不再有任何影响**：`feed_snapshot._order_pool` 仍按有无信号
重排 `pool` 数组，但前端一律按符号找票（`TICKERS.find(x => x.sym === tk.sym)`），
顺序只由 `/api/tickers` 的观察列表决定，改盯盘列表之外不会变。
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


def _code() -> str:
    """去掉整行注释后的源码 —— 说明文字里出现关键词不算违例。"""
    return "\n".join(
        line for line in _js().splitlines()
        if not line.lstrip().startswith("//")
    )


def test_不再对卡片数组排序():
    """任何一次 TICKERS.sort 都会让数字键与卡片错位。"""
    assert not re.search(r"TICKERS\s*\.\s*sort\s*\(", _code())


def test_趋势分排序辅助已删():
    """trendStrength 只为排序而存在，排序没了它就是死代码。"""
    assert "trendStrength" not in _code()


def test_不再按闸门状态重排():
    """「可交易在前」也一并删掉：规则 6 上线后 st 翻转频繁，它才是跳得最凶的一层。"""
    code = _code()
    assert "aTradable" not in code
    assert "bTradable" not in code


def test_不再按最新信号置顶():
    code = _code()
    assert "aLatest" not in code
    assert "bLatest" not in code


def test_数字键仍按下标绑定():
    """位置固定的前提是 hot 仍来自下标。改成按符号绑就是另一套设计了。"""
    code = _code()
    assert re.search(r"i\s*<\s*9\s*\?\s*String\(i\s*\+\s*1\)", code)


def test_按符号找票的写法保留():
    """顺序不再由后端决定，靠的就是这句按符号匹配。改回按下标取会立刻错位。"""
    assert re.search(r"TICKERS\.find\(\s*\(?\s*x\s*\)?\s*=>\s*x\.sym\s*===\s*tk\.sym",
                     _code())


def test_热键映射的实际行为():
    """把源码里那句 hot 映射抠出来真跑一遍：前九个是 1~9、第十个是 0、之后是点。"""
    code = _code()
    match = re.search(r"hot:\s*(i\s*<\s*9\s*\?[^,]+?:\s*\(i === 9[^,]+?\))\s*,", code)
    assert match, "找不到渲染层的 hot 映射表达式"
    node = shutil.which("node")
    if node is None:
        pytest.skip("未安装 node，跳过 JS 运行时校验（源码契约断言仍生效）")
    script = (
        "const f = (i) => (" + match.group(1) + ");"
        "console.log(JSON.stringify([0, 1, 8, 9, 10].map(f)));"
    )
    out = subprocess.run([node, "-e", script], capture_output=True, text=True,
                         encoding="utf-8", timeout=30)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == ["1", "2", "9", "0", "·"]
