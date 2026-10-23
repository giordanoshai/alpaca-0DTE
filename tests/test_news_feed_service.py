# -*- coding: utf-8 -*-
"""盘中快讯滚动条的服务层（app/services/news_feed_service.py）。

spec: docs/superpowers/specs/2026-09-05-盘中快讯滚动条-design.md

设计立论（2026-09-03 现场实测）：那波 10:56 的行情大概率不是消息驱动（三家新闻源
在那一刻全空、长债纹丝不动），但真正有用的消息**盘前 08:33 就有了**（Waller 倾向
9 月不加息），只是没送到眼前。所以这个服务的价值全在「窗口含盘前」+「只推最高档」
两件事上，两者都由本文件钉住。

异步用例按本仓库惯例走 `asyncio.run()`（没装 pytest-asyncio）。
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from app.integrations.llm import LlmUnavailable
from app.services import news_feed_service as svc

ET = timezone(timedelta(hours=-4))


def et(day: str, hhmm: str) -> datetime:
    return datetime.fromisoformat(f"{day}T{hhmm}:00").replace(tzinfo=ET)


def uw_row(headline: str, ts: str = "2026-09-03T14:56:00Z") -> dict:
    return {"headline": headline, "created_at": ts, "source": "Tradex",
            "is_major": False, "sentiment": "neutral", "tickers": [], "tags": []}


class _FakeUw:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def news_headlines(self, *, source="Tradex", limit=100):
        self.calls.append((source, limit))
        return self.rows


class _FakeLlm:
    """按顺序吐预设结果；raises 非空时每次都抛。"""

    def __init__(self, payloads=None, raises=None):
        self.payloads = list(payloads or [])
        self.raises = raises
        self.calls = []

    def complete(self, prompt, max_tokens=1024):
        self.calls.append(prompt)
        if self.raises:
            raise self.raises
        return self.payloads.pop(0) if self.payloads else "[]"


def make(rows, llm=None):
    return svc.NewsFeedService(uw=_FakeUw(rows), llm=llm or _FakeLlm())


def poll(service, when):
    return asyncio.run(service.poll_once(when))


# ─────────────── 时间窗 ───────────────

def test_window_covers_premarket_0833():
    """整个功能的立论：9/3 唯一有用的消息在 08:33 盘前。窗口不含盘前就白做。"""
    assert svc.in_news_window(et("2026-09-03", "08:33")) is True


def test_window_boundaries():
    assert svc.in_news_window(et("2026-09-03", "07:59")) is False
    assert svc.in_news_window(et("2026-09-03", "08:00")) is True
    assert svc.in_news_window(et("2026-09-03", "16:05")) is True
    assert svc.in_news_window(et("2026-09-03", "16:06")) is False


def test_window_excludes_weekend():
    # 2026-09-05 是周六
    assert svc.in_news_window(et("2026-09-05", "10:00")) is False


def test_outside_window_does_not_call_uw():
    """窗口外一次请求都不该发 —— 半夜每分钟白打一次 UW 是纯浪费。"""
    s = make([uw_row("Fed cuts rates")])
    assert poll(s, et("2026-09-03", "03:00")) == []
    assert s._uw.calls == []


# ─────────────── 分档与去重 ───────────────

def test_only_level_2_enters_queue():
    """用户明确要求「盘中不想分心，只要重要的」。0/1 档算出来也不推。"""
    rows = [uw_row("Fed's Waller backs September hold"),
            uw_row("Kazakh central bank cuts key rate"),
            uw_row("Some PR fluff about a robot")]
    llm = _FakeLlm(['[{"i":0,"cn":"沃勒支持9月按兵不动","lv":2,"dir":"bull"},'
                    ' {"i":1,"cn":"哈萨克央行降息","lv":1,"dir":"neutral"},'
                    ' {"i":2,"cn":"某公司发布机器人","lv":0,"dir":"neutral"}]'])
    s = make(rows, llm)

    out = poll(s, et("2026-09-03", "10:56"))

    assert [x["cn"] for x in out] == ["沃勒支持9月按兵不动"]
    assert [x["cn"] for x in s.recent()] == ["沃勒支持9月按兵不动"]
    assert out[0]["dir"] == "bull"
    assert out[0]["translated"] is True


def test_same_headline_pushed_only_once():
    """UW 每轮返回最近 100 条，绝大部分上轮已经推过。"""
    rows = [uw_row("Fed's Waller backs September hold")]
    llm = _FakeLlm(['[{"i":0,"cn":"沃勒支持9月按兵不动","lv":2,"dir":"bull"}]',
                    '[{"i":0,"cn":"沃勒支持9月按兵不动","lv":2,"dir":"bull"}]'])
    s = make(rows, llm)

    first = poll(s, et("2026-09-03", "10:56"))
    second = poll(s, et("2026-09-03", "10:57"))

    assert len(first) == 1
    assert second == [], "同一条标题不能推第二次"
    assert len(s.recent()) == 1
    assert len(llm.calls) == 1, "全是旧条目时不该再花 15 秒去调模型"


def test_queue_is_capped():
    # 取满单轮上限（3 批 × 8 = 24 条）：既超过队列上限 20 能触发裁剪，
    # 又不会被 MAX_BATCHES_PER_ROUND 截掉尾巴（那是另一条用例管的事）。
    n_rows = svc.MAX_BATCHES_PER_ROUND * svc.BATCH_MAX
    assert n_rows > svc.QUEUE_MAX, "这条用例要靠 n_rows > QUEUE_MAX 才测得到裁剪"
    rows = [uw_row(f"Fed headline number {i}") for i in range(n_rows)]
    # 模型是按批调用的，每批只认得本批的序号（0 起），所以假数据也要按批给 ——
    # 一次性返回全部 25 条会让后面几批的序号对错行。
    payloads = []
    for start in range(0, len(rows), svc.BATCH_MAX):
        n = len(rows[start:start + svc.BATCH_MAX])
        payloads.append("[" + ",".join(
            f'{{"i":{j},"cn":"消息{start + j}","lv":2,"dir":"bull"}}'
            for j in range(n)) + "]")
    s = make(rows, _FakeLlm(payloads))

    poll(s, et("2026-09-03", "10:56"))

    assert len(s.recent()) == svc.QUEUE_MAX
    assert s.recent()[-1]["cn"] == f"消息{len(rows) - 1}", "留下的应该是最新那批"


# ─────────────── 模型挂掉时的降级 ───────────────

def test_llm_failure_falls_back_to_keywords():
    """端点单次要 15 秒，超时是迟早的事。整条横条空着比显示英文原文更糟。"""
    rows = [uw_row("Fed's Waller backs September rate hold"),
            uw_row("Some company wins a design award")]
    s = make(rows, _FakeLlm(raises=LlmUnavailable("timeout")))

    out = poll(s, et("2026-09-03", "10:56"))

    assert len(out) == 1, "只有命中宏观关键词的那条该留下"
    assert out[0]["translated"] is False, "没翻出来必须标出来，别让用户以为这就是全部"
    assert "Waller" in out[0]["en"]
    assert out[0]["lv"] == 2


def test_keyword_fallback_not_fooled_by_substrings():
    """`rate` 藏在 Accelerated / Corporate 里。子串匹配会把 FDA 批药当成利率消息。"""
    rows = [uw_row("FDA Grants Accelerated Approval To AstraZeneca Drug"),
            uw_row("Corporate bond issuance hits record")]
    s = make(rows, _FakeLlm(raises=LlmUnavailable("timeout")))

    assert poll(s, et("2026-09-03", "10:56")) == []


def test_watchlist_tickers_pass_the_fallback():
    rows = [uw_row("NVDA said to expand Hugging Face deal")]
    s = make(rows, _FakeLlm(raises=LlmUnavailable("boom")))

    out = poll(s, et("2026-09-03", "10:56"))
    assert len(out) == 1 and out[0]["translated"] is False


# ─────────────── 线程池隔离 ───────────────

def test_never_uses_default_executor(monkeypatch):
    """模型一次要 15~65 秒。丢进默认线程池会和下单抢槽（见 gex_wall_service 的说明）。"""
    called = []

    async def spy(fn, *a, **kw):
        called.append(fn)
        return fn(*a, **kw)

    monkeypatch.setattr(asyncio, "to_thread", spy)
    s = make([uw_row("Fed's Waller backs September hold")],
             _FakeLlm(['[{"i":0,"cn":"沃勒","lv":2,"dir":"bull"}]']))

    poll(s, et("2026-09-03", "10:56"))

    assert called == [], "阻塞调用必须走本模块专属线程池，不能碰 asyncio.to_thread"


# ─────────── 冷启动不能被历史条目淹没（2026-09-05 真实端到端暴露）───────────

def test_stale_headlines_are_marked_seen_but_not_translated():
    """UW 一次返回最近 100 条，冷启动时全都"没见过"。

    真实现场：首轮把 100 条切成 13 批送模型，一批 30~65 秒 —— 光第一轮就要跑好几分钟，
    而轮询间隔只有 60 秒。且那 100 条大多是几小时前的旧闻，滚动条要的是当下，
    翻译它们既没意义又烧 token。

    所以超龄条目只登记为「见过」（防下轮重复判断），**不送模型**。
    """
    fresh_ts = "2026-09-03T14:56:00Z"          # 与 now 同分钟
    stale_ts = "2026-09-03T11:00:00Z"          # 近 4 小时前
    rows = [uw_row("Fed's Waller backs September hold", fresh_ts)] + \
           [uw_row(f"Old headline {i}", stale_ts) for i in range(60)]
    llm = _FakeLlm(['[{"i":0,"cn":"沃勒支持9月按兵不动","lv":2,"dir":"bull"}]'])
    s = make(rows, llm)

    out = poll(s, et("2026-09-03", "10:56"))

    assert len(llm.calls) == 1, "超龄条目不该被送进模型"
    assert "Old headline" not in llm.calls[0], "提示词里混进了旧闻"
    assert [x["cn"] for x in out] == ["沃勒支持9月按兵不动"]


def test_stale_headlines_not_reprocessed_next_round():
    """旧闻虽然没翻译，也必须登记为见过 —— 否则每轮都要重新过一遍判龄。"""
    stale = [uw_row(f"Old headline {i}", "2026-09-03T11:00:00Z") for i in range(5)]
    s = make(stale, _FakeLlm())

    poll(s, et("2026-09-03", "10:56"))
    assert len(s._seen) == 5, "超龄条目也要进已见集合"


def test_batches_per_round_are_capped():
    """极端情况（长时间断线后恢复）也不能一轮塞几十批进去。"""
    rows = [uw_row(f"Fed headline {i}", "2026-09-03T14:56:00Z") for i in range(80)]
    llm = _FakeLlm(["[]"] * 50)
    s = make(rows, llm)

    poll(s, et("2026-09-03", "10:56"))

    assert len(llm.calls) <= svc.MAX_BATCHES_PER_ROUND, \
        f"一轮最多 {svc.MAX_BATCHES_PER_ROUND} 批，实际 {len(llm.calls)}"


# ─────────────── 滚动条上留多久 ───────────────
#
# 用户 2026-09-10 原话：「那个新闻啊，你不要整天的都显示，我觉得没必要啊，
# 只要显示前 30 分钟的就行。」
#
# **`recent()` 和 `visible()` 是两件事，别合并**：前者是「队列里有什么」（去重、
# 裁剪这些内部逻辑都靠它），后者是「此刻该显示什么」。合成一个的话，队列裁剪那几条
# 用例就得跟着时钟走，而它们测的根本不是时间。

def _queued(s, *ages_min):
    """往队列里直接塞几条，年龄按分钟给（相对 2026-09-03 14:56Z）。"""
    base = datetime.fromisoformat("2026-09-03T14:56:00+00:00")
    s._queue = [{"ts": (base - timedelta(minutes=m)).isoformat().replace("+00:00", "Z"),
                 "et": "", "cn": f"消息{m}", "en": f"news {m}",
                 "lv": 2, "dir": "neutral", "translated": True}
                for m in ages_min]
    return base


def test_只显示最近30分钟的():
    """页面开一整天时，早上那批还挂在滚动条上轮播——正是用户报的毛病。"""
    s = make([], _FakeLlm())
    base = _queued(s, 90, 45, 31, 29, 5, 0)

    got = [it["cn"] for it in s.visible(base)]

    assert got == ["消息29", "消息5", "消息0"], "只留 30 分钟以内的"


def test_边界那一条算在里面():
    """正好 30 分钟的留下。**边界写死在一处**，免得两边各判各的差一条。"""
    s = make([], _FakeLlm())
    base = _queued(s, 30)
    assert len(s.visible(base)) == 1


def test_时间戳解析不了的照常显示():
    """**宁可多显示一条，也不要把真消息吞掉。**

    跟 `_age_s()` 那条「时间戳缺失时返回 0」是同一个取舍：这一栏是盯盘用的，
    漏掉一条重要消息的代价，比多挂一条旧的大得多。
    """
    s = make([], _FakeLlm())
    base = _queued(s, 5)
    s._queue.append({"ts": "", "cn": "坏时间戳", "en": "bad", "lv": 2,
                     "dir": "neutral", "translated": True, "et": ""})

    assert [it["cn"] for it in s.visible(base)] == ["消息5", "坏时间戳"]


def test_展示窗口不动队列本身():
    """`visible()` 只是个视图。就地裁掉队列的话，一条刚过 30 分钟的消息会被永久
    丢掉——而队列还担着去重和「回头扫一眼」的职责。"""
    s = make([], _FakeLlm())
    base = _queued(s, 90, 5)

    s.visible(base)

    assert len(s.recent()) == 2, "visible() 不许就地改队列"


def test_展示窗口比送模型那道龄限宽():
    """两个数管的是两件事，别混：`NEWS_MAX_AGE_S`（15 分钟）管「这条还值不值得
    花 token 去翻」，`NEWS_SHOW_MAX_AGE_S`（30 分钟）管「翻好的还该在屏幕上留多久」。

    反过来（展示窗口比送模型那道还短）的话，会出现刚翻译好就已经不该显示了——
    白烧一轮 token。
    """
    assert svc.NEWS_SHOW_MAX_AGE_S > svc.NEWS_MAX_AGE_S
