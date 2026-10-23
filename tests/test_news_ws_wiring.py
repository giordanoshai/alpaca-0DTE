# -*- coding: utf-8 -*-
"""快讯滚动条的接线：WS 首帧补发 + lifespan 起停。

spec: docs/superpowers/specs/2026-09-05-盘中快讯滚动条-design.md

首帧补发是必须的：横条平时靠 `news_feed_refresh_loop` 推，而最高档消息一天才十几条。
浏览器中途刷新时若不补一帧，横条会一直空到下一条重要消息出现 —— 可能是几小时后。
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.routers import watcher_ws
from app.services import news_feed_service as svc

ROOT = Path(__file__).resolve().parents[1]


class _FakeWs:
    def __init__(self):
        self.sent = []

    async def send_json(self, payload):
        self.sent.append(payload)


def _item(cn="沃勒支持9月按兵不动", *, age_min=1):
    """一条队列里的条目。

    **时间戳相对「现在」给，不能写死一个历史时刻**：首帧补的是「此刻该显示的那些」
    （最近 30 分钟），写死的话这条用例哪天都测不到真正的补发路径——它会稳定地走进
    「没有可显示的、所以不推」那一支，而失败的样子跟接线断了一模一样。
    """
    ts = datetime.now(timezone.utc) - timedelta(minutes=age_min)
    return {"ts": ts.isoformat().replace("+00:00", "Z"), "et": "10:56", "cn": cn,
            "en": "Waller backs September hold", "lv": 2, "dir": "bull",
            "translated": True}


def test_ws_sends_news_snapshot_on_connect(monkeypatch):
    monkeypatch.setattr(svc.GLOBAL_NEWS_FEED, "_queue", [_item()])
    ws = _FakeWs()

    asyncio.run(watcher_ws._send_news_snapshot(ws))

    assert len(ws.sent) == 1
    assert ws.sent[0]["type"] == "news"
    assert ws.sent[0]["data"][0]["cn"] == "沃勒支持9月按兵不动"


def test_ws_snapshot_skips_stale_items(monkeypatch):
    """**首帧补的是「此刻该显示的」，不是整个队列。**

    队列里会留着上午的条目（它们担着去重的职责，不能就地裁掉），整个推给刚连上的
    人就成了「刷新一下、几小时前的消息又回来了」——用户 2026-09-10 报的正是这个。
    """
    monkeypatch.setattr(svc.GLOBAL_NEWS_FEED, "_queue",
                        [_item("上午那条", age_min=180), _item("刚刚这条", age_min=2)])
    ws = _FakeWs()

    asyncio.run(watcher_ws._send_news_snapshot(ws))

    assert [n["cn"] for n in ws.sent[0]["data"]] == ["刚刚这条"]


def test_ws_skips_snapshot_when_queue_empty(monkeypatch):
    """队列空就别推空帧，前端会被一个空数组清掉正在滚的内容。"""
    monkeypatch.setattr(svc.GLOBAL_NEWS_FEED, "_queue", [])
    ws = _FakeWs()

    asyncio.run(watcher_ws._send_news_snapshot(ws))

    assert ws.sent == []


def test_ws_snapshot_failure_does_not_break_connection(monkeypatch):
    """首帧失败不能连累连接本身（与 _send_tide_snapshot 同一原则）。"""
    def boom():
        raise RuntimeError("队列炸了")

    monkeypatch.setattr(svc.GLOBAL_NEWS_FEED, "recent", boom)
    asyncio.run(watcher_ws._send_news_snapshot(_FakeWs()))   # 不抛就算过


def test_connect_handler_sends_news_snapshot():
    """补发要真的挂在连接建立那条路上，不能只是定义了函数没人调。"""
    src = (ROOT / "app" / "routers" / "watcher_ws.py").read_text(encoding="utf-8")
    assert "_send_news_snapshot(websocket)" in src
    assert src.index("_send_tide_snapshot(websocket)") < src.index(
        "_send_news_snapshot(websocket)"), "补发顺序：先行情后新闻，别插在行情前面"


def test_main_registers_and_cancels_news_loop():
    """起了不收会在 Ctrl+C 时留一串未取消任务的回溯（见 main.py 关停原则）。"""
    src = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    assert "news_feed_refresh_loop" in src, "lifespan 没起快讯轮询"
    assert "news_task" in src
    # 清理列表里必须有它
    tail = src[src.index("finally:"):]
    assert "news_task" in tail, "news_task 没进关停清理"
