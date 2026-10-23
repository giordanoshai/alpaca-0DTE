# -*- coding: utf-8 -*-
"""期权链取数的分解埋点：慢的时候要能一眼看出慢在哪一段。

背景（2026-08-14 复盘 docs/reports/2026-08-13-夜盘交易复盘.md）：
`contract_ms` 是点击→提交路径上最大的一段，但它是**一个数**。08-13 夜盘那三个
3.5s 的样本，只能靠「日志静默 3.5 秒」反推是单次调用卡住，无法区分到底是
  - `nearest_expiry`（另一发 REST，300s 缓存，冷启动时要付）
  - `_CHAIN_WIDTHS` 逐档扩宽（最多 4 个**串行**往返）
  - 单次 `get_option_chain` 本身慢（SDK 重试 sleep / 网络）
  - `_call_with_rate_limit_retry` 的 429 退避

拆成 expiry_ms / chain_ms / chain_calls / other_ms 之后，这四种情形各自有唯一签名。
`other_ms = total - expiry - chain` 专门用来抓退避 sleep —— sleep 不产生任何日志行，
是上一轮排查里最难看见的那一段。

**只在慢的时候记**：`contract_preview` 是前端每张卡片持续调用的背景负载，
走的是同一条 `contract_candidates`。无阈值全量记录会把 jsonl 淹掉，
反而让真正的慢样本更难找。
"""
from __future__ import annotations

import datetime as dt
import json
import time
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from app.integrations.alpaca import data as data_mod
from app.integrations.alpaca.data import AlpacaData

_SLOW_S = 0.06
_SLOW_MS_FLOOR = 50      # 给调度抖动留余量
_FAST_MS_CEILING = 40


def _snap(*, bid="1.00", ask="1.02", delta="0.42"):
    return SimpleNamespace(
        latest_quote=SimpleNamespace(bid_price=D(bid), ask_price=D(ask)),
        greeks=SimpleNamespace(delta=None if delta is None else D(delta)),
    )


class FakeOptionClient:
    def __init__(self, chains, *, per_call_s=0.0):
        self.chains = list(chains)
        self.requests = []
        self.per_call_s = per_call_s

    def get_option_chain(self, request):
        self.requests.append(request)
        if self.per_call_s:
            time.sleep(self.per_call_s)
        index = min(len(self.requests) - 1, len(self.chains) - 1)
        return self.chains[index]


@pytest.fixture()
def captured(monkeypatch) -> list[dict]:
    events: list[dict] = []
    monkeypatch.setattr(
        data_mod, "logger",
        SimpleNamespace(info=lambda p: events.append(json.loads(p)),
                        warning=lambda *_a, **_k: None),
    )
    return events


def _data(monkeypatch, chains, *, per_call_s=0.0, expiry_s=0.0):
    data = AlpacaData(cache_ttl=20)
    client = FakeOptionClient(chains, per_call_s=per_call_s)
    monkeypatch.setattr(data, "_oc", lambda: client)

    def _expiry(_ticker):
        if expiry_s:
            time.sleep(expiry_s)
        return dt.date(2026, 7, 27)

    monkeypatch.setattr(data, "nearest_expiry", _expiry)
    return data, client


def _fetch(data, **overrides):
    kwargs = dict(
        ticker="QQQ", direction="long", price=D("500"),
        min_delta=D("0.40"), max_spread_pct=D("0.03"),
        max_contract_cost=D("600"), template_version="7",
    )
    kwargs.update(overrides)
    return data.contract_candidates(**kwargs)


def _slow(events):
    hits = [e for e in events if e.get("event") == "chain_fetch_slow"]
    assert len(hits) == 1, f"应恰好吐一条 chain_fetch_slow，实得 {len(hits)}"
    return hits[0]


