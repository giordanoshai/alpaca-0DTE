"""Watcher 页面 WebSocket 的状态编码与初始快照。"""
import json
from datetime import date, datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers import watcher_ws


def _watcher_state() -> dict:
    return {
        "ticker": "SPY",
        "session_date": date(2026, 8, 3),
        "current_bar": {
            "datetime": datetime(2026, 8, 3, 6, 55),
            "timestamp": 1785754500,
            "close": 751.07,
        },
    }


def test_state_payload_encodes_session_date_without_mutating_source():
    state = _watcher_state()

    payload = watcher_ws._state_payload(state)

    assert payload["session_date"] == "2026-08-03"
    assert "datetime" not in payload["current_bar"]
    assert state["session_date"] == date(2026, 8, 3)
    assert "datetime" in state["current_bar"]
    json.dumps(payload)


def test_watcher_ws_initial_snapshot_accepts_session_date(monkeypatch):
    monkeypatch.setattr(
        watcher_ws.GLOBAL_WATCHER,
        "get_all_states",
        lambda: {"SPY": _watcher_state()},
    )
    app = FastAPI()
    app.include_router(watcher_ws.router)

    with TestClient(app).websocket_connect("/watcher/ws") as ws:
        frame = ws.receive_json()

    assert frame["type"] == "update"
    assert frame["ticker"] == "SPY"
    assert frame["data"]["session_date"] == "2026-08-03"
    assert "datetime" not in frame["data"]["current_bar"]


def test_连线首帧补市场潮汐快照(monkeypatch):
    """徽章平时靠 market_tide_refresh_loop 推送，非盘中它几乎不推（值不变）。

    浏览器若在盘前刷新，不补这一帧就只剩 `--`，累加器里回填好的昨收白拿。
    """
    import app.services.market_tide_service as mts

    acc = mts.TideRestState()
    acc.cum_call = 30_000_000.0
    acc.cum_put = 1_000_000.0
    acc.seeded = True
    acc.session_date = "1970-01-01"          # 与今天不同日 ⇒ prev，与真实时钟无关
    monkeypatch.setattr(mts, "_ACTIVE_ACC", acc)
    monkeypatch.setattr(watcher_ws.GLOBAL_WATCHER, "get_all_states", dict)

    app = FastAPI()
    app.include_router(watcher_ws.router)

    with TestClient(app).websocket_connect("/watcher/ws") as ws:
        frame = ws.receive_json()

    assert frame["type"] == "market_tide"
    assert frame["data"]["session"] == "prev"
    assert frame["data"]["diff"] == 29_000_000.0
    assert frame["data"]["series"] == []


def test_无累加器时不发首帧潮汐(monkeypatch):
    """循环没起来时别推一个 0 值：那和「盘中真中性」在徽章上分不清。"""
    import app.services.market_tide_service as mts

    monkeypatch.setattr(mts, "_ACTIVE_ACC", None)
    monkeypatch.setattr(
        watcher_ws.GLOBAL_WATCHER,
        "get_all_states",
        lambda: {"SPY": _watcher_state()},
    )
    app = FastAPI()
    app.include_router(watcher_ws.router)

    with TestClient(app).websocket_connect("/watcher/ws") as ws:
        frame = ws.receive_json()

    assert frame["type"] == "update"          # 首帧仍是行情快照，没有多余的潮汐帧
