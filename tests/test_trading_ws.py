# -*- coding: utf-8 -*-
"""trading_ws 下发距墙 section。"""
import json

from app.routers.trading_ws import TradingWsHub


def test_snapshot帧带walls段(monkeypatch):
    from app.services import gex_wall_service as svc
    monkeypatch.setattr(svc, "_WALLS", {
        "SPY": {"call_wall": "600", "put_wall": "590", "ts": 1.0, "stale": False}
    })
    monkeypatch.setattr(svc, "_GENERATION", 7)

    hub = TradingWsHub()
    frame = json.loads(hub._snapshot_payload())
    assert frame["walls"]["SPY"]["call_wall"] == "600"
    assert "walls" in frame["revisions"]


def test_generation没变时hub不产生patch(monkeypatch):
    """本条只钉 hub 这一侧：token 相同就不推。

    ⚠️ 「值没动 → generation 就不动」是**服务侧**的责任，由
    `test_gex_wall_service.py::test_值一个没动的一轮不递增generation` 钉。
    本条手工冻住 `_GENERATION`，证明不了那一点 —— 原实现里 `refresh_once` 是
    **无条件** `+1` 的，本条照样绿，而线上每 10 秒就在强推一帧。
    """
    from app.services import gex_wall_service as svc
    monkeypatch.setattr(svc, "_WALLS", {"SPY": {"call_wall": "600"}})
    monkeypatch.setattr(svc, "_GENERATION", 1)

    hub = TradingWsHub()
    hub._advance()                      # 首轮建立基线
    before = hub._revisions["walls"]
    hub._advance()                      # generation 没变
    assert hub._revisions["walls"] == before


def test_墙位变了产生patch(monkeypatch):
    from app.services import gex_wall_service as svc
    monkeypatch.setattr(svc, "_WALLS", {"SPY": {"call_wall": "600"}})
    monkeypatch.setattr(svc, "_GENERATION", 1)

    hub = TradingWsHub()
    hub._advance()
    before = hub._revisions["walls"]

    monkeypatch.setattr(svc, "_WALLS", {"SPY": {"call_wall": "610"}})
    monkeypatch.setattr(svc, "_GENERATION", 2)
    hub._advance()
    assert hub._revisions["walls"] == before + 1


# ── 0DTE 资金潮徽章 section（2026-08-20 加）────────────────────────────────────

def _tide_state(cum_call, cum_put, session_date="2026-01-02"):
    """造一个已播种的 TideRestState。

    `session_date` 故意用一个不会等于"今天"的日子 —— `session_label()` 于是恒定
    返回 'prev'，测试不随运行当天的美东时间飘。
    """
    from app.services.market_tide_service import TideRestState
    st = TideRestState()
    st.cum_call, st.cum_put, st.seeded, st.session_date = cum_call, cum_put, True, session_date
    return st


def test_snapshot帧带tide段(monkeypatch):
    from app.services import market_tide_service as svc
    monkeypatch.setattr(svc, "_ACTIVE_ACC", _tide_state(9_000_000.0, 5_000_000.0))

    hub = TradingWsHub()
    frame = json.loads(hub._snapshot_payload())
    assert frame["tide"]["diff"] == 4_000_000
    assert frame["tide"]["direction"] == "bullish"
    assert frame["tide"]["label"] == "看多 +$4.0M"
    assert frame["tide"]["session"] == "prev"
    assert "tide" in frame["revisions"]
    # 顶栏徽章不画走势图，390 点的数组不该出现在每一帧里
    assert "series" not in frame["tide"]


def test_tide只有ts在变时不产生patch(monkeypatch):
    """**本条是 tide section 的命门。**

    `tide_snapshot()` 每次都带一个新的 `ts`（当前时刻）。若 hub 拿整个 dict 当
    token，两次 `_advance()` 之间只隔几微秒也会判定"变了"，于是每 0.5 秒推一帧
    tide patch —— 而这个值每分钟才真的动一次。token 必须只挑
    (diff, direction, seeded, session) 四个字段。
    """
    from app.services import market_tide_service as svc
    monkeypatch.setattr(svc, "_ACTIVE_ACC", _tide_state(9_000_000.0, 5_000_000.0))

    hub = TradingWsHub()
    hub._advance()                      # 首轮建立基线
    before = hub._revisions["tide"]
    hub._advance()                      # 净额没动，只有 ts 变了
    assert hub._revisions["tide"] == before


def test_tide净额变了产生patch(monkeypatch):
    from app.services import market_tide_service as svc
    monkeypatch.setattr(svc, "_ACTIVE_ACC", _tide_state(9_000_000.0, 5_000_000.0))

    hub = TradingWsHub()
    hub._advance()
    before = hub._revisions["tide"]

    monkeypatch.setattr(svc, "_ACTIVE_ACC", _tide_state(9_000_000.0, 12_000_000.0))
    hub._advance()
    assert hub._revisions["tide"] == before + 1


def test_tide服务未启动时下发空徽章而不是崩(monkeypatch):
    """`market_tide_refresh_loop` 没跑（配置关闭 / 启动初期）时 `current_accumulator()`
    返回 None。交易页的 /ws 是下单链路的状态通道，绝不能被一个纯展示徽章带崩。
    """
    from app.services import market_tide_service as svc
    monkeypatch.setattr(svc, "_ACTIVE_ACC", None)

    hub = TradingWsHub()
    frame = json.loads(hub._snapshot_payload())
    assert frame["tide"]["diff"] is None
    assert frame["tide"]["label"] == "--"
    assert frame["tide"]["session"] == "empty"
    assert frame["tide"]["seeded"] is False