def test_慢查吐出分解埋点(monkeypatch, captured):
    monkeypatch.setattr(data_mod, "_CHAIN_SLOW_LOG_MS", _SLOW_MS_FLOOR)
    data, _ = _data(monkeypatch, [{"QQQ260727C00500000": _snap()}], per_call_s=_SLOW_S)

    _fetch(data)

    slow = _slow(captured)
    assert slow["ticker"] == "QQQ"
    assert slow["direction"] == "long"
    assert slow["chain_ms"] >= _SLOW_MS_FLOOR
    assert slow["chain_calls"] == 1


def test_快查不吐埋点(monkeypatch, captured):
    """卡片预览走同一条路径，是持续背景负载 —— 无阈值会把 jsonl 淹掉。"""
    data, _ = _data(monkeypatch, [{"QQQ260727C00500000": _snap()}])

    _fetch(data)

    assert [e for e in captured if e.get("event") == "chain_fetch_slow"] == []


def test_默认阈值足够高不会淹没日志():
    """阈值必须高于**当前基线的常态**，否则记的是常态不是异常。

    08-14 当天两次调整，门柱都是被实测推着走的：
      500ms  → 太低（`COIN chain_ms=527.9 chain_calls=1` 只是一次正常代理往返）
      2500ms → 当时中位 330ms、目标 3.5s，落在两者之间
      8000ms → 恢复 SDK 重试后，**3.5s 变成了中位数**。22:58 重启后 14 分钟 330 条：
               中位 3665ms、p90 6834ms、最大 14020ms；2500ms 会打出 23.6 条/分钟，
               8000ms 只留多次重试的病态样本（0.8 条/分钟）。

    下界跟着基线走：低于 p90（~6.8s）就会把「一次 429 重试」这种常态当异常记。
    """
    assert data_mod._CHAIN_SLOW_LOG_MS >= 7000, "低于当前 p90 会被常态 429 重试刷屏"
    assert data_mod._CHAIN_SLOW_LOG_MS <= 12000, "高于 12s 只剩极端样本，失去预警价值"


# ── 节流：项目的 RepeatSuppressFilter 只管 WARNING 以上，INFO 得自己节流 ──────
@pytest.fixture()
def clock(monkeypatch):
    now = {"t": 1000.0}
    monkeypatch.setattr(data_mod.time, "monotonic", lambda: now["t"])
    return now


def _slow_data(monkeypatch):
    monkeypatch.setattr(data_mod, "_CHAIN_SLOW_LOG_MS", _SLOW_MS_FLOOR)
    return _data(monkeypatch, [{"QQQ260727C00500000": _snap()}], per_call_s=_SLOW_S)[0]


def test_同一标的方向在节流窗口内只记一条(monkeypatch, captured, clock):
    data = _slow_data(monkeypatch)

    for price in ("500", "501", "502"):     # 变价 → cache miss → 每次都真取数
        _fetch(data, price=D(price))

    assert len(captured) == 1, "窗口内应只留首条"


def test_节流期间被吞掉的条数带在下一条上(monkeypatch, captured, clock):
    """静默丢弃会让人误以为「只慢了一次」——被吞掉多少必须说出来。"""
    data = _slow_data(monkeypatch)

    _fetch(data, price=D("500"))
    _fetch(data, price=D("501"))
    _fetch(data, price=D("502"))
    clock["t"] += data_mod._CHAIN_SLOW_LOG_THROTTLE_S + 1
    _fetch(data, price=D("503"))

    assert len(captured) == 2
    assert captured[0].get("suppressed") is None, "首条没有被吞的前序，不该带这个字段"
    assert captured[1]["suppressed"] == 2


def test_不同标的各自独立节流(monkeypatch, captured, clock):
    """一只票在刷屏不能把另一只票的慢样本盖掉。"""
    data = _slow_data(monkeypatch)

    _fetch(data, ticker="QQQ")
    _fetch(data, ticker="COIN")

    assert {e["ticker"] for e in captured} == {"QQQ", "COIN"}


