# -*- coding: utf-8 -*-
"""交易终端前端源码的单一读取口。

**为什么有这个文件**：2026-09-02 把 `static/app.js`（3385 行）按职责拆成
`static/js/` 下的多个 ES module 之后，本仓库二十多个 `test_frontend_*.py` 的
字符串断言不能再只读入口文件 —— 它们断言的代码大多已经搬进子模块。这里把入口
和全部子模块按依赖顺序拼成一份源码返回，断言语义与拆分前完全一致。

**不去重、不改内容**：拼接顺序刻意贴着拆分前的原始顺序（叶子纯函数 → class App
的五个分片 → 装配层 → 入口），因为有些用例靠 `js.index(A) < js.index(B)` 判断
先后，也有靠 `\n  gate() {` 这类**带缩进**的正则抠函数体 —— 换顺序或改缩进都会
让它们静默失效。

注意：`static/watcher/app.js` 是盯盘页的另一份前端，不在这里；需要它的用例
（test_frontend_kline_gap_fill.py）自己读。
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"

# 拆分前的原始顺序。新增子模块必须登记进来，否则针对它的断言会静默落空 ——
# 下面 app_js_files() 里有一道守卫专门挡这件事。
_ORDER = (
    "js/shell.js",
    "js/template.js",
    "js/format.js",
    "js/self-check.js",
    "js/gex-alert.js",
    "js/hotkeys.js",
    "js/sfx.js",
    "js/app-core.js",
    "js/app-trade.js",
    "js/app-sync.js",
    "js/app-settings.js",
    "js/render.js",
    "js/runtime.js",
    "app.js",
)


def app_js_files() -> list[Path]:
    """按拼接顺序返回交易终端的全部前端源码文件。"""
    files = [STATIC / name for name in _ORDER]
    missing = [str(p.relative_to(STATIC)) for p in files if not p.is_file()]
    assert not missing, f"前端源码文件缺失：{missing}"

    # 拆出新文件却忘了登记 —— 断言会读不到它、悄悄变成空跑。宁可这里红。
    on_disk = set(STATIC.glob("*.js")) | set((STATIC / "js").glob("*.js"))
    unlisted = sorted(str(p.relative_to(STATIC)) for p in on_disk - set(files))
    assert not unlisted, f"这些前端文件没登记进 frontend_src._ORDER：{unlisted}"
    return files


def app_js() -> str:
    """交易终端前端全部源码，拼成一份字符串。"""
    return "\n".join(p.read_text(encoding="utf-8") for p in app_js_files())
