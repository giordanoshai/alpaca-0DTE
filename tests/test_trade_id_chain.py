"""贯穿 ID 验收 —— 任取一个 trade_id，grep 一遍就能重建全链路。

2026-08-03 复盘时这条做不到：resting_close_filled 只有 contract，我只能靠
「合约 + 时间最近」猜它归属哪张单；close 完全没有 order_id；execute 的
order.id 埋在子对象里。本文件把那次的痛点变成可执行断言。

2026-08-04 校正：`trade_close_order_submitted` / `trade_close_order_status`
两个事件名在此前的重构中已经从 `app/exec_close.py` 迁到了
`app/order_pricing.py`（`atomic_close()` 内），因此源码扫描列表里也把它带上，
否则正则找不到这两个事件块，测试会产生假阴性。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 这些事件必须能只靠顶层 trade_id 归属到某笔交易
# 2026-08-04 最终审查修复波（I-1）：atomic_close() 内另有 10 处 trade_close_*
# 事件当初按「历史上 join 不上的事件名」列出、不是按调用点扫描，漏了这些——
# 其中 trade_close_order_repriced / trade_close_order_reprice_failed 是追价
# 换单事件，是复盘平仓滑点符号的关键数据，尤其不能漏。
必须带TRADE_ID = {
    "execute", "close", "resting_close_filled", "timeout_cancel_buy",
    "trade_close_order_submitted", "trade_close_order_status",
    "trade_close_stale_open_snapshot_ignored",
    "trade_close_prior_order_cancelled",
    "trade_close_prior_order_already_terminal",
    "trade_close_prior_order_cancel_failed",
    "trade_close_prior_order_cancel_unconfirmed",
    "trade_close_prior_order_position_changed",
    "trade_close_position_snapshot_floor_used",
    "trade_close_order_repriced",
    "trade_close_order_reprice_failed",
}


def _源码(*相对路径):
    return "\n".join((ROOT / p).read_text(encoding="utf-8") for p in 相对路径)


def _全部埋点块(src: str, 事件: str) -> list[str]:
    """返回源码里该事件名【全部】埋点块，不是只挡第一处。

    2026-08-04 审查发现的缺口：旧版用 ``re.search`` 只找源码里"第一次出现"
    的那一处——`trade_close_order_status` 在 ``order_pricing.py`` 里实际有
    7 处、`trade_close_order_submitted` 有 2 处，`re.search` 只验证了其中
    各 1 处，另外 7 处删掉 `trade_id` 这条测试也测不出来。改用 ``re.findall``
    遍历全部非重叠匹配，逐块都要断言。

    两种调用形态互斥（同一事件名只用一种）：raw ``logger.info(json.dumps(
    {...}))`` 字典形态以连续 ``))`` 收尾；``audit("event", ...)`` 位置实参
    调用形态以单个 ``)`` 收尾。非贪婪 ``.{0,600}?`` 遇到形参里任何字面 ``)``
    （如 ``order.get("id")``）就会提前收尾，但本项目全部 9 处 `trade_id=`
    都紧跟在事件名之后（书写顺序上是第一个关键字参数），落在提前收尾的边界
    之前，不受影响；即便某处顺序变了导致误判，也是宁可漏报不会误报——因为
    截断点只会更早，不会让本该缺失的 `trade_id` 假装存在。

    2026-08-11：窗口由 600 放宽到 900。`execute` 埋点加入提交前分段耗时字段后
    块长 581，离 600 只剩 19 字符的余量 —— 下一个往该事件加字段的人会撞上
    「一个埋点都没扫到」这条报错，而它读起来像是漏了 trade_id，极易误诊。
    放宽是安全的：当前 `必须带TRADE_ID` 里每个事件的终止符都落在 600 以内
    （否则测试现在就是红的），因此放宽**不改变任何现有块的匹配结果**，只是
    让窗口不再是绊线。逐块的 `trade_id` 断言一字未动。
    """
    块列表 = re.findall(
        r'"event"\s*:\s*"' + re.escape(事件) + r'"(.{0,900}?)\)\)',
        src, re.S)
    if 块列表:
        return 块列表
    return re.findall(
        r'audit\(\s*"' + re.escape(事件) + r'"(.{0,600}?)\)',
        src, re.S)


def test_六个事件的埋点处都写了trade_id():
    src = _源码(
        "app/exec_entry.py", "app/exec_close.py", "app/watchdog.py",
        "app/order_pricing.py",
    )
    for 事件 in 必须带TRADE_ID:
        块列表 = _全部埋点块(src, 事件)
        assert 块列表, f"{事件} 源码里一个埋点都没扫到（正则本身失效，不是漏 trade_id）"
        for 序号, 块 in enumerate(块列表, start=1):
            assert "trade_id" in 块, (
                f"{事件} 第 {序号}/{len(块列表)} 处埋点没带 trade_id")


def test_全链路经真实日志栈落盘后可由trade_id重建(tmp_path):
    """驱动真实 logger + EventFilter + JsonlEventHandler 落盘，再读回校验。

    不是过滤一个手搭的列表——那样只断言了测试自己的 fixture。这里事件必须真的
    穿过过滤层、信封注入、序列化、写盘，再从磁盘读回来才算数。
    """
    import logging

    from app.core.event_stream import EventFilter, JsonlEventHandler
    import app.trade_recorder as tr

    TID = "855a0f47-d48f-4d39-85d2-12e8b332032d"
    path = tmp_path / "e.jsonl"
    # encoding 显式指定 utf-8：与生产侧 app/core/dailylogger.py 的构造方式一致
    # （否则在非 UTF-8 系统 locale 下会按平台默认编码写盘，落盘的中文字段回读乱码）。
    h = JsonlEventHandler(str(path), encoding="utf-8")
    h.addFilter(EventFilter())
    logger = logging.getLogger("tt.trade")
    旧handlers, 旧propagate = logger.handlers, logger.propagate
    logger.handlers, logger.propagate = [h], False
    logger.setLevel(logging.INFO)
    try:
        tr.audit("trade_intent_accepted", intent_id="bic67ac21236")
        tr.audit("trade_order_submitted", order_id=TID, contract="SOFI260807C00017000")
        tr.audit("trade_order_filled", order_id=TID, fill_price="0.41")
        tr.audit("trade_close_order_submitted", trade_id=TID, limit_price="0.59")
        tr.audit("trade_position_closed", order_id=TID, exit_price="0.51")
        tr.audit("trade_order_submitted", order_id="别的单", contract="QQQ260803C00692000")
        h.flush()
    finally:
        logger.handlers, logger.propagate = 旧handlers, 旧propagate

    行 = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    # 断言分两段，不是疏漏：`trade_intent_accepted` 此刻订单还不存在，天然拿不到
    # order_id/trade_id（见 design §4.3 2026-08-04 修正口径）。所以第一段只按
    # trade_id 校验「下单之后」的链路（submitted→filled→close→closed），第二段
    # 单独按 intent_id 校验受理事件——两段合起来才是「受理→平仓」全链路，中间
    # 那一跳要靠 trade_intent_resolved（同时带 intent_id 和 order_id）拼接，
    # 这里没有模拟 trade_intent_resolved，故不对这一跳做断言，只证明两端各自可查。
    链 = [e for e in 行 if e.get("trade_id") == TID]
    assert [e["event"] for e in 链] == [
        "trade_order_submitted", "trade_order_filled",
        "trade_close_order_submitted", "trade_position_closed",
    ]
    assert all("run_id" in e and "ts" in e for e in 链), "信封字段必须已注入"
    assert [e for e in 行 if e["event"] == "trade_intent_accepted"][0]["intent_id"] == "bic67ac21236"
    assert not any(e.get("trade_id") == TID for e in 行 if e.get("contract") == "QQQ260803C00692000")