def test_同标的两个方向各自独立节流(monkeypatch, captured, clock):
    data = _slow_data(monkeypatch)

    _fetch(data, direction="long")
    _fetch(data, direction="short")

    assert {e["direction"] for e in captured} == {"long", "short"}


def test_节流窗口过后重新记录(monkeypatch, captured, clock):
    data = _slow_data(monkeypatch)

    _fetch(data, price=D("500"))
    clock["t"] += data_mod._CHAIN_SLOW_LOG_THROTTLE_S + 1
    _fetch(data, price=D("501"))

    assert len(captured) == 2


def test_节流窗口足够长():
    assert data_mod._CHAIN_SLOW_LOG_THROTTLE_S >= 60, "窗口太短起不到节流作用"


def test_扩宽档数被记录(monkeypatch, captured):
    """chain_calls 是区分「逐档扩宽」与「单次调用慢」的唯一判据。"""
    monkeypatch.setattr(data_mod, "_CHAIN_SLOW_LOG_MS", _SLOW_MS_FLOOR)
    chains = [
        {"QQQ260727P00500000": _snap(delta="-0.30")},   # delta 不够 → 继续扩宽
        {"QQQ260727P00495000": _snap(delta="-0.41")},
    ]
    data, client = _data(monkeypatch, chains, per_call_s=_SLOW_S)

    _fetch(data, direction="short")

    assert len(client.requests) == 2
    assert _slow(captured)["chain_calls"] == 2


def test_取到期日的耗时单独归属(monkeypatch, captured):
    """expiry_ms 记错会把排查引向期权链，而真正慢的是另一发 REST。"""
    monkeypatch.setattr(data_mod, "_CHAIN_SLOW_LOG_MS", _SLOW_MS_FLOOR)
    data, _ = _data(monkeypatch, [{"QQQ260727C00500000": _snap()}], expiry_s=_SLOW_S)

    _fetch(data)

    slow = _slow(captured)
    assert slow["expiry_ms"] >= _SLOW_MS_FLOOR
    assert slow["chain_ms"] < _FAST_MS_CEILING, "取到期日的耗时不该记到期权链头上"


def test_退避sleep落在other_ms(monkeypatch, captured):
    """429 退避 sleep 不产生任何日志行，是上一轮排查里最难看见的一段。"""
    monkeypatch.setattr(data_mod, "_CHAIN_SLOW_LOG_MS", _SLOW_MS_FLOOR)
    data, _ = _data(monkeypatch, [{"QQQ260727C00500000": _snap()}])

    real_cached = data._cached

    def _slow_cached(key, ttl, fn, force=False):
        time.sleep(_SLOW_S)          # 冒充 _call_with_rate_limit_retry 的退避
        return real_cached(key, ttl, fn, force)

    monkeypatch.setattr(data, "_cached", _slow_cached)

    _fetch(data)

    slow = _slow(captured)
    assert slow["other_ms"] >= _SLOW_MS_FLOOR
    assert slow["chain_ms"] < _FAST_MS_CEILING


def test_埋点不改变返回值(monkeypatch, captured):
    monkeypatch.setattr(data_mod, "_CHAIN_SLOW_LOG_MS", _SLOW_MS_FLOOR)
    data, _ = _data(monkeypatch, [{"QQQ260727C00500000": _snap()}], per_call_s=_SLOW_S)

    result = _fetch(data)

    assert [row[1] for row in result] == ["QQQ260727C00500000"]


def test_缓存命中时不再计入链耗时(monkeypatch, captured):
    """第二次同 key 调用没有真的取数，不该被误报成一次慢查。"""
    monkeypatch.setattr(data_mod, "_CHAIN_SLOW_LOG_MS", _SLOW_MS_FLOOR)
    data, client = _data(monkeypatch, [{"QQQ260727C00500000": _snap()}], per_call_s=_SLOW_S)

    _fetch(data)
    captured.clear()
    _fetch(data)

    assert len(client.requests) == 1, "第二次应命中缓存"
    assert [e for e in captured if e.get("event") == "chain_fetch_slow"] == []
