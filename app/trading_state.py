"""交易终端实时状态快照。

订单、持仓和风控都属于服务端状态：后台只生成一份快照，所有 ``/ws`` 客户端复用，
避免每个浏览器连接各自查询券商。REST GET 路由保留作诊断与兼容，不再作为前端轮询源。
"""
from __future__ import annotations

import asyncio
import json
import traceback
from datetime import datetime, timezone

from app.exec_reconcile import (
    fetch_broker_order_session,
    list_positions,
    list_unmanaged_positions,
)
from app.config import ALPACA_TRADE_RECONCILE_INTERVAL_S
from app.core.dailylogger import get_logger
from app.order_journal import JOURNAL
from app.core.market_session import order_session_window
from app.risk import compute_risk
from app.services.account_state import closed_trades, trade_count_today
from app.services.data_source_registry import current_source
from app.services.order_sync import ORDER_SYNC
from app.templates import get_store
from app.trade_update_stream import TRADE_UPDATE_STREAM

logger = get_logger("tt.gate")


def _order_session_metadata(now: datetime | None = None) -> dict:
    window = order_session_window(now)
    return {
        "session_date": window["session_date"],
        "session_start": window["session_start"].isoformat(),
        "session_end": window["session_end"].isoformat(),
        "timezone": window["timezone"],
    }


def risk_snapshot(now: datetime | None = None) -> dict:
    """当前 Alpaca 账户盈亏、交易次数与生效模板组成的只读风控快照。

    日/周盈亏包含同一 Alpaca 账户中所有程序和手工交易；连亏/冷却仍按本系统已平仓
    交易计算。页面展示与实际止盈/止损闸门共用这套账户盈亏口径。
    """
    tpl = get_store().active()
    as_of = now or datetime.now(timezone.utc)
    local_state = compute_risk(
        closed_trades(),
        as_of,
        tpl.cooldown_seconds,
    )
    source = current_source()
    broker = source.program_pnl(as_of)
    return {
        "trades_today": trade_count_today(as_of),
        "max_trades_per_day": tpl.max_trades_per_day,
        "daily_pnl": str(broker.daily.program_gross_pnl),
        "weekly_pnl": str(broker.weekly.program_gross_pnl),
        "consecutive_losses": local_state.consecutive_losses,
        "cooldown_remaining_s": local_state.cooldown_remaining_s,
        "daily_profit_target": str(tpl.daily_profit_target),
        "max_daily_loss": str(tpl.max_daily_loss),
        "max_weekly_loss": str(tpl.max_weekly_loss),
        "pnl_basis": "alpaca_program_fills",
        "pnl_timezone": "America/New_York",
        "week_starts_on": "monday",
        "account_daily_pnl": str(broker.daily.account_net_pnl),
        "external_daily_gross_pnl": str(broker.daily.external_gross_pnl),
        "account_daily_adjustment": str(broker.daily.account_adjustment),
        "account_weekly_pnl": str(broker.weekly.account_net_pnl),
        "external_weekly_gross_pnl": str(broker.weekly.external_gross_pnl),
        "account_weekly_adjustment": str(broker.weekly.account_adjustment),
        "as_of": broker.as_of.isoformat(),
    }


