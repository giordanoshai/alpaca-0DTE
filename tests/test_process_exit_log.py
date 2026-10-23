# -*- coding: utf-8 -*-
"""进程退出必须在日志里留痕。

2026-08-28 21:44:27 进程突然没了，查不出原因：uvicorn 自己的输出从来没进过文件日志
（当天与前一天 `ttt.log` 里 "uvicorn" 出现 0 次），`lifespan` 的收尾步骤只在超时/出错
时才写日志、正常关停一行不写，Windows 应用事件日志也没有任何崩溃记录。于是
「日志里没有关停记录」既不能证明是崩溃、也不能证明是手动停的 —— 现场是空的。

修法：退出路径上强制留一条 `process_exit` 结构化事件，说清是正常退出、Ctrl+C
还是抛异常死的（抛异常连回溯一起写进去）。
"""
from __future__ import annotations

import json
import logging

import pytest

from app.lifecycle import log_process_exit, run_with_exit_log


def _events(caplog) -> list[dict]:
    """只取带 `event` 键的结构化行 —— 机读事件流（ttt.events.jsonl）也只收这种。"""
    out = []
    for record in caplog.records:
        try:
            payload = json.loads(record.getMessage())
        except (ValueError, TypeError):
            continue
        if isinstance(payload, dict) and "event" in payload:
            out.append(payload)
    return out


def test_正常退出留一条记录(caplog):
    with caplog.at_level(logging.INFO):
        log_process_exit(None)

    events = _events(caplog)
    assert len(events) == 1
    assert events[0]["event"] == "process_exit"
    assert events[0]["cause"] == "normal"


def test_ctrl_c_退出记成信号(caplog):
    with caplog.at_level(logging.INFO):
        log_process_exit(KeyboardInterrupt())

    assert _events(caplog)[0]["cause"] == "signal"


def test_非零退出码单独成一类(caplog):
    """uvicorn 端口占用等致命启动错误走的是 SystemExit(1)，不是 Ctrl+C。

    混成「signal」会把人引去找一个根本没发生过的 Ctrl+C —— 这次事故就吃了
    「现场看不出是哪种死法」的亏，新加的留痕不许再造一个含混分类。
    """
    with caplog.at_level(logging.INFO):
        log_process_exit(SystemExit(1))

    event = _events(caplog)[0]
    assert event["cause"] == "exit_code"
    assert event["exit_code"] == 1


def test_零退出码算正常(caplog):
    with caplog.at_level(logging.INFO):
        log_process_exit(SystemExit(0))

    assert _events(caplog)[0]["cause"] == "normal"


def test_崩溃退出带异常与回溯(caplog):
    try:
        raise ValueError("引擎炸了")
    except ValueError as exc:
        with caplog.at_level(logging.INFO):
            log_process_exit(exc)

    event = _events(caplog)[0]
    assert event["cause"] == "exception"
    assert "ValueError" in event["error"] and "引擎炸了" in event["error"]
    # 回溯是这次事故里唯一缺的东西：没有它只知道「没了」，不知道死在哪
    assert "Traceback" in event["traceback"]


def test_服务器正常返回时留正常退出(caplog):
    with caplog.at_level(logging.INFO):
        run_with_exit_log(lambda: None)

    assert _events(caplog)[0]["cause"] == "normal"


def test_服务器抛异常时先留痕再原样抛出(caplog):
    def _boom():
        raise RuntimeError("端口被占")

    with caplog.at_level(logging.INFO):
        with pytest.raises(RuntimeError, match="端口被占"):
            run_with_exit_log(_boom)

    event = _events(caplog)[0]
    assert event["cause"] == "exception"
    assert "端口被占" in event["error"]
