"""事件流信封字段 —— 锁死七个保留字与键冲突策略。

2026-08-03 复盘的三个痛点直接对应本文件：
  - 11 次重启无法切段            -> run_id
  - 无 paper/live 标记，只能从三方库日志反推 -> mode
  - 事件散在文本里，来源要靠正则拆前缀      -> lv/logger/src
"""
from __future__ import annotations

import json
import logging

from app.core.event_stream import (ENVELOPE_KEYS, RUN_ID, EventFilter,
                                   JsonlEventHandler, get_mode, set_mode)


def _emit(handler: JsonlEventHandler, msg: str, level: int = logging.INFO) -> dict:
    rec = logging.LogRecord("tt.trade", level, "trade_recorder.py", 58, msg, (), None)
    assert EventFilter().filter(rec) is True
    return json.loads(handler.format(rec))


def test_七个信封字段全部注入(tmp_path):
    h = JsonlEventHandler(str(tmp_path / "e.jsonl"))
    out = _emit(h, '{"event":"trade_order_filled","fill_price":"0.41"}')
    for key in ENVELOPE_KEYS:
        assert key in out, key
    assert out["run_id"] == RUN_ID
    assert out["lv"] == "INFO"
    assert out["logger"] == "tt.trade"
    assert out["src"] == "trade_recorder.py:58"
    assert out["event"] == "trade_order_filled"
    assert out["fill_price"] == "0.41"


def test_键冲突时业务原值挪到evt后缀两边都不丢(tmp_path):
    h = JsonlEventHandler(str(tmp_path / "e.jsonl"))
    out = _emit(h, '{"event":"x","ts":"业务自己的时间","mode":"业务自己的模式"}')
    assert out["ts"] != "业务自己的时间"      # 信封字段权威
    assert out["ts_evt"] == "业务自己的时间"  # 业务原值不丢
    assert out["mode"] == get_mode()
    assert out["mode_evt"] == "业务自己的模式"


def test_键冲突升级_业务同时带key与key_evt两个值都不丢(tmp_path):
    # 回归：审查发现的双键覆盖缺口——若业务 payload 同时含 `ts` 和字面上
    # 就叫 `ts_evt` 的键，旧实现按字典遍历顺序后者会静默覆盖前者，真的
    # 丢数据。这里断言信封值、业务 `ts` 原值、业务 `ts_evt` 原值三者必须
    # 全部能在输出里找到，一个不少，且互不相同（没有发生覆盖）。
    h = JsonlEventHandler(str(tmp_path / "e.jsonl"))
    out = _emit(h, '{"event":"x","ts":"业务ts原值","ts_evt":"业务ts_evt原值"}')
    assert out["ts"] != "业务ts原值"           # 信封值权威
    assert "业务ts原值" in out.values()
    assert "业务ts_evt原值" in out.values()
    assert len({out["ts"], "业务ts原值", "业务ts_evt原值"}) == 3


def test_键冲突升级_三层撞名ts_ts_evt_ts_evt2都不丢(tmp_path):
    # 更深一层的连环撞名：`ts` 撞信封字段、`ts_evt` 撞 `ts` 的挪位目标、
    # `ts_evt2` 又撞 `ts` 与 `ts_evt` 冲突后的下一个候选位。三个业务值
    # 必须全部保留，互不覆盖。
    h = JsonlEventHandler(str(tmp_path / "e.jsonl"))
    out = _emit(h, '{"event":"x","ts":"T1","ts_evt":"T2","ts_evt2":"T3"}')
    assert out["ts"] not in ("T1", "T2", "T3")  # 信封值权威，不是业务值
    for v in ("T1", "T2", "T3"):
        assert v in out.values(), v
    assert len({out["ts"], "T1", "T2", "T3"}) == 4  # 四个值分落四个不同的键


def test_mode由外部注册且默认unknown(tmp_path):
    h = JsonlEventHandler(str(tmp_path / "e.jsonl"))
    set_mode("unknown")
    assert _emit(h, '{"event":"x"}')["mode"] == "unknown"
    set_mode("paper")
    assert _emit(h, '{"event":"x"}')["mode"] == "paper"
    set_mode("unknown")


def test_输出是单行不含换行(tmp_path):
    h = JsonlEventHandler(str(tmp_path / "e.jsonl"))
    line = h.format(_rec_for(h))
    assert "\n" not in line


def _rec_for(handler):
    rec = logging.LogRecord(
        "tt.gate", logging.INFO, "watchdog.py", 110,
        '{"event":"resting_close_filled","note":"多行\\n文本"}', (), None)
    EventFilter().filter(rec)
    return rec


def test_不可序列化对象不炸走default_str(tmp_path):
    from decimal import Decimal
    h = JsonlEventHandler(str(tmp_path / "e.jsonl"))
    rec = logging.LogRecord("tt.trade", logging.INFO, "x.py", 1, "{}", (), None)
    rec._event_payload = {"event": "x", "price": Decimal("0.41")}
    assert json.loads(h.format(rec))["price"] == "0.41"


def test_不传encoding也用utf8写盘中文原样回读(tmp_path):
    """2026-08-04 审查 Task 6 第 1 轮修复的回归锁死点。

    这条流上常年混着中文业务字段（``reason="手动开仓"``、``reason="watchdog:
    超时 15分"`` 等）。此前 ``JsonlEventHandler`` 未在 ``__init__`` 兜底
    ``encoding``，全靠调用方人肉记得传 ``encoding='utf-8'``——生产侧
    ``app/core/dailylogger.py`` 传了能跑对，但本仓库其余测试（包括本文件
    上面的用例、``tests/test_event_stream_wiring.py``）构造该类时都没传，
    只是恰好payload 全是 ASCII 没暴露问题。这里故意不传 ``encoding``，走真实
    ``logger.info()`` -> ``emit()`` -> 落盘 -> 用 UTF-8 读回，验证类自身的
    默认值已经顶住，不必再靠调用方记得传参。
    """
    path = tmp_path / "e.jsonl"
    h = JsonlEventHandler(str(path))  # 故意不传 encoding，验证类自身默认值
    h.addFilter(EventFilter())
    logger = logging.getLogger("tt.trade.encoding_regress")
    logger.handlers = [h]
    logger.setLevel(logging.INFO)
    logger.propagate = False
    try:
        logger.info(json.dumps(
            {"event": "close", "reason": "手动平仓 F"}, ensure_ascii=False))
    finally:
        logger.handlers = []
    h.flush()
    行 = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(行) == 1
    assert 行[0]["reason"] == "手动平仓 F"
