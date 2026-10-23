"""闸门驱动的期权候选协调器。

每票 **CALL/PUT 各维护一份候选**：闸门对某个方向持续放行后才请求该方向的窄链，
REST 排序得到的 Top 3 留在内存，但每票每方向常驻 Option WS 只订阅 Top 1。
主合约换档采用 make-before-break：先把新旧两个放入 scope，确认新合约已订阅后立即释放旧合约。

2026-08-08 趋势分降级第二期：此前是「Watcher 已确定 CALL/PUT 方向、只查那一个方向」，
方向来自趋势分。规则 6 反向硬拦截删除后，买入方向只来自用户实际点击，协调器无从预知，
因而两边都预取、由人挑边。两侧的 WS scope owner 分开（`candidates:{TICKER}:{方向}`），
一侧换档不会连带释放另一侧的订阅。
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from app.integrations.alpaca.data import SelectedContract
from app.services.market_feed import MARKET_FEED, MarketFeedHub
from app.option_stream import (
    OPTION_STREAM,
    PRIORITY_CANDIDATE,
    OptionQuoteStream,
)
from app.position_registry import REGISTRY
from app.services.contract_selection import select_contract_shortlist
from app.services.data_source_registry import current_source
from app.templates import get_store

_ALLOW_STABLE_S = 1.0
_SOFT_RELEASE_S = 3.0
_IDENTITY_TTL_S = 15.0
_IDENTITY_TTL_NO_GREEKS_S = 120.0
_PRICE_MOVE_RATIO = Decimal("0.005")
_LOOP_INTERVAL_S = 0.1


def identity_ttl_s() -> float:
    """身份保鲜期，按 option feed 分档。

    这个定时器只买两样东西：**盘口新鲜度**（bid/ask 已全走 WS，纯属白花）与
    **delta 漂移**（规则 9 要用）。而 **indicative 档下第二样也是零** ——
    Alpaca 免费档不填 greeks（`integrations/alpaca/data.py` 的 `contract_candidates`
    docstring），`contract_selection.py` 于是恒判 `delta_source="approx"`
    （`log/ttt.events.jsonl` 实测 23/23 全 approx、零条 alpaca），
    `_candidate_eligible()` 又因 `delta is not None` 短路而整条跳过 delta 判据。
    **重拉一次链拿回来的 delta 还是 None**，故此档下拉长 TTL 是纯收益、无需标定。

    为什么这很要紧（清单 §3）：66 槽 ÷ 15s → 160–264 次选约/分钟，而 Alpaca 额度
    **200/min**，结构性超限 → 选约被拖到 3.5s、扫完一轮 ≈58s ≫ 15s TTL →
    identity 永远过期，点击必然回落全量拉链（08-14 重启后 7/7 次 `miss=identity_stale`，
    复用命中率 **0%**，表现为「点一下不进、要点两次」）。

    **签下 OPRA 后 delta 变真，本函数自动回到 15s 原值等标定** —— 这也是这里按 feed 分档、
    而不是直接把常量调大的原因：`test_coordinator_miss_reason.py` 立过「盲调阈值是最坏的
    做法」，此处动的不是判据本身，而是「该判据在无 greeks 档位下本就买不到东西」。
    行权价该不该换仍由 `_PRICE_MOVE_RATIO`(0.5%) 与模板变更覆盖，未被削弱。
    """
    from app import config as _config
    feed = (getattr(_config, "ALPACA_OPTION_FEED", "") or "").strip().lower()
    return _IDENTITY_TTL_S if feed == "opra" else _IDENTITY_TTL_NO_GREEKS_S

DIRECTIONS = ("long", "short")

_HARD_RELEASE_RULES = {
    "breaker",
    "cooldown",
    "blackout",
    "afternoon",
    "whitelist",
    "execution",
    "offline",
}


@dataclass
class _CandidateState:
    ticker: str
    direction: str
    status: str = "BLOCKED"
    gate_allowed: bool = False
    gate_rule: str = ""
    gate_reason: str = ""
    allowed_since: float | None = None
    blocked_since: float | None = None
    anchor_price: Decimal | None = None
    template_version: str | None = None
    shortlist: list[SelectedContract] = field(default_factory=list)
    selected: SelectedContract | None = None
    pending_new: SelectedContract | None = None
    last_discovery_at: float | None = None
    updated_at: float | None = None
    error: str | None = None
    token: int = 0


def _block_reason(item: dict, price, gate: dict):
    """不选约时返回 (rule, 人话原因)；可以选约则返回 None。

    此前四个判据合成一个 bool，判否后统一显示兜底文案「当前不可入场」——
    而那串字恰恰出现在**下单闸门根本没拦**的时候（`gate.reason` 为空），
    用户于是看到「不可入场」却能一键买入成交（2026-08-03 BMNR）。
    每条判据必须自报家门，否则这类矛盾无从排查。

    2026-08-08：原先还有一条 `direction is None → ("chop", 分档文案)`，随规则 6
    一并删除——方向不再来自趋势分，「趋势分说不出方向所以没合约」这种状态不复存在。
    """
    if gate.get("blocked"):
        return str(gate.get("rule") or "watcher"), str(gate.get("reason") or "闸门拦截")
    if not gate:
        # tick 上没有 gates 键 = 干跑闸门取数失败（账户/行情异常），不是「不可入场」。
        return "gate_pending", "闸门未就绪"
    if not item.get("tradable"):
        return "quality", "数据质量不足"
    if price is None:
        return "price", "无有效现价"
    return None


def _price(value) -> Decimal | None:
    try:
        price = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return price if price > 0 else None


class OptionCandidateCoordinator:
    """单后台任务：MarketFeedHub → REST shortlist → Option WS scope。

    `max_workers` 是在途 REST 选约的并发上限，也是整条链路对券商的真实速率闸：
    扫完一轮 = 槽位数 × 单次耗时 / 并发。双边后 33 票 = 66 槽位，沿用旧值 2 会让
    周期从 ~14s 退化到 ~28s，超过 `_IDENTITY_TTL_S`（15s），一半时间在展示过期报价。
    """

    def __init__(
        self,
        *,
        stream: OptionQuoteStream = OPTION_STREAM,
        feed: MarketFeedHub = MARKET_FEED,
        max_workers: int = 4,
    ) -> None:
        self.stream = stream
        self.feed = feed
        self._states: dict[str, dict[str, _CandidateState]] = {}
        self._held: dict[str, bool] = {}
        self._tasks: dict[tuple[str, str], asyncio.Task] = {}
        self._semaphore = asyncio.Semaphore(max_workers)

    @staticmethod
    def _owner(ticker: str, direction: str) -> str:
        return f"candidates:{ticker.upper()}:{direction}"

    def _sides(self, ticker: str) -> dict[str, _CandidateState]:
        """取（或建）某票的两侧状态。"""
        sides = self._states.get(ticker)
        if sides is None:
            sides = {
                direction: _CandidateState(ticker=ticker, direction=direction)
                for direction in DIRECTIONS
            }
            self._states[ticker] = sides
        return sides

    def _all_states(self):
        for sides in self._states.values():
            yield from sides.values()

    def _template_version(self) -> str:
        store = get_store()
        try:
            return str(store.active_id())
        except AttributeError:
            return str(getattr(store.active(), "name", "active"))

    def _release(self, state: _CandidateState, *, reason: str) -> None:
        state.token += 1  # 让已经在工作线程里的旧结果失效
        self.stream.release_scope(self._owner(state.ticker, state.direction))
        state.status = "BLOCKED"
        state.selected = None
        state.pending_new = None
        state.shortlist = []
        state.anchor_price = None
        state.last_discovery_at = None
        state.error = reason

    def _promote_confirmed(self) -> None:
        confirmed = self.stream.confirmed_symbols
        for state in self._all_states():
            pending = state.pending_new
            if pending is None:
                if (
                    state.selected is not None
                    and state.status == "SUBSCRIBING"
                    and state.selected.option_code in confirmed
                ):
                    state.status = "SUBSCRIBED"
                continue
            if pending.option_code not in confirmed:
                continue
            state.selected = pending
            state.pending_new = None
            state.status = "SUBSCRIBED"
            state.updated_at = time.time()
            self.stream.replace_scope(
                self._owner(state.ticker, state.direction),
                {state.selected.option_code},
                PRIORITY_CANDIDATE,
            )

    def _refresh_reason(
        self,
        state: _CandidateState,
        *,
        price: Decimal,
        template_version: str,
        now: float,
    ) -> str | None:
        """该刷新的原因；`None` = 当前选中合约仍然有效。

        判据与拆分前一字不差，只是把「哪一条不满足」也说出来 —— 点击路径的
        复用命中率要靠这个分类才能定位（见 tests/test_coordinator_miss_reason.py）。
        """
        if state.selected is None or not state.shortlist:
            return "no_selection"
        if state.template_version != template_version:
            return "template_changed"
        if state.anchor_price is None:
            return "no_anchor"
        if abs(price - state.anchor_price) / state.anchor_price >= _PRICE_MOVE_RATIO:
            return "price_moved"
        if (
            state.last_discovery_at is None
            or now - state.last_discovery_at >= identity_ttl_s()
        ):
            return "identity_stale"
        return None

    def _needs_refresh(
        self,
        state: _CandidateState,
        *,
        price: Decimal,
        template_version: str,
        now: float,
    ) -> bool:
        return self._refresh_reason(
            state, price=price, template_version=template_version, now=now
        ) is not None

    def selection_miss_reason(
        self, ticker: str, direction: str, *, price: Decimal
    ) -> str:
        """`current_selection` 拿不到东西的原因；命中时返回 `"hit"`。

        纯只读诊断，与 `current_selection` 同源（同一个 `_refresh_reason`），
        故两者结论恒等 —— 报 hit 就一定拿得到。**不触发 discovery、不改状态。**

        与 `current_selection` 同款：`state` 缺失时立即返回，不去读模板仓库。
        """
        sides = self._states.get(str(ticker).upper())
        state = sides.get(direction) if sides else None
        if state is None:
            return "untracked"
        return self._refresh_reason(
            state,
            price=price,
            template_version=self._template_version(),
            now=time.monotonic(),
        ) or "hit"

    def current_selection(
        self, ticker: str, direction: str, *, price: Decimal
    ) -> SelectedContract | None:
        """本协调器当前对 (ticker, direction) 的选中合约，仅当它对 `price` 仍然有效。

        供点击路径复用，省掉一次期权链 REST —— `contract_candidates` 的缓存 key 含
        `str(price.normalize())`，协调器用 MARKET_FEED tick 价、点击路径用 `signal.price`，
        两者几乎不可能逐厘相同，于是**每次点击都是 cache miss、必然完整拉一次链**
        （2026-08-11 实测 contract_ms 中位 330ms、p90 3836ms、最大 6889ms）。

        **有效判据直接复用 `_needs_refresh()`**：协调器自己都认为该刷新的，点击路径
        更不能拿去下单。只读，不触发 discovery；拿不到返回 None 由调用方回落全量选约。

        线程安全性同 `snapshot()`：工作线程只做原子属性读，不改状态。
        """
        sides = self._states.get(str(ticker).upper())
        state = sides.get(direction) if sides else None
        if state is None:
            return None
        if self._needs_refresh(
            state,
            price=price,
            template_version=self._template_version(),
            now=time.monotonic(),
        ):
            return None
        return state.selected

    def _schedule_discovery(
        self,
        state: _CandidateState,
        *,
        price: Decimal,
        template_version: str,
        now: float,
    ) -> None:
        key = (state.ticker, state.direction)
        running = self._tasks.get(key)
        if running is not None and not running.done():
            return
        state.token += 1
        token = state.token
        state.status = "DISCOVERING" if state.selected is None else "REFRESHING"
        state.error = None
        task = asyncio.create_task(self._discover(
            state.ticker,
            direction=state.direction,
            price=price,
            template_version=template_version,
            requested_at=now,
            token=token,
        ))
        self._tasks[key] = task

    async def _discover(
        self,
        ticker: str,
        *,
        direction: str,
        price: Decimal,
        template_version: str,
        requested_at: float,
        token: int,
    ) -> None:
        owner = self._owner(ticker, direction)
        try:
            async with self._semaphore:
                state = self._states.get(ticker, {}).get(direction)
                if state is None or state.token != token or not state.gate_allowed:
                    return
                source = current_source()
                if getattr(source, "name", None) not in {"watcher", "live"}:
                    raise RuntimeError("当前数据源不是可执行档")
                shortlist = await asyncio.to_thread(
                    select_contract_shortlist,
                    source,
                    ticker,
                    direction,
                    price,
                )
            state = self._states.get(ticker, {}).get(direction)
            if state is None or state.token != token or not state.gate_allowed:
                return
            shortlist = list(shortlist or [])[:3]
            if not shortlist:
                state.status = "ERROR"
                state.error = "无合格期权候选"
                return

            old = state.selected
            primary = shortlist[0]
            state.anchor_price = price
            state.template_version = template_version
            state.shortlist = shortlist
            state.last_discovery_at = requested_at
            state.updated_at = time.time()
            state.error = None

            if old is None or old.option_code == primary.option_code:
                state.selected = primary
                state.pending_new = None
                state.status = (
                    "SUBSCRIBED"
                    if primary.option_code in self.stream.confirmed_symbols
                    else "SUBSCRIBING"
                )
                self.stream.replace_scope(
                    owner,
                    {primary.option_code},
                    PRIORITY_CANDIDATE,
                )
                return

            # 同方向换档：先订新、保留旧；下一轮看到新代码进入 confirmed 后才释放旧代码。
            state.pending_new = primary
            state.status = "SWITCHING"
            actual = self.stream.replace_scope(
                owner,
                {old.option_code, primary.option_code},
                PRIORITY_CANDIDATE,
            )
            if primary.option_code not in actual:
                state.pending_new = None
                state.status = "SUBSCRIBED"
                state.error = "Option WS 预算不足，保留旧主合约"
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — 单票单侧失败不影响其余候选
            state = self._states.get(ticker, {}).get(direction)
            if state is not None and state.token == token:
                state.status = "ERROR" if state.selected is None else "STALE"
                state.error = str(exc)
        finally:
            key = (ticker, direction)
            if self._tasks.get(key) is asyncio.current_task():
                self._tasks.pop(key, None)

    def _reconcile_side(
        self,
        state: _CandidateState,
        *,
        item: dict,
        price: Decimal | None,
        gate: dict,
        template_version: str,
        now: float,
    ) -> None:
        """单票单方向的 allow/block 状态推进。"""
        block_reason = _block_reason(item, price, gate)
        if block_reason is None:
            state.gate_allowed = True
            state.gate_rule = ""
            state.gate_reason = ""
            state.blocked_since = None
            if state.allowed_since is None:
                state.allowed_since = now
            if now - state.allowed_since < _ALLOW_STABLE_S:
                return
            assert price is not None
            if self._needs_refresh(
                state,
                price=price,
                template_version=template_version,
                now=now,
            ):
                self._schedule_discovery(
                    state,
                    price=price,
                    template_version=template_version,
                    now=now,
                )
            return

        state.gate_allowed = False
        state.allowed_since = None
        state.gate_rule = str(gate.get("rule") or block_reason[0])
        state.gate_reason = block_reason[1]
        if state.gate_rule in _HARD_RELEASE_RULES:
            self._release(state, reason=state.gate_reason)
            return
        if state.blocked_since is None:
            state.blocked_since = now
        if now - state.blocked_since >= _SOFT_RELEASE_S:
            self._release(state, reason=state.gate_reason)

    async def reconcile_snapshot(self, snapshot: dict, *, now: float | None = None) -> None:
        """处理一份共享行情快照。公开方法便于确定性单元测试。"""
        now = time.monotonic() if now is None else now
        self._promote_confirmed()
        source = current_source()
        source_name = str(getattr(source, "name", ""))
        frame_source = str(snapshot.get("source") or source_name)
        offline = bool(snapshot.get("stale")) or (
            source_name == "watcher" and snapshot.get("signal_online") is False
        )
        updated_at = snapshot.get("updated_at")
        if updated_at is not None and time.time() - float(updated_at) > 3.0:
            offline = True
        if source_name not in {"watcher", "live"} or frame_source != source_name or offline:
            for state in self._all_states():
                state.gate_allowed = False
                state.gate_rule = "offline"
                state.gate_reason = "行情源离线或已切换"
                self._release(state, reason=state.gate_reason)
            return

        template_version = self._template_version()
        ticks = {
            str(item.get("sym", "")).upper(): item
            for item in snapshot.get("ticks", [])
            if item.get("sym")
        }
        active_tickers: set[str] = set()
        held_tickers = {
            pos.ticker.upper()
            for pos in REGISTRY.active()
            if pos.status in {"open", "closing"}
        }

        for item in snapshot.get("pool", []):
            ticker = str(item.get("sym", "")).upper()
            if not ticker or ticker.startswith("^"):
                continue
            active_tickers.add(ticker)
            sides = self._sides(ticker)
            if ticker in held_tickers:
                # 信号候选与持仓行情解耦：持仓后释放会继续换档的候选订阅，
                # 实际持仓合约由 watchdog 的 positions scope 独立订阅。
                # 一票只要有仓就两侧一起停——加仓走持仓行的加仓按钮，不走候选。
                for state in sides.values():
                    if (state.status != "HELD" or state.selected is not None
                            or state.pending_new is not None):
                        self._release(state, reason="已持仓，候选订阅已暂停")
                    state.status = "HELD"
                    state.gate_allowed = False
                    state.gate_rule = "position"
                    state.gate_reason = "已持仓"
                    state.allowed_since = None
                    state.blocked_since = None
                self._held[ticker] = True
                continue
            if self._held.get(ticker):
                # 持仓刚清零时立即撤掉旧 HELD 展示。后续是否重新选约仍按 1 秒稳定闸门，
                # 但不能在这段等待期继续对用户声称“已持仓”。
                for state in sides.values():
                    state.status = "BLOCKED"
                    state.gate_allowed = False
                    state.gate_rule = ""
                    state.gate_reason = ""
                    state.error = None
                    state.allowed_since = None
                    state.blocked_since = None
                self._held[ticker] = False
            tick = ticks.get(ticker, {})
            raw_gates = tick.get("gates")
            gates = raw_gates if isinstance(raw_gates, dict) else {}
            price = _price(tick.get("price"))
            # 方向不再来自 watcher：规则 6 删除后买入方向只由用户点击决定，
            # 协调器无从预知，因而 CALL/PUT 各跑一份闸门与选约。
            for direction in DIRECTIONS:
                gate = gates.get(direction)
                self._reconcile_side(
                    sides[direction],
                    item=item,
                    price=price,
                    gate=gate if isinstance(gate, dict) else {},
                    template_version=template_version,
                    now=now,
                )

        # 从票池删除等同白名单/执行档硬关闭，立即释放候选。
        for ticker, sides in self._states.items():
            if ticker in active_tickers:
                continue
            for state in sides.values():
                state.gate_allowed = False
                state.gate_rule = "whitelist"
                state.gate_reason = "标的已离开当前票池"
                self._release(state, reason=state.gate_reason)

        self._promote_confirmed()

    async def wait_idle(self) -> None:
        """等待当前已排队的 REST worker 完成（测试/优雅关停使用）。"""
        while self._tasks:
            await asyncio.gather(*list(self._tasks.values()), return_exceptions=True)

    @staticmethod
    def _contract_dict(contract: SelectedContract) -> dict:
        return {
            "option_code": contract.option_code,
            "bid": str(contract.bid),
            "ask": str(contract.ask),
            "mid": str(contract.mid),
            "delta": str(contract.delta) if contract.delta is not None else None,
            "delta_source": contract.delta_source,
            "spread_pct": str(contract.spread_pct),
            "cost_per_contract": str(contract.ask * Decimal("100")),
        }

    def _side_dict(self, state: _CandidateState) -> dict:
        selected = state.selected
        quote = None
        source = "rest_snapshot"
        if selected is not None:
            ws_quote = self.stream.quote(selected.option_code)
            bid, ask = ws_quote if ws_quote is not None else (selected.bid, selected.ask)
            source = "ws" if ws_quote is not None else "rest_snapshot"
            mid = (bid + ask) / 2
            quote = {
                "bid": str(bid),
                "ask": str(ask),
                "mid": str(mid),
                "spread_pct": str((ask - bid) / mid if mid > 0 else Decimal("1")),
            }
        return {
            "direction": state.direction,
            "gate_allowed": state.gate_allowed,
            "gate": {"rule": state.gate_rule, "reason": state.gate_reason},
            "status": state.status,
            "selected": selected.option_code if selected else None,
            "selected_contract": self._contract_dict(selected) if selected else None,
            "pending_new": state.pending_new.option_code if state.pending_new else None,
            "shortlist": [self._contract_dict(item) for item in state.shortlist],
            "quote": quote,
            "source": source,
            "updated_at": state.updated_at,
            "error": state.error,
        }

    def snapshot(self) -> dict:
        """候选与订阅状态只读快照，供交易终端 WS 合并。"""
        previews = {}
        for ticker, sides in sorted(self._states.items()):
            previews[ticker] = {
                "ticker": ticker,
                "held": bool(self._held.get(ticker)),
                "sides": {
                    direction: self._side_dict(sides[direction])
                    for direction in DIRECTIONS
                },
            }
        return {"previews": previews, "subscription": self.stream.subscription_snapshot()}

    async def run(self) -> None:
        """后台协调循环；生命周期由 FastAPI lifespan 管理。"""
        try:
            while True:
                await self.reconcile_snapshot(self.feed.snapshot())
                await asyncio.sleep(_LOOP_INTERVAL_S)
        finally:
            for task in list(self._tasks.values()):
                task.cancel()
            await self.wait_idle()
            for state in self._all_states():
                self.stream.release_scope(self._owner(state.ticker, state.direction))


OPTION_CANDIDATES = OptionCandidateCoordinator()