class TradingStateHub:
    """单生产者、多 WS 消费者的交易状态缓存。"""

    def __init__(
        self,
        *,
        state_interval_s: float = 0.5,
        positions_interval_s: float = 0.5,
        broker_reconcile_interval_s: float = ALPACA_TRADE_RECONCILE_INTERVAL_S,
    ) -> None:
        self.state_interval_s = state_interval_s
        self.positions_interval_s = positions_interval_s
        self.broker_reconcile_interval_s = broker_reconcile_interval_s
        self._orders: dict = {"pending": [], "filled": [], "cancelled": []}
        self._positions: dict = {"positions": [], "unmanaged": []}
        self._risk: dict | None = None
        self._broker_sync: dict = {}
        # 上一条刷新错误的文本。同一个错误每 state_interval_s 重复一次会刷屏，
        # 只在错误内容变化时附完整堆栈——既不淹没日志，又保证任何新故障都能定位来源。
        self._last_refresh_error: str | None = None
        self._revisions: dict[str, int] = {
            "orders": 0,
            "positions": 0,
            "risk": 0,
            "broker_sync": 0,
        }
        self._orders_source_revision = -1
        self._orders_session_date: str | None = None
        self._order_baseline: dict = {
            **_order_session_metadata(),
            "last_synced_at": None,
            "orders_received": 0,
            "last_error": None,
        }

    def snapshot(self) -> dict:
        """返回当前不可变引用集合；各子快照更新时均以整对象原子替换。"""
        return {
            "orders": self._orders,
            "positions": self._positions,
            "risk": self._risk,
            "broker_sync": self._broker_sync,
            "revisions": dict(self._revisions),
        }

    def section_revisions(self) -> dict[str, int]:
        return dict(self._revisions)

    def _replace_section(self, name: str, value, *, force: bool = False) -> bool:
        attr = f"_{name}"
        if not force and getattr(self, attr) == value:
            return False
        setattr(self, attr, value)
        self._revisions[name] += 1
        return True

    def _refresh_orders(
        self,
        now: datetime | None = None,
        *,
        force: bool = False,
    ) -> bool:
        window = order_session_window(now)
        source_revision = JOURNAL.revision
        session_date = window["session_date"]
        session_changed = session_date != self._orders_session_date
        if (
            not force
            and source_revision == self._orders_source_revision
            and not session_changed
        ):
            return False
        orders = JOURNAL.current_session(now, compact=True)
        changed = self._replace_section(
            "orders",
            orders,
            force=force or session_changed,
        )
        self._orders_source_revision = source_revision
        self._orders_session_date = session_date
        return changed

    def _refresh_broker_sync(self, *, force: bool = False) -> bool:
        broker_sync = {
            **TRADE_UPDATE_STREAM.status_snapshot(),
            **ORDER_SYNC.status_snapshot(),
            "order_baseline": self._order_baseline,
        }
        return self._replace_section("broker_sync", broker_sync, force=force)

    def refresh_fast(self) -> None:
        """同步刷新入口（诊断/测试兼容）；生产循环使用异步版本避免阻塞事件循环。"""
        now = datetime.now(timezone.utc)
        self._refresh_orders(now)
        self._replace_section("risk", risk_snapshot(now))
        self._refresh_broker_sync()

    async def refresh_runtime_state(self) -> None:
        """刷新共享订单/风控快照；账户 PnL 等同步 I/O 在线程池执行。"""
        now = datetime.now(timezone.utc)
        self._refresh_orders(now)
        risk = await asyncio.to_thread(risk_snapshot, now)
        self._replace_section("risk", risk)
        self._refresh_broker_sync()

    async def refresh_broker_orders(self) -> dict:
        """启动、ET 换日或 TradingStream 重连时补齐 ET 当天终态与所有 OPEN 单。"""
        try:
            orders = await asyncio.to_thread(
                fetch_broker_order_session,
            )
            merged = await asyncio.to_thread(
                JOURNAL.upsert_broker_orders,
                orders,
            )
            self._order_baseline = {
                **_order_session_metadata(),
                "last_synced_at": datetime.now(timezone.utc).isoformat(),
                "orders_received": len(orders),
                "last_error": None,
                **merged,
            }
            # 即使基线内容为空，也必须让前端收到一次 orders，建立“不补播历史提示音”的水位。
            self._refresh_orders(force=True)
            self._refresh_broker_sync(force=True)
            logger.info(json.dumps(
                {"event": "broker_order_baseline_synced", **self._order_baseline},
                ensure_ascii=False,
            ))
            return self._order_baseline
        except Exception as exc:
            self._order_baseline = {
                **self._order_baseline,
                "last_error": str(exc),
            }
            self._refresh_broker_sync(force=True)
            raise

    async def refresh_positions(self) -> None:
        """刷新登记持仓与行情；不扫描券商账户，避免 UI 刷新制造 REST 轮询。"""
        source = current_source()
        managed = await asyncio.to_thread(
            list_positions,
            source,
            include_unmanaged=False,
        )
        positions = {
            "positions": managed["positions"],
            "unmanaged": self._positions["unmanaged"],
        }
        self._replace_section("positions", positions)

    async def refresh_broker_positions(self) -> None:
        """低频 REST 真源扫描，只用于发现未登记仓；TradingStream 不提供初始持仓快照。"""
        source = current_source()
        unmanaged = await asyncio.to_thread(list_unmanaged_positions, source)
        positions = {
            "positions": self._positions["positions"],
            "unmanaged": unmanaged,
        }
        self._replace_section("positions", positions)

    async def _positions_loop(self) -> None:
        while True:
            try:
                await self.refresh_positions()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — 单次券商抖动保留上一份成功快照
                logger.warning(json.dumps(
                    {"event": "trading_state_positions_error", "error": str(exc)},
                    ensure_ascii=False,
                ))
            await asyncio.sleep(self.positions_interval_s)

    async def _broker_reconcile_loop(self) -> None:
        while True:
            try:
                await self.refresh_broker_positions()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning(json.dumps(
                    {"event": "trading_state_broker_reconcile_error", "error": str(exc)},
                    ensure_ascii=False,
                ))
            await asyncio.sleep(self.broker_reconcile_interval_s)

    async def _broker_orders_loop(self) -> None:
        """REST 建基线后等 WS；每次重连或 ET 换日再补一次当前会话。"""
        seeded = False
        seeded_generation = -1
        seeded_session_date: str | None = None
        while True:
            connected = TRADE_UPDATE_STREAM.connected
            generation = TRADE_UPDATE_STREAM.connection_generation
            session_date = order_session_window()["session_date"]
            should_sync = (
                not seeded
                or session_date != seeded_session_date
                or (connected and generation != seeded_generation)
            )
            if should_sync:
                try:
                    await self.refresh_broker_orders()
                    seeded = True
                    seeded_session_date = session_date
                    if connected:
                        seeded_generation = generation
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    logger.warning(json.dumps(
                        {"event": "broker_order_baseline_error", "error": str(exc)},
                        ensure_ascii=False,
                    ))
                    await asyncio.sleep(5.0)
                    continue
            await asyncio.sleep(0.5)

    async def run(self) -> None:
        """持续刷新共享快照，生命周期由 FastAPI lifespan 管理。"""
        positions_task = asyncio.create_task(self._positions_loop())
        broker_task = asyncio.create_task(self._broker_reconcile_loop())
        broker_orders_task = asyncio.create_task(self._broker_orders_loop())
        try:
            while True:
                try:
                    await self.refresh_runtime_state()
                except Exception as exc:  # noqa: BLE001 — 保留上一份状态，不中断推流
                    msg = str(exc)
                    payload = {"event": "trading_state_refresh_error", "error": msg}
                    if msg != self._last_refresh_error:
                        # 只有错误内容变化时才附堆栈：这个 except 吞掉了调用链里
                        # 任何一处的异常，只打 str(exc) 时根本看不出是谁抛的
                        # （实测 2026-08-03 一条 Alpaca 400 每秒刷屏，本地却复现不出）。
                        payload["traceback"] = traceback.format_exc()[-1500:]
                        self._last_refresh_error = msg
                    logger.warning(json.dumps(payload, ensure_ascii=False))
                else:
                    self._last_refresh_error = None
                await asyncio.sleep(self.state_interval_s)
        finally:
            positions_task.cancel()
            broker_task.cancel()
            broker_orders_task.cancel()
            await asyncio.gather(
                positions_task,
                broker_task,
                broker_orders_task,
                return_exceptions=True,
            )


TRADING_STATE = TradingStateHub()
