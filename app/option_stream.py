"""⑥ watchdog 期权报价实时流 —— Alpaca options WebSocket（indicative / 签 OPRA 后换 feed）。

0DTE 权利金 3 秒内变化可观（用户决策 2026-07-17）：watchdog 报价从 REST 轮询升级为
WS 实时缓存 + REST 兜底。协议：msgpack 二进制帧；客户端发 {"action":"auth",...} /
{"action":"subscribe","quotes":[...]}；服务端回消息数组，报价帧
{"T":"q","S":合约代码,"bp":bid,"ap":ask,...}。

订阅集 = 本系统登记的活跃仓合约（watchdog 每拍 set_symbols() 声明）；差异由
reconciler 任务对连接发 subscribe/unsubscribe。无期望订阅时不建连（夜间不空挂）。
一切异常 → 断线指数退避重连（上限 10s）；WS 不可用时 quote() 只回 None，
watchdog 自动落回 REST —— 止损永不因 WS 故障而失明（降级哲学与 ③/7b/TP 一致）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from dataclasses import dataclass
from decimal import Decimal

import msgpack

from app.config import OPTION_STREAM_URL, OPTION_WS_SYMBOL_BUDGET

from app.core.dailylogger import get_logger

logger = get_logger("tt.option_stream")

_RECONCILE_INTERVAL_S = 1.0
_IDLE_POLL_S = 1.0
_BACKOFF_MAX_S = 10.0
_PING_INTERVAL_S = 1.0
_PING_TIMEOUT_S = 1.0

PRIORITY_POSITION = 1000
PRIORITY_CANDIDATE = 500
PRIORITY_PREVIEW = 400
PRIORITY_RECORDING = 100

# 给**真实委托定价**时可接受的 WS 盘口年龄。本类的 staleness_s=5.0 是 watchdog 估值口径，
# 拿来定价太旧 —— 0DTE 上 1 秒就够 ask 抬一档。入场链路（选约复用 + 提交价）共用此值，
# 两处必须同口径：若提交价肯用的报价比选约复用要求的更旧，就会出现「用新鲜价过闸、
# 拿陈旧价下单」的错配。
ENTRY_QUOTE_MAX_AGE_S = 1.0


class OptionSubscriptionBudgetError(ValueError):
    """严格订阅 scope 无法在硬预算内完整容纳。"""


@dataclass(frozen=True)
class _SubscriptionScope:
    symbols: frozenset[str]
    priority: int


class OptionQuoteStream:
    """期权报价 WS 客户端 + 新鲜度缓存（单例见模块尾 OPTION_STREAM）。

    线程模型：run_forever 在事件循环跑；set_symbols/quote 供 watchdog 工作线程调用
    （只做原子的 dict/set 引用读写，GIL 下安全）。
    """

    def __init__(
        self,
        url: str | None = None,
        staleness_s: float = 5.0,
        symbol_budget: int = OPTION_WS_SYMBOL_BUDGET,
    ):
        from app.integrations.alpaca.client import option_stream_endpoint_override

        self._url = url or option_stream_endpoint_override() or OPTION_STREAM_URL
        self._staleness = staleness_s
        if symbol_budget <= 0:
            raise ValueError("Option WS symbol budget 必须大于 0")
        self._symbol_budget = int(symbol_budget)
        self._scope_lock = threading.RLock()
        self._scopes: dict[str, _SubscriptionScope] = {}
        self._desired: set[str] = set()
        self._dropped: set[str] = set()
        self._confirmed_symbols: set[str] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._reconcile_event: asyncio.Event | None = None
        self._activity: asyncio.Event | None = None
        self._cache: dict[str, tuple[Decimal, Decimal, float]] = {}
        self._cache_origins: dict[str, str] = {}
        self.connected = False
        self._connected_at: float | None = None
        self.last_server_error: dict | None = None
        self._metrics = {
            "connections": 0,
            "subscription_acks": 0,
            "subscribe_frames": 0,
            "unsubscribe_frames": 0,
            "server_errors": {},
            "last_ack_latency_ms": None,
        }
        self._last_control_sent_at: float | None = None
        # 录制（供 scripts/record_option_quotes.py 远程开关；见 start_recording）：
        # 复用本连接而不是另开一条 —— Alpaca 期权 WS 每账号只允许一条并发连接，
        # 2026-07-17 事故：独立脚本自己开连接，把这条盯盘用的连接顶掉了。
        self._record_symbols: set[str] = set()
        self._record_file = None
        self.record_count = 0
        self._record_path: str | None = None

    # ── scope 订阅账本 ───────────────────────────────────────────
    @property
    def _want(self) -> set[str]:
        """兼容旧测试/调用方；新代码使用 ``desired_symbols``。"""
        return self.desired_symbols

    @property
    def desired_symbols(self) -> set[str]:
        with self._scope_lock:
            return set(self._desired)

    @property
    def confirmed_symbols(self) -> set[str]:
        with self._scope_lock:
            return set(self._confirmed_symbols)

    @property
    def dropped_symbols(self) -> set[str]:
        with self._scope_lock:
            return set(self._dropped)

    @property
    def symbol_budget(self) -> int:
        return self._symbol_budget

    def scope_symbols(self, owner: str) -> set[str]:
        with self._scope_lock:
            scope = self._scopes.get(owner)
            return set(scope.symbols) if scope else set()

    def _select_desired(
        self, scopes: dict[str, _SubscriptionScope]
    ) -> tuple[set[str], set[str]]:
        """按优先级构造唯一合约并集；持仓即使超过预算也绝不淘汰。"""
        desired: set[str] = set()
        ordered = sorted(scopes.items(), key=lambda item: (-item[1].priority, item[0]))
        for _owner, scope in ordered:
            for symbol in sorted(scope.symbols):
                if symbol in desired:
                    continue
                if scope.priority >= PRIORITY_POSITION or len(desired) < self._symbol_budget:
                    desired.add(symbol)
        all_symbols = {symbol for scope in scopes.values() for symbol in scope.symbols}
        return desired, all_symbols - desired

    def replace_scope(
        self,
        owner: str,
        symbols: set[str],
        priority: int = PRIORITY_CANDIDATE,
        *,
        strict: bool = False,
    ) -> set[str]:
        """原子替换 owner 的订阅声明，并返回实际进入 desired 的该 scope 合约。

        低优先级 scope 超预算时按确定性顺序裁剪；``strict=True`` 则整组拒绝，适用于
        人工录制，避免界面声称已录制但实际只有部分合约。
        """
        owner = owner.strip()
        if not owner:
            raise ValueError("订阅 scope owner 不能为空")
        clean = {str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()}
        new_scope = _SubscriptionScope(frozenset(clean), int(priority))
        with self._scope_lock:
            current = self._scopes.get(owner)
            if current == new_scope:
                return set(clean) & self._desired
            candidate = dict(self._scopes)
            if clean:
                candidate[owner] = new_scope
            else:
                candidate.pop(owner, None)
            desired, dropped = self._select_desired(candidate)
            owner_dropped = clean - desired
            if strict and owner_dropped:
                raise OptionSubscriptionBudgetError(
                    f"Option WS 合约预算不足：请求 {len(clean)}，"
                    f"当前上限 {self._symbol_budget}，未容纳 {len(owner_dropped)}"
                )
            self._scopes = candidate
            self._desired = desired
            self._dropped = dropped
        self._wake_reconciler()
        return clean & desired

    def release_scope(self, owner: str) -> None:
        with self._scope_lock:
            if owner not in self._scopes:
                return
            candidate = dict(self._scopes)
            candidate.pop(owner, None)
            self._scopes = candidate
            self._desired, self._dropped = self._select_desired(candidate)
        self._wake_reconciler()

    def subscription_snapshot(self) -> dict:
        with self._scope_lock:
            return {
                "connected": self.connected,
                "budget": self._symbol_budget,
                "desired": sorted(self._desired),
                "confirmed": sorted(self._confirmed_symbols),
                "dropped": sorted(self._dropped),
                "scopes": {
                    owner: {
                        "priority": scope.priority,
                        "count": len(scope.symbols),
                    }
                    for owner, scope in sorted(self._scopes.items())
                },
                "metrics": {
                    **self._metrics,
                    "server_errors": dict(self._metrics["server_errors"]),
                },
            }

    def _bind_loop(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._reconcile_event = asyncio.Event()
        self._reconcile_event.set()
        self._activity = asyncio.Event()

    def _wake_reconciler(self) -> None:
        loop, event = self._loop, self._reconcile_event
        if loop is None or event is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(event.set)

    def _wake_activity(self) -> None:
        """只唤醒止损巡检；与订阅对账事件分离，避免控制帧触发风控拍。"""
        loop, event = self._loop, self._activity
        if loop is None or event is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(event.set)

    async def wait_activity(self, timeout_s: float = 1.0) -> bool:
        """等待活跃持仓的新期权报价；无事件对象时退化为定时拍。"""
        event = self._activity
        if event is None:
            await asyncio.sleep(timeout_s)
            return False
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout_s)
            return True
        except asyncio.TimeoutError:
            return False

    def clear_activity(self) -> None:
        if self._activity is not None:
            self._activity.clear()

    def _begin_connection(self) -> None:
        """建立新的 WS 缓存代次；旧连接报价不得供 SOS 开仓复用。"""
        with self._scope_lock:
            self._confirmed_symbols = set()
            self._cache.clear()
            self._cache_origins.clear()
            self._metrics["connections"] += 1
            self.connected = True
            self._connected_at = time.monotonic()

    def _end_connection(self) -> None:
        """断线后立即让纯 WS 开仓读取 fail-closed。"""
        with self._scope_lock:
            self.connected = False
            self._connected_at = None
            self._confirmed_symbols = set()
            self._cache.clear()
            self._cache_origins.clear()

    # ── 供 watchdog 调用 ─────────────────────────────────────────
    def set_symbols(self, codes: set[str]) -> None:
        """兼容入口：声明活跃持仓 scope；非可执行档传空集。"""
        self.replace_scope("positions", set(codes), PRIORITY_POSITION)

    def quote_with_age(
        self,
        code: str,
        *,
        max_age_s: float | None = None,
        require_confirmed: bool = False,
        require_origin: str | None = None,
    ) -> tuple[Decimal, Decimal, float] | None:
        """返回最新 ``(bid, ask, age_s)``，并支持 SOS 的纯 WS 开仓约束。"""
        code = str(code or "").strip().upper()
        with self._scope_lock:
            if require_confirmed and (
                not self.connected
                or code not in self._desired
                or code not in self._confirmed_symbols
            ):
                return None
            if require_origin is not None and self._cache_origins.get(code) != require_origin:
                return None
            hit = self._cache.get(code)
            if hit is None:
                return None
        bid, ask, ts = hit
        age_s = max(0.0, time.monotonic() - ts)
        allowed_age_s = self._staleness
        if max_age_s is not None:
            allowed_age_s = min(allowed_age_s, max(0.0, float(max_age_s)))
        if age_s > allowed_age_s:
            return None
        return bid, ask, age_s

    def quote(self, code: str) -> tuple[Decimal, Decimal] | None:
        """最新 (bid, ask)，仅当缓存新鲜（≤staleness_s）；否则 None。"""
        quote = self.quote_with_age(code)
        if quote is None:
            return None
        bid, ask, _age_s = quote
        return bid, ask

    def entry_quote_status(self, code: str) -> dict:
        """SOS 开仓等待失败时的只读 WS 状态，不触发 REST。"""
        code = str(code or "").strip().upper()
        now = time.monotonic()
        with self._scope_lock:
            hit = self._cache.get(code)
            return {
                "connected": self.connected,
                "desired": code in self._desired,
                "confirmed": code in self._confirmed_symbols,
                "cache_present": hit is not None,
                "quote_origin": self._cache_origins.get(code),
                "quote_age_ms": (
                    round(max(0.0, now - hit[2]) * 1000, 3)
                    if hit is not None
                    else None
                ),
                "connected_age_ms": (
                    round(max(0.0, now - self._connected_at) * 1000, 3)
                    if self.connected and self._connected_at is not None
                    else None
                ),
            }

    # ── 录制（与 watchdog 订阅共用同一条连接）──────────────────────
    @property
    def is_recording(self) -> bool:
        return self._record_file is not None

    def start_recording(self, symbols: set[str], path) -> None:
        """把 symbols 并入订阅集，并把之后收到的 quote 帧追加写到 path（JSONL）。"""
        clean = {str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()}
        self.replace_scope("recording", clean, PRIORITY_RECORDING, strict=True)
        path = str(path)
        import pathlib
        pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
        try:
            new_file = open(path, "a", encoding="utf-8")  # noqa: SIM115
        except Exception:
            self.release_scope("recording")
            raise
        if self._record_file is not None:
            self._record_file.close()
        self._record_file = new_file
        self._record_path = path
        self.record_count = 0
        self._record_symbols = clean

    def stop_recording(self) -> None:
        self._record_symbols = set()
        self.release_scope("recording")
        if self._record_file is not None:
            self._record_file.close()
            self._record_file = None

    def _effective_want(self) -> set[str]:
        return self.desired_symbols

    # ── 纯逻辑（可测） ────────────────────────────────────────────
    def handle_messages(self, msgs: list[dict]) -> None:
        """服务端消息数组 → 更新报价缓存；非报价帧/残帧忽略（error 帧记日志）。"""
        position_quote_seen = False
        for m in msgs:
            if not isinstance(m, dict):
                continue
            t = m.get("T")
            if t == "q":
                code = str(m.get("S") or "").strip().upper()
                bp, ap = m.get("bp"), m.get("ap")
                if not code or bp is None or ap is None:
                    continue
                with self._scope_lock:
                    if (
                        not self.connected
                        or code not in self._desired
                        or code not in self._confirmed_symbols
                    ):
                        continue
                    self._cache[code] = (
                        Decimal(str(bp)), Decimal(str(ap)), time.monotonic())
                    self._cache_origins[code] = "ws"
                    position_scope = self._scopes.get("positions")
                    if position_scope is not None and code in position_scope.symbols:
                        position_quote_seen = True
                if self._record_file is not None and code in self._record_symbols:
                    rec = {"ts": time.time(), "symbol": code, "bid": bp, "bid_size": m.get("bs"),
                           "ask": ap, "ask_size": m.get("as")}
                    self._record_file.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    self.record_count += 1
                    if self.record_count % 200 == 0:
                        self._record_file.flush()
            elif t == "error":
                self.last_server_error = dict(m)
                code = str(m.get("code") or "unknown")
                errors = self._metrics["server_errors"]
                errors[code] = errors.get(code, 0) + 1
                logger.warning(json.dumps({"event": "option_stream_server_error", "msg": m.get("msg")},
                                          ensure_ascii=False))
            elif t == "subscription":
                quotes = m.get("quotes")
                if not isinstance(quotes, list):
                    continue
                with self._scope_lock:
                    previous = self._confirmed_symbols
                    confirmed = {
                        str(symbol).strip().upper()
                        for symbol in quotes
                        if str(symbol).strip()
                    }
                    for symbol in previous ^ confirmed:
                        self._cache.pop(symbol, None)
                        self._cache_origins.pop(symbol, None)
                    self._confirmed_symbols = confirmed
                    self._metrics["subscription_acks"] += 1
                    if self._last_control_sent_at is not None:
                        self._metrics["last_ack_latency_ms"] = round(
                            (time.monotonic() - self._last_control_sent_at) * 1000,
                            3,
                        )
                self._wake_reconciler()
        if position_quote_seen:
            self._wake_activity()

    @staticmethod
    def sub_frames(want: set[str], subscribed: set[str]) -> list[dict]:
        """订阅集差异 → 待发送动作帧（纯函数）。"""
        frames: list[dict] = []
        add = sorted(want - subscribed)
        rm = sorted(subscribed - want)
        if add:
            frames.append({"action": "subscribe", "quotes": add})
        if rm:
            frames.append({"action": "unsubscribe", "quotes": rm})
        return frames

    # ── 连接生命周期 ──────────────────────────────────────────────
    async def run_forever(self) -> None:
        """lifespan 后台任务：按需建连 → 认证 → 收帧/差异订阅 → 断线退避重连。"""
        import websockets

        from app.integrations.alpaca.client import (
            alpaca_proxy_url,
            is_loopback_url,
            read_keys as _read_keys,
        )

        self._bind_loop()
        backoff = 1.0
        while True:
            if not self._effective_want():
                await asyncio.sleep(_IDLE_POLL_S)
                continue
            try:
                key, sec = _read_keys()
                proxy = None if is_loopback_url(self._url) else alpaca_proxy_url()
                async with websockets.connect(
                    self._url,
                    max_size=2 ** 20,
                    proxy=proxy,
                    ping_interval=_PING_INTERVAL_S,
                    ping_timeout=_PING_TIMEOUT_S,
                    close_timeout=1.0,
                ) as ws:
                    await ws.send(msgpack.packb({"action": "auth", "key": key, "secret": sec}))
                    self._begin_connection()
                    backoff = 1.0
                    logger.info(json.dumps({
                        "event": "option_stream_connected",
                        "url": self._url,
                        "proxy_mode": "direct" if proxy is None else "socks5h",
                    }, ensure_ascii=False))
                    self._wake_reconciler()
                    recon = asyncio.create_task(self._reconciler(ws))
                    try:
                        async for raw in ws:
                            self.handle_messages(msgpack.unpackb(raw, timestamp=3))
                    finally:
                        recon.cancel()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — 一切故障走重连；watchdog 已有 REST 兜底
                logger.warning(json.dumps({"event": "option_stream_error", "error": str(e)[:200]},
                                          ensure_ascii=False))
            finally:
                self._end_connection()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _BACKOFF_MAX_S)

    async def _reconciler(self, ws) -> None:
        """事件驱动对齐 desired/confirmed；安全超时后重发未确认差异。"""
        if self._reconcile_event is None:
            self._bind_loop()
        assert self._reconcile_event is not None
        while True:
            self._reconcile_event.clear()
            want = self._effective_want()
            confirmed = self.confirmed_symbols
            for frame in self.sub_frames(want, confirmed):
                await ws.send(msgpack.packb(frame))
                action = str(frame.get("action") or "")
                metric_key = f"{action}_frames"
                if metric_key in self._metrics:
                    self._metrics[metric_key] += 1
                self._last_control_sent_at = time.monotonic()
            try:
                await asyncio.wait_for(
                    self._reconcile_event.wait(),
                    timeout=_RECONCILE_INTERVAL_S,
                )
            except TimeoutError:
                pass


def atm_chain_symbols(alpaca, tickers: tuple[str, ...] = ("QQQ", "IWM", "SPY"),
                      pad: float = 0.02) -> set[str]:
    """今日 0DTE 链里、现价 ±pad 范围内的全部合约代码（CALL+PUT）。供录制用。

    pad 默认 ±2% 不是随便定的：实测本账号 OptionDataStream/期权 WS 订阅超过
    ~200 张合约会被拒（error 405 / connection limit exceeded），三标的 ±3%
    （约210~230张）就稳定触发，±2%（约130~140张）稳定能订上。调大前务必先用
    少量标的/短时间实测。
    """
    from alpaca.data.requests import OptionChainRequest

    clock = alpaca.clock()
    out: set[str] = set()
    for t in tickers:
        bars = alpaca.rth_bars(t)
        if not bars:
            continue
        price = float(bars[-1].close)
        try:
            req = OptionChainRequest(
                underlying_symbol=t, expiration_date=clock.session_date,
                strike_price_gte=price * (1 - pad), strike_price_lte=price * (1 + pad),
            )
            chain = alpaca._oc().get_option_chain(req)  # noqa: SLF001
        except Exception as e:  # noqa: BLE001 — 单标的取链失败不影响其它标的
            logger.warning(json.dumps({"event": "atm_chain_error", "ticker": t, "error": str(e)[:200]},
                                      ensure_ascii=False))
            continue
        out.update(chain.keys())
    return out


OPTION_STREAM = OptionQuoteStream()


def fresh_entry_quote(code: str) -> tuple[Decimal, Decimal] | None:
    """入场链路定价用的 WS 盘口：当前连接已 ACK 订阅 且 年龄 ≤ ENTRY_QUOTE_MAX_AGE_S。

    `option_candidates` 协调器已把每个 (ticker, direction) 的选中合约推进 scope
    （`option_candidates.py:179 / :284`，PRIORITY_CANDIDATE），命中即 0 网络往返。

    **拿不到一律返回 None，调用方回落 REST —— 绝不因此拒单。** 2026-08-10 那次把这层
    做成 fail-closed（没有已确认 WS 盘口就拒单）当天打出 9 笔 trade_order_rejected 并被
    整体回退（docs/reports/2026-08-11-下单卡顿与拦截诊断.md §A）：点击路径的合约与协调器
    15s 刷一次的选中合约不保证同一个 OCC，换档瞬间必然没有 confirmed 报价。

    选约复用、提交定价、卡片预览三处共用本函数，口径必须一致：若某一处肯用更旧的报价，
    就会出现「用新鲜价过闸、拿陈旧价下单」的错配。
    """
    hit = OPTION_STREAM.quote_with_age(
        code, max_age_s=ENTRY_QUOTE_MAX_AGE_S, require_confirmed=True)
    if hit is None:
        return None
    bid, ask, _age_s = hit
    return bid, ask
