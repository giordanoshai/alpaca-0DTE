"""合约卡片在「没有合约」时必须说清原因 —— 修 2026-08-03 IREN 空白面板。

现象：IREN 卡片显示「可做多 · 趋势 +88.40」，下方合约面板却是
`IREN — · delta — · spread —`、bid/mid/ask 全横杠，无任何解释。

后端其实一直在下发 `status` / `error` / `gate_allowed` / `gate.reason`
（`app/services/option_candidates.py::snapshot`），是前端把它们全丢了：
DISCOVERING（选约还没轮到）与 ERROR（选不出合约）渲染成同一串横杠。

选约是异步的 —— 并发上限 2、票池 30+ 个标的、单次约 0.85s，新放行的标的排到
队尾要等十几秒才有合约。这段等待必须可见，否则用户只能干瞪眼或误以为坏了。

这里用源码解析守住前端契约（与 test_frontend_primary_stage_labels.py 同法），
并用一个最小 JS 运行时校验 candidateNote 的分支。
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


def test_preview_branch_delegates_to_candidate_note():
    """无合约时的文案必须委托给 candidateNote，不在渲染处现写。

    2026-08-08 双边预取后调用点从 applyOptionPreviews 移到了 _sideContract
    （每票 CALL/PUT 各调一次），委托关系不变。
    """
    js = _app_js()
    assert "function candidateNote(preview, held)" in js
    assert "candidateNote(side, held)" in js
    assert "_sideContract(sym, side, old, held)" in js
    # 旧写法（HELD 之外一律 sym + ' —'）不得残留
    assert "t.candidateHeld ? (sym + ' · 已持仓') : (sym + ' —')" not in js


def test_backend_statuses_are_all_reachable_by_the_note():
    """后端可能给出的、selected 为空的状态，前端都要有对应文案。"""
    js = _app_js()
    for token in ("'DISCOVERING'", "'ERROR'", "gate_allowed", "preview.error"):
        assert token in js, f"candidateNote 未处理 {token}"


def _run_note(preview, held=False):
    """在 node 里执行 candidateNote，避免把分支逻辑在测试里重写一遍。"""
    node = shutil.which("node")
    if node is None:
        pytest.skip("未安装 node，跳过 JS 运行时校验（源码契约断言仍生效）")
    js = _app_js()
    match = re.search(
        r"const CANDIDATE_NOTE_MAXLEN = \d+;\s*\nfunction candidateNote\(preview, held\) \{.*?\n\}",
        js, re.S)
    assert match, "app.js 里找不到 candidateNote 定义"
    script = (
        match.group(0)
        + "\nconsole.log(JSON.stringify(candidateNote("
        + json.dumps(preview) + ", " + json.dumps(held) + ")));"
    )
    out = subprocess.run([node, "-e", script], capture_output=True, text=True,
                         encoding="utf-8", timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip())


def test_discovering_reads_as_in_progress_not_failure():
    assert _run_note({"status": "DISCOVERING", "gate_allowed": True}) == " · 选约中…"


def test_error_surfaces_backend_reason():
    assert _run_note({"status": "ERROR", "error": "无合格期权候选",
                      "gate_allowed": True}) == " · 无合格期权候选"


def test_error_without_reason_still_says_failed():
    assert _run_note({"status": "ERROR", "gate_allowed": True}) == " · 选约失败"


def test_gate_blocked_shows_gate_reason():
    assert _run_note({"status": "BLOCKED", "gate_allowed": False,
                      "gate": {"rule": "chop", "reason": "横盘震荡"}}) == " · 横盘震荡"


def test_held_wins_over_everything():
    # 2026-08-13 与 static/app.js candidateNote 同步：held 卡片副标题从「 · 已持仓」
    # 改为「 —」（用户 2026-08-12 在途文案调整）。
    assert _run_note({"status": "HELD", "gate_allowed": False,
                      "gate": {"reason": "已持仓"}}, held=True) == " —"


def test_long_reason_is_clipped_to_keep_card_layout():
    note = _run_note({"status": "ERROR", "error": "x" * 80, "gate_allowed": True})
    assert note.startswith(" · ") and note.endswith("…")
    assert len(note) <= 3 + 22


def test_unknown_shape_falls_back_to_dash():
    assert _run_note({"status": "BLOCKED", "gate_allowed": True}) == " —"
    assert _run_note(None) == " —"
