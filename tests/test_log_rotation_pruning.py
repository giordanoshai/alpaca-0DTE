"""日志轮转的 backupCount 剪枝 —— 锁死「改 suffix 必须同步改 extMatch」。

2026-08-05 实测发现：`ttt.log` 与 `ttt.events.jsonl` 两个 handler 都设了自定义
`suffix`（`%Y-%m-%d.log` / `%Y-%m-%d.jsonl`）却没动 `extMatch`，导致
`backupCount=30` **从来没删过任何文件**，轮转产物会一直堆到磁盘满。

根因（Python 3.12 `logging/handlers.py`）：
    getFilesToDelete() 用的是  self.extMatch.fullmatch(suffix)
    when='midnight' 的默认 extMatch 是  (?<!\\d)\\d{4}-\\d{2}-\\d{2}(?!\\d)

    "2026-08-04"        fullmatch -> True    （默认后缀，能剪）
    "2026-08-04.log"    fullmatch -> False   （自定义后缀，剪不掉）
    "2026-08-04.jsonl"  fullmatch -> False   （同上）

即：只要给 suffix 加了扩展名，剪枝正则就对不上自己生成的文件名了。这个陷阱很隐蔽
——轮转本身完全正常（文件名对、按天切），只有「30 天后清理」这一环静默失效，
而它要跑满 backupCount 天才会暴露。

本文件直接断言 `getFilesToDelete()` 能认出旧文件，而不是断言 extMatch 长什么样
——后者是实现细节，前者才是真正要的行为。
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.core.dailylogger import _get_shared_handlers


@pytest.fixture()
def 干净的handler工厂(tmp_path, monkeypatch):
    """每次都拿到全新的共享 handler，用完复原模块级全局，避免污染其他测试。"""
    import app.core.dailylogger as dl

    旧 = (dl._shared_file_handler, dl._shared_console_handler,
          dl._shared_queue_handler, dl._shared_jsonl_handler,
          dl._shared_suppressor)
    dl._shared_file_handler = None
    dl._shared_console_handler = None
    dl._shared_queue_handler = None
    dl._shared_jsonl_handler = None
    dl._shared_suppressor = None
    monkeypatch.delenv("TT_DISABLE_FILE_LOG", raising=False)
    try:
        yield lambda: _get_shared_handlers(str(tmp_path))
    finally:
        for h in (dl._shared_file_handler, dl._shared_jsonl_handler):
            if h is not None:
                h.close()
        (dl._shared_file_handler, dl._shared_console_handler,
         dl._shared_queue_handler, dl._shared_jsonl_handler,
         dl._shared_suppressor) = 旧


def _造旧文件(tmp_path, 基名: str, 扩展: str, 天数: int) -> None:
    """按 handler 真实的轮转命名造出历史文件：<基名>.<YYYY-MM-DD>.<扩展>

    必须是**连续不重复**的日期：`getFilesToDelete()` 末尾是
    ``result[:len(result) - self.backupCount]``，唯一文件数不超过 backupCount(30)
    时它恒返回空，测试就会误判成「剪枝坏了」。跨月生成即可拿到足够多的唯一日期。
    """
    起 = date(2026, 6, 1)
    for i in range(天数):
        d = 起 + timedelta(days=i)
        (tmp_path / f"{基名}.{d:%Y-%m-%d}.{扩展}").write_text("x", encoding="utf-8")


def test_文本日志的backupCount真的能剪枝(干净的handler工厂, tmp_path):
    file_h, *_ = 干净的handler工厂()
    assert file_h is not None
    _造旧文件(tmp_path, "ttt.log", "log", 35)
    待删 = file_h.getFilesToDelete()
    assert 待删, "getFilesToDelete() 一个都没认出来 —— backupCount 形同虚设，日志会堆到磁盘满"
    assert all(p.endswith(".log") for p in 待删)


def test_事件流的backupCount真的能剪枝(干净的handler工厂, tmp_path):
    *_, jsonl_h = 干净的handler工厂()
    assert jsonl_h is not None
    _造旧文件(tmp_path, "ttt.events.jsonl", "jsonl", 35)
    待删 = jsonl_h.getFilesToDelete()
    assert 待删, "getFilesToDelete() 一个都没认出来 —— backupCount 形同虚设，事件流会堆到磁盘满"
    assert all(p.endswith(".jsonl") for p in 待删)


def test_剪枝只认自己的扩展名不会误删邻居(干净的handler工厂, tmp_path):
    """两个 handler 的轮转产物同处一个目录，各剪各的，绝不能互相误删。"""
    file_h, _, _, jsonl_h = 干净的handler工厂()
    _造旧文件(tmp_path, "ttt.log", "log", 35)
    _造旧文件(tmp_path, "ttt.events.jsonl", "jsonl", 35)

    文本待删 = file_h.getFilesToDelete()
    事件待删 = jsonl_h.getFilesToDelete()
    assert 文本待删 and 事件待删
    assert not any(p.endswith(".jsonl") for p in 文本待删), "文本 handler 想删事件流文件"
    assert not any(p.endswith(".log") for p in 事件待删), "事件流 handler 想删文本日志"


def test_轮转后缀命名保持不变(干净的handler工厂):
    """修剪枝不能顺手改掉命名 —— log/ 里已有一批 ttt.log.2026-08-03.log 这样的历史文件。"""
    file_h, _, _, jsonl_h = 干净的handler工厂()
    assert file_h.suffix == "%Y-%m-%d.log"
    assert jsonl_h.suffix == "%Y-%m-%d.jsonl"


def test_剪枝正则不误伤非日期后缀(干净的handler工厂, tmp_path):
    """log/ 目录里有 ttt.log.bak08011132 这类人工备份，绝不能被当成轮转产物删掉。"""
    file_h, *_ = 干净的handler工厂()
    _造旧文件(tmp_path, "ttt.log", "log", 35)
    for 备份 in ("ttt.log.bak08011132", "ttt.log.bak_mock0729", "ttt.log.bak_07281756"):
        (tmp_path / 备份).write_text("x", encoding="utf-8")
    待删 = file_h.getFilesToDelete()
    assert 待删
    assert not any("bak" in p for p in 待删), "人工备份被当成轮转产物了"
