"""[Deprecated] 4in1_watcher 跨进程客户端 —— 整合后已无生产消费者。

2026-07-22：4in1_watcher 已整合进 tt_trading 作为 app/watcher/ 子包，
交易系统通过 GLOBAL_WATCHER 内存直连，不再需要跨进程 REST/WS。
本文件仅 scripts/market_open_probe.py 探针脚本还在使用，待该脚本改用
进程内 GLOBAL_WATCHER 后可安全删除。

原始职责：跨进程 REST/WS 客户端，断线指数退避重连。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.request
from decimal import Decimal
from typing import Callable

import websockets

from app.config import WATCHER_BASE_URL, WATCHER_SIGNAL_TIMEOUT_S, WATCHER_WS_URL
from app.market_state import Bar

from app.core.dailylogger import get_logger

_log = get_logger("tt.watcher")


class WatcherClient:
    def __init__(self) -> None:
        self._base = WATCHER_BASE_URL.rstrip("/")
        self._ws_url = WATCHER_WS_URL
        self._timeout = float(WATCHER_SIGNAL_TIMEOUT_S)
        self._http_timeout = 4.0
        self._clock: Callable[[], float] = time.monotonic
        # 共享状态（GIL 下整值读写足够；WS 任务写、数据源线程读）
        self.states: dict[str, dict] = {}
        self.last_signal_ts: dict[str, float] = {}
        self._ws_connected = False
        self._running = False
        self._last_msg_ts = 0.0
        self._stop = False
        self._ws = None  # 当前活跃连接（供 stop() 主动关闭）

    # ── 纯逻辑：消息分发 ──────────────────────────────────────────────
    def _handle_message(self, msg: dict) -> None:
        self._last_msg_ts = self._clock()
        t = msg.get("type")
        if t == "update":
            tk = msg.get("ticker")
            data = msg.get("data") or {}
            if tk:
                self.states[tk] = data
                if "running" in data:
                    self._running = bool(data["running"])
        elif t in ("signal", "signal_event"):
            tk = msg.get("ticker")
            if tk:
                self.last_signal_ts[tk] = self._clock()
        # pong / position_alert / event_log：仅刷新存活时间戳（已在上方更新）

    @property
    def signal_online(self) -> bool:
        if not self._ws_connected or not self._running:
            return False
        return (self._clock() - self._last_msg_ts) <= self._timeout

    # ── REST（stdlib urllib；路径带 /api 前缀）──────────────────────────
    def _get_json(self, path: str) -> dict:
        req = urllib.request.Request(self._base + path, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=self._http_timeout) as r:  # noqa: S310 localhost
            return json.loads(r.read().decode("utf-8"))

    def get_watchlist(self) -> list[str]:
        try:
            return list(self._get_json("/api/watchlist").get("tickers", []))
        except Exception:  # noqa: BLE001 —watcher 不可达时空票池
            return []

    def get_ticker_state(self, ticker: str) -> dict | None:
        try:
            return self._get_json(f"/api/state/{ticker.upper()}")
        except Exception:  # noqa: BLE001
            return None

    def get_watcher_status(self) -> bool:
        try:
            return bool(self._get_json("/api/watcher/status").get("running", False))
        except Exception:  # noqa: BLE001
            return False

    def get_history_bars(self, ticker: str, interval: str = "5m") -> list[Bar]:
        data = self._get_json(f"/api/history/{ticker.upper()}?interval={interval}")
        out: list[Bar] = []
        for b in data.get("bars", []):
            out.append(Bar(
                int(b["time"]),
                Decimal(str(b["open"])), Decimal(str(b["high"])),
                Decimal(str(b["low"])), Decimal(str(b["close"])),
                Decimal(str(b["volume"])),
            ))
        return out

    @staticmethod
    def _next_backoff(cur: float) -> float:
        return min(cur * 2, 10.0)

    def stop(self) -> None:
        self._stop = True
        ws = self._ws
        if ws is not None:
            # 从其它任务请求关闭当前连接，打断空闲状态下阻塞的 async for
            try:
                asyncio.get_running_loop().create_task(ws.close())
            except RuntimeError:
                pass  # 无运行中的事件循环时忽略

    async def _ping_loop(self, ws) -> None:
        """每 5s 发应用层 ping；watcher 回 pong 刷新存活时间戳（冷清时段也能判活）。"""
        try:
            while True:
                await asyncio.sleep(5.0)
                await ws.send(json.dumps({"type": "ping"}))
        except Exception:  # noqa: BLE001 —连接关闭时静默退出
            return

    async def run_forever(self) -> None:
        """后台任务：连 watcher WS，收信号；断线指数退避重连（上限 10s）。"""
        backoff = 1.0
        while not self._stop:
            try:
                async with websockets.connect(self._ws_url, open_timeout=5) as ws:
                    self._ws = ws
                    self._ws_connected = True
                    self._last_msg_ts = self._clock()
                    self._running = await asyncio.to_thread(self.get_watcher_status)
                    backoff = 1.0
                    ping = asyncio.create_task(self._ping_loop(ws))
                    try:
                        async for raw in ws:
                            try:
                                self._handle_message(json.loads(raw))
                            except json.JSONDecodeError:
                                pass
                    finally:
                        ping.cancel()
            except asyncio.CancelledError:
                break
            except Exception as e:  # noqa: BLE001 —连接失败/断开 → 退避重连
                # 曾静默 pass：watcher 停机时日志零输出，运维无法区分「watcher 挂了」与
                # 「当前没信号」（2026-07-20 盘中排障踩坑）。改为记一条，含下次退避秒数。
                _log.warning(json.dumps({"event": "watcher_ws_down", "url": self._ws_url,
                                         "error": f"{type(e).__name__}: {e}", "retry_in_s": backoff},
                                        ensure_ascii=False))
            finally:
                self._ws_connected = False
                self._ws = None
            if self._stop:
                break
            await asyncio.sleep(backoff)
            backoff = self._next_backoff(backoff)


watcher_client = WatcherClient()
