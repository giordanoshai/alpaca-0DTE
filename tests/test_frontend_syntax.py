# -*- coding: utf-8 -*-
"""前端 JS 语法守卫 —— `node --check` 每个 static/**.js。

**为什么需要这条**（2026-08-25 的教训）：本仓库 18 个 `test_frontend_*.py` 全部是
**字符串断言**（「某个标识符在不在源码里」），没有一个真去解析 JS。于是当
`static/app.js` 的一行注释被写坏、断成两行、第二行丢了 `//` 前缀时，
全量 1443 条测试**照样全绿**，而浏览器里是
`Uncaught SyntaxError: Invalid or unexpected token`，整个前端起不来。

字符串断言看的是「有没有这段文本」，语法错误恰恰是「文本对了但结构坏了」——
两者正交，必须各管各的。这条测试很便宜（一次 node --check），却能挡住所有
让页面白屏的语法错误。

注：`node --check` 只做**语法**检查，不执行、也不查运行时错误（拼错的变量名、
读空对象的属性都查不出来）。那类问题仍然靠各自的行为用例覆盖。
"""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
JS_FILES = sorted(p for p in (ROOT / "static").rglob("*.js") if p.is_file())


def test_至少扫到了主脚本():
    """防止 glob 写错导致这条测试空跑、假装通过。"""
    names = {p.name for p in JS_FILES}
    assert "app.js" in names, f"static 下没扫到 app.js，实际扫到：{sorted(names)}"


@pytest.mark.parametrize("js_path", JS_FILES, ids=lambda p: p.name)
def test_JS语法可解析(js_path: Path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("未安装 node，跳过语法检查")
    out = subprocess.run(
        [node, "--check", str(js_path)],
        capture_output=True, text=True, timeout=60,
    )
    assert out.returncode == 0, (
        f"{js_path.relative_to(ROOT)} 语法错误：\n{out.stderr.strip()}"
    )
