"""lifespan 优雅关停不变式测试。

历史问题：关停时 11 个后台任务全部 `.cancel()`，但只有 3 个进了 `asyncio.gather`。
只 cancel 不 await 的任务，在事件循环关闭时会抛「Task was destroyed but it is
pending!」，任务内部的异常也永远不会被取回 —— Ctrl+C 时那串异常回溯即由此而来。

用静态分析锁住两条不变式，避免以后新增后台任务时又漏掉 await。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

_MAIN = Path(__file__).resolve().parents[1] / "app" / "main.py"


@pytest.fixture(scope="module")
def lifespan_node() -> ast.AsyncFunctionDef:
    tree = ast.parse(_MAIN.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "lifespan":
            return node
    pytest.fail("app/main.py 中找不到 lifespan 函数")


def _list_vars(node: ast.AST) -> dict:
    """`background = [a, b, c]` 形式的列表变量 → 元素名集合。"""
    out: dict = {}
    for n in ast.walk(node):
        if isinstance(n, ast.Assign) and isinstance(n.value, ast.List):
            for tgt in n.targets:
                if isinstance(tgt, ast.Name):
                    out[tgt.id] = {e.id for e in n.value.elts if isinstance(e, ast.Name)}
    return out


def _cancelled_names(node: ast.AST) -> set:
    """所有出现 `X.cancel()` 的 X；`for t in background: t.cancel()` 展开成列表元素。"""
    list_vars = _list_vars(node)
    # 循环变量 → 被遍历的列表名，用于把 t.cancel() 归并到真实任务名
    aliases = {n.target.id: n.iter.id for n in ast.walk(node)
               if isinstance(n, ast.For) and isinstance(n.target, ast.Name)
               and isinstance(n.iter, ast.Name)}
    out = set()
    for n in ast.walk(node):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "cancel" and isinstance(n.func.value, ast.Name)):
            name = n.func.value.id
            if name in aliases:
                out |= list_vars.get(aliases[name], set())
            else:
                out.add(name)
    return out


def _awaited_names(node: ast.AST) -> set:
    """进了 asyncio.gather(...) 的名字，含 `*background` 这类列表展开。"""
    out = set()
    list_vars = _list_vars(node)

    for n in ast.walk(node):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "gather"):
            for arg in n.args:
                if isinstance(arg, ast.Name):
                    out.add(arg.id)
                elif isinstance(arg, ast.Starred) and isinstance(arg.value, ast.Name):
                    out |= list_vars.get(arg.value.id, set())
    return out


def test_每个被取消的后台任务都必须被await(lifespan_node):
    cancelled = _cancelled_names(lifespan_node)
    awaited = _awaited_names(lifespan_node)
    assert cancelled, "未解析到任何 .cancel() 调用，AST 提取可能失效"
    missing = cancelled - awaited
    assert not missing, (
        f"这些任务被 cancel 但从未 await，事件循环关闭时会抛 "
        f"'Task was destroyed but it is pending!'：{sorted(missing)}"
    )


def test_关停步骤逐个隔离(lifespan_node):
    """任一步失败/超时都不得中断后续收尾，否则会留下未关闭的连接。"""
    src = ast.get_source_segment(_MAIN.read_text(encoding="utf-8"), lifespan_node) or ""
    finally_src = src[src.index("finally:"):] if "finally:" in src else ""
    assert finally_src, "lifespan 缺少 finally 收尾块"
    assert "_step(" in finally_src, "收尾步骤未走隔离包装，单步异常会中断整条收尾链"
    # 隔离包装必须同时挡住超时与普通异常
    assert "asyncio.TimeoutError" in finally_src
    assert "except Exception" in finally_src


def test_收尾不得有裸的无限等待(lifespan_node):
    """关停期的 await 必须有界，否则会被 uvicorn 的 graceful timeout 强杀成回溯。"""
    src = _MAIN.read_text(encoding="utf-8")
    seg = ast.get_source_segment(src, lifespan_node) or ""
    finally_src = seg[seg.index("finally:"):] if "finally:" in seg else ""
    # finally 块里的 await 只允许两种：走 _step(...) 包装，或 _step 自身的 wait_for 限时
    bare = [ln.strip() for ln in finally_src.splitlines()
            if ln.strip().startswith("await ")
            and "_step(" not in ln and "asyncio.wait_for(" not in ln]
    assert not bare, f"收尾块存在未限时的裸 await：{bare}"
