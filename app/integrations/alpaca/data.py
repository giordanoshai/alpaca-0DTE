"""Alpaca 数据侧（PAPER）—— 账户余额/市场时钟/正股 RTH 分钟线/期权链原始候选。

本模块零 UW 依赖：contract_candidates() 返回候选含 delta（取自 Alpaca 快照 snap.greeks，
PUT 已 abs）；上层合约选择服务直接消费，不再另拉 UW（indicative feed 无 greeks 时回退近似）。
"""
from __future__ import annotations

import datetime as dt
import json
import re
import time
from dataclasses import dataclass
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.broker_pnl import ProgramPnlSnapshot, compute_program_pnl
from app.config import ALPACA_OPTION_FEED
from app.core.dailylogger import get_logger
from app.integrations.alpaca.client import CLIENTS
from app.core.market_types import Bar

logger = get_logger("tt.alpaca_data")

_ET = ZoneInfo("America/New_York")
_CHAIN_WIDTHS = (Decimal("0.005"), Decimal("0.01"), Decimal("0.02"), Decimal("0.06"))
_OPTION_ROOT_MAP = {"SPX": "SPXW"}
_CONTRACT_MULTIPLIER = Decimal("100")
# 429 退避（2026-08-11）：08-10 三次撞到 Alpaca 限流，此前整个 integrations/alpaca
# 没有任何重试，限流一到持仓报价就空掉一拍。退避总时长上限约 0.75s——
# 点击路径本就慢，不能再给它加一个长尾。
_RATE_LIMIT_BACKOFF_S = (0.25, 0.5)
# 期权链取数超过这个耗时才落分解埋点。`contract_preview` 是前端每张卡片持续调用的
# 背景负载，走的是同一条 `contract_candidates` —— 无阈值全量记录会把 jsonl 淹掉，
# 反而让真正的慢样本更难找。
#
# 08-14 当天调过两次，门柱是被现实推着走的：
#   500ms  → 太低。样本 `COIN chain_ms=527.9 chain_calls=1` 只是一次正常代理往返。
#   2500ms → 当时基线中位 330ms、要抓的目标是 3.5s，落在两者之间。
#   8000ms → **3.5s 已经变成常态**。恢复 SDK 重试后实测（22:58 重启后 14 分钟、330 条）：
#            中位 3665ms、p90 6834ms、最大 14020ms —— 中位数就是「一次 429 + SDK sleep(3)」，
#            每条都记等于把常态当异常记，23.6 条/分钟。8000ms 只留多次重试的病态样本
#            （0.8 条/分钟）。
# 注意：这条日志现在是 429 的**唯一**可见性 —— SDK 重试恢复后它自己把 429 吞掉，
# `alpaca_rate_limited` 在重启后是 0 条。别因为"看起来干净了"就以为限流没了。
# 根治手段是降请求量，见 docs/reports/2026-08-14-当日改动审核与后续计划.md 的 P0。
_CHAIN_SLOW_LOG_MS = 8000.0
# 同一 (ticker, direction) 的节流窗口。项目的 `RepeatSuppressFilter`
# （`core/event_stream.py:294`）只对 WARNING 以上生效，INFO 事件必须自己节流；
# 与它同款取 60s，且同样把窗口内被吞掉的条数带出来 —— 静默丢弃会让人误以为「只慢了一次」。
_CHAIN_SLOW_LOG_THROTTLE_S = 60.0
# 期权盘口缓存 TTL。单发 option_quote 与批量 option_quotes **必须同值同缓存**，
# 否则两条路各自持有不同时刻的价，会出现「面板一个价、下单兜底另一个价」。
_OPTION_QUOTE_TTL_S = 5.0


def _is_rate_limited(exc: BaseException) -> bool:
    """Alpaca SDK 的 APIError 正文即 `{"message": "too many requests."}`。

    status_code 不是所有异常类型都带，故文本判定是主路径而非兜底。
    """
    if getattr(exc, "status_code", None) == 429:
        return True
    return "too many requests" in str(exc).lower()


@dataclass(frozen=True)
class AccountPnlSnapshot:
    """Alpaca 账户级盈亏；包含同账户下所有程序和手工交易。"""

    daily_pnl: Decimal
    weekly_pnl: Decimal
    equity: Decimal
    last_equity: Decimal
    week_base_equity: Decimal
    buying_power: Decimal
    as_of: dt.datetime


class ClockInfo:
    def __init__(self, is_open: bool, minutes_since_open: Decimal, session_date: dt.date):
        self.is_open = is_open
        self.minutes_since_open = minutes_since_open
        self.session_date = session_date  # 用于 0DTE 到期日


class SelectedContract:
    """③ 选中的期权合约（真实报价 + 期权代码，供执行下单）。

    delta：Alpaca 期权快照 Greeks 真值（delta_source="alpaca"）；快照缺失时
    ``None`` 时由上层合约选择服务使用贴平值近似 0.50
    （delta_source="approx"）。
    """

    def __init__(self, option_code: str, bid: Decimal, ask: Decimal,
                 delta: Decimal | None = None, delta_source: str = "approx"):
        self.option_code = option_code
        self.bid = bid
        self.ask = ask
        self.mid = (bid + ask) / 2
        self.spread_pct = (ask - bid) / self.mid if self.mid > 0 else Decimal("1")
        self.delta = delta
        self.delta_source = delta_source


class AlpacaData:
    def __init__(self, cache_ttl: float = 20.0):
        self._ttl = cache_ttl
        self._cache: dict = {}
        self.last_chain_meta: dict[tuple[str, str], dict] = {}
        # (ticker, direction) -> [上次落日志的 monotonic, 之后被节流吞掉的条数]
        self._chain_slow_streak: dict[tuple[str, str], list] = {}

    def _tc(self):
        return CLIENTS.trading()

    def _sc(self):
        return CLIENTS.stock()

    def _oc(self):
        return CLIENTS.option()

    def _cached(self, key, ttl, fn, force: bool = False):
        """force=True 跳过命中判定强制重取，并把新值写回缓存（后续普通读拿到的也是新值）。

        本方法是**只读取数**的唯一收口，429 退避重试就挂在这里。下单/撤单走
        `trading.py`，是非幂等调用 —— 429 之后原请求可能已经到达券商，重试会造成
        重复下单，那边一次都不许重试（由 tests/test_alpaca_rate_limit_retry.py 守住）。
        """
        hit = self._cache.get(key)
        if not force and hit and (time.time() - hit[0]) < ttl:
            return hit[1]
        val = self._call_with_rate_limit_retry(key, fn)
        self._cache[key] = (time.time(), val)
        return val

    @staticmethod
    def _call_with_rate_limit_retry(key, fn):
        """只对 429 退避重试；其余异常立刻抛出，不浪费点击路径的时间。"""
        for attempt in range(len(_RATE_LIMIT_BACKOFF_S) + 1):
            try:
                return fn()
            except Exception as exc:  # noqa: BLE001 — 判定后原样抛出，不吞
                if attempt >= len(_RATE_LIMIT_BACKOFF_S) or not _is_rate_limited(exc):
                    raise
                delay = _RATE_LIMIT_BACKOFF_S[attempt]
                logger.warning(json.dumps({
                    "event": "alpaca_rate_limited",
                    "key": str(key),
                    "attempt": attempt + 1,
                    "backoff_s": delay,
                }, ensure_ascii=False))
                time.sleep(delay)
        raise AssertionError("unreachable")  # pragma: no cover

    # --- 账户 ---
    def buying_power(self) -> Decimal:
        return self._cached("bp", self._ttl, lambda: Decimal(str(self._tc().get_account().buying_power)))

    def account_pnl(self, now: dt.datetime | None = None) -> AccountPnlSnapshot:
        """Alpaca 账户级今日/本周盈亏，与 Alpaca 页面账户权益口径一致。

        今日 = ``equity - last_equity``；
        本周 = ``equity - 本周一开始时的 portfolio history base_value``。
        两者都包含此 Alpaca 账户中的全部交易和当前未实现盈亏，而不仅是本程序落库交易。
        """
        def build() -> AccountPnlSnapshot:
            from alpaca.common.exceptions import APIError
            from alpaca.trading.requests import GetPortfolioHistoryRequest

            as_of = now or dt.datetime.now(dt.timezone.utc)
            if as_of.tzinfo is None:
                as_of = as_of.replace(tzinfo=dt.timezone.utc)
            now_et = as_of.astimezone(_ET)
            monday = now_et.date() - dt.timedelta(days=now_et.weekday())
            week_start = dt.datetime.combine(monday, dt.time.min, tzinfo=_ET)

            account = self._tc().get_account()
            equity = Decimal(str(account.equity))
            last_equity = Decimal(str(account.last_equity))

            try:
                history = self._tc().get_portfolio_history(GetPortfolioHistoryRequest(
                    start=week_start,
                    end=as_of,
                    timeframe="1D",
                    pnl_reset="no_reset",
                ))
            except APIError as exc:
                # 周一盘前（以及周初遇休市）还没有本周的 1D 数据点。Alpaca 会把
                # end 对齐到上周最后一个日线点，即使显式传入当前时间，仍可能得到
                # start(本周一) > end(上周五) 的 400。这只表示本周基准尚未生成。
                if exc.code != 400 or "start cannot be after end" not in exc.message:
                    raise
                week_base = last_equity
            else:
                # 空 history 同样是正常的周初状态；以上一交易日收盘权益为基准。
                week_base = (
                    last_equity
                    if history.base_value is None
                    else Decimal(str(history.base_value))
                )
            return AccountPnlSnapshot(
                daily_pnl=equity - last_equity,
                weekly_pnl=equity - week_base,
                equity=equity,
                last_equity=last_equity,
                week_base_equity=week_base,
                buying_power=Decimal(str(account.buying_power)),
                as_of=as_of,
            )

        return self._cached("account-pnl", self._ttl, build)

    def program_pnl(
        self,
        now: dt.datetime | None = None,
    ) -> ProgramPnlSnapshot:
        """从 Alpaca 周内成交拆出 ``ttt-*`` 订单盈亏；本地数据库不参与归属判断。"""
        as_of = now or dt.datetime.now(dt.timezone.utc)
        if as_of.tzinfo is None:
            as_of = as_of.replace(tzinfo=dt.timezone.utc)
        now_et = as_of.astimezone(_ET)
        monday = now_et.date() - dt.timedelta(days=now_et.weekday())
        week_start = dt.datetime.combine(monday, dt.time.min, tzinfo=_ET)
        day_start = dt.datetime.combine(now_et.date(), dt.time.min, tzinfo=_ET)
        cache_key = (
            "program-pnl",
            monday.isoformat(),
        )

        def build() -> ProgramPnlSnapshot:
            from alpaca.common.enums import Sort
            from alpaca.trading.enums import QueryOrderStatus
            from alpaca.trading.requests import GetOrdersRequest

            account = self.account_pnl(as_of)
            orders = self._tc().get_orders(GetOrdersRequest(
                status=QueryOrderStatus.ALL,
                after=week_start,
                direction=Sort.ASC,
                limit=500,
            ))
            positions = self._tc().get_all_positions()
            return compute_program_pnl(
                orders=orders,
                current_positions=positions,
                daily_start=day_start,
                weekly_start=week_start,
                account_daily_pnl=account.daily_pnl,
                account_weekly_pnl=account.weekly_pnl,
                as_of=as_of,
            )

        return self._cached(cache_key, self._ttl, build)

    # --- 时钟 ---
    def clock(self) -> ClockInfo:
        def build():
            c = self._tc().get_clock()
            now_et = c.timestamp.astimezone(_ET)
            if c.is_open:
                open_et = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
                mins = Decimal(str(round((now_et - open_et).total_seconds() / 60, 1)))
                session = now_et.date()
            else:
                mins = Decimal("9999")  # 非交易时段：blackout 不适用
                session = c.next_open.astimezone(_ET).date()
            return ClockInfo(c.is_open, mins, session)
        return self._cached("clock", self._ttl, build)

    # --- 正股 RTH 分钟线（最近一个交易日）---
    def rth_bars(self, ticker: str) -> list[Bar]:
        def build():
            from alpaca.data.requests import StockBarsRequest
            from alpaca.data.timeframe import TimeFrame
            req = StockBarsRequest(symbol_or_symbols=ticker, timeframe=TimeFrame.Minute,
                                   start=dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=4))
            raw = self._sc().get_stock_bars(req).data.get(ticker, [])
            # 转 ET，取最新交易日的 RTH(09:30–16:00)
            rows = []
            for b in raw:
                et = b.timestamp.astimezone(_ET)
                rows.append((et, b))
            if not rows:
                return []
            latest = max(r[0].date() for r in rows)
            session = [b for (et, b) in rows
                       if et.date() == latest
                       and (et.hour, et.minute) >= (9, 30) and et.hour < 16]
            return [Bar(int(b.timestamp.timestamp()), Decimal(str(b.open)), Decimal(str(b.high)),
                        Decimal(str(b.low)), Decimal(str(b.close)), Decimal(str(b.volume))) for b in session]
        return self._cached(("bars", ticker.upper()), self._ttl, build)

    def nearest_expiry(self, ticker: str):
        """最近可用期权到期日（≥ 今天）。每日到期标的（QQQ/SPY/IWM）=今天；
        无当日到期的标的（如 CRWV）=最近的那个到期（周五等）。

        交易纪律是「日内进出、不持仓过夜」——这不要求合约**当天到期**，取最近到期即可。
        此前 contract_candidates 写死 clock().session_date，致 CRWV 类标的选不到合约、
        前端下单报「无可用合约」（2026-07-21 盘中实锤）。"""
        def build():
            from alpaca.trading.requests import GetOptionContractsRequest
            root = _option_root(ticker)
            # 筛选下界必须是**美东交易日**，不能用 dt.date.today()：本机在 UTC+8 时，
            # 美东 12:00–16:00 ET 正对应北京次日 00:00–04:00，本机日期已跳到次日，
            # 会把当天到期的 0DTE 整段过滤掉 —— 不报错，静默选到次日到期的合约
            # （2026-08-15 代码审查 CRITICAL-1）。clock() 盘中给 ET 当天、
            # 闭市给下一开盘日，正是本函数「最近可用到期日」需要的口径。
            req = GetOptionContractsRequest(underlying_symbols=[root],
                                            expiration_date_gte=self.clock().session_date,
                                            limit=100)
            res = self._tc().get_option_contracts(req)
            exps = sorted({c.expiration_date for c in res.option_contracts})
            return exps[0] if exps else self.clock().session_date
        return self._cached(("nearexp", _option_root(ticker)), 300.0, build)

    def contract_candidates(
        self,
        ticker: str,
        direction: str,
        price: Decimal,
        *,
        min_delta: Decimal | None = None,
        max_spread_pct: Decimal | None = None,
        max_contract_cost: Decimal | None = None,
        template_version: str | int | None = None,
    ):
        """③ 期权链候选（按平值距离升序）：list[(strike_dist, code, bid, ask, delta)]。

        delta 取自同一次 get_option_chain 返回的 snap.greeks（PUT 取绝对值，与 UW 口径一致），
        免二次取数；indicative(免费) feed 未填 greeks → delta=None（选约回退最贴平值近似）。
        切实盘签 OPRA 后 greeks 自动到位。服务端先锁定 expiry、CALL/PUT 和 strike 范围，
        范围按 ±0.5% → ±1% → ±2% → ±6% 扩宽，当前档出现满足 Delta/点差/成本条件的
        候选即停止，避免常态拉整段 ±6% 链。
        """
        ticker = ticker.upper()
        direction = direction.lower()
        if direction not in {"long", "short"}:
            raise ValueError(f"不支持的期权方向: {direction}")
        price = Decimal(str(price))
        started = time.perf_counter()
        expiry = self.nearest_expiry(ticker)
        expiry_ms = (time.perf_counter() - started) * 1000
        root = _option_root(ticker)
        cache_key = (
            "cands-adaptive",
            root,
            direction,
            expiry.isoformat(),
            str(price.normalize()),
            str(min_delta),
            str(max_spread_pct),
            str(max_contract_cost),
            str(template_version),
        )

        # 分解埋点用；build() 只在 cache miss 时跑，命中时两项都留 0。
        probe = {"chain_ms": 0.0, "chain_calls": 0}

        def build():
            from alpaca.data.requests import OptionChainRequest
            from alpaca.trading.enums import ContractType
            ctype = ContractType.CALL if direction == "long" else ContractType.PUT
            latest: list[tuple[float, str, Decimal, Decimal, Decimal | None]] = []
            width_used = _CHAIN_WIDTHS[-1]
            for width in _CHAIN_WIDTHS:
                width_used = width
                req = OptionChainRequest(
                    underlying_symbol=root,
                    expiration_date=expiry,
                    type=ctype,
                    strike_price_gte=float(price * (Decimal(1) - width)),
                    strike_price_lte=float(price * (Decimal(1) + width)),
                    feed=ALPACA_OPTION_FEED,
                )
                call_started = time.perf_counter()
                chain = self._oc().get_option_chain(req)
                probe["chain_ms"] += (time.perf_counter() - call_started) * 1000
                probe["chain_calls"] += 1
                latest = _parse_chain(chain, price)
                if any(
                    _candidate_eligible(
                        candidate,
                        min_delta=min_delta,
                        max_spread_pct=max_spread_pct,
                        max_contract_cost=max_contract_cost,
                    )
                    for candidate in latest
                ):
                    break
            self.last_chain_meta[(ticker, direction)] = {
                "root": root,
                "expiry": expiry.isoformat(),
                "width": str(width_used),
                "count": len(latest),
                "template_version": str(template_version),
            }
            return latest

        result = self._cached(cache_key, self._ttl, build)

        # 慢样本才落埋点。四种慢法各有唯一签名：
        #   chain_calls≥2         → _CHAIN_WIDTHS 逐档扩宽（串行往返）
        #   chain_calls==1 且 chain_ms 高 → 单次调用慢（网络 / SDK 内部 sleep）
        #   expiry_ms 高          → nearest_expiry 那一发 REST（300s 缓存冷启动）
        #   other_ms 高           → `_parse_chain()` 的 CPU 解析 + `_call_with_rate_limit_retry`
        #                           的 429 退避 sleep。08-14 实测这两者里**解析是大头**：
        #                           461 条慢样本中 318 条 other_ms>200ms，而同期
        #                           `alpaca_rate_limited` 只有 2 次 —— 别再把 other_ms
        #                           当成纯退避读（初版注释就是这么写的，会误导排查）。
        total_ms = (time.perf_counter() - started) * 1000
        if total_ms >= _CHAIN_SLOW_LOG_MS:
            key = (ticker, direction)
            now = time.monotonic()
            streak = self._chain_slow_streak.get(key)
            if streak is not None and now - streak[0] < _CHAIN_SLOW_LOG_THROTTLE_S:
                streak[1] += 1                      # 窗口内：只计数，不落盘
            else:
                suppressed = streak[1] if streak else 0
                self._chain_slow_streak[key] = [now, 0]
                meta = self.last_chain_meta.get(key, {})
                payload = {
                    "event": "chain_fetch_slow",
                    "ticker": ticker,
                    "direction": direction,
                    "expiry_ms": round(expiry_ms, 1),
                    "chain_ms": round(probe["chain_ms"], 1),
                    "chain_calls": probe["chain_calls"],
                    "other_ms": round(total_ms - expiry_ms - probe["chain_ms"], 1),
                    "width_used": meta.get("width"),
                    "count": meta.get("count"),
                    "total_ms": round(total_ms, 1),
                }
                if suppressed:
                    payload["suppressed"] = suppressed
                logger.info(json.dumps(payload, ensure_ascii=False))
        return result

    def option_quote(self, option_code: str, fresh: bool = False):
        """某期权最新 (bid, ask)；平仓定价用。取不到→None。

        fresh=True 强制穿透 5s 缓存。**凡是给真实委托定价的读取都必须 fresh**——
        追单 attempts=3 × wait_s=1.5 全程 4.5s < 5s TTL，不穿透的话三手拿的是同一条
        缓存报价，「重取报价、更深重挂」名存实亡（2026-07-25 审查 2.2）。
        展示类读取（/api/positions 每 2s 轮询 N 个持仓）保持吃缓存，否则打爆 Alpaca。
        """
        def build():
            from alpaca.data.requests import OptionLatestQuoteRequest
            q = self._oc().get_option_latest_quote(OptionLatestQuoteRequest(
                symbol_or_symbols=option_code, feed=ALPACA_OPTION_FEED))
            item = q.get(option_code)
            if not item or item.bid_price is None or item.ask_price is None:
                return None
            return (Decimal(str(item.bid_price)), Decimal(str(item.ask_price)))
        return self._cached(("oq", option_code), _OPTION_QUOTE_TTL_S, build, force=fresh)

    def option_quotes(
        self, option_codes, fresh: bool = False
    ) -> dict[str, tuple[Decimal, Decimal]]:
        """批量取 (bid, ask)：N 个合约合并成**一发** REST，取不到的合约不进返回值。

        `OptionLatestQuoteRequest.symbol_or_symbols` 官方类型是 `Union[str, List[str]]`
        （docs/vendor/alpaca/python_api/data/option/requests.md:164）。最痛的调用方是
        `exec_reconcile.list_positions` —— 它被 `TradingStateHub._positions_loop` 以
        0.5 秒一拍驱动，持仓 N 个又没命中 Option WS 时就是每拍 N 发 REST，打的正是那个
        已经在限流的行情 API（2026-08-11 三次 alpaca_rate_limited）。

        与 `option_quote()` **共用同一份 `("oq", code)` 缓存与 TTL**：只对未命中的部分
        发请求，结果写回缓存供后续单发命中。`fresh=True` 全部穿透（给真实委托定价用）。

        无文档化的单请求符号数上限；当前调用方是持仓数（个位数），故未分片。
        若将来拿它批量刷整个候选池，需要按 URL 长度分片。
        """
        codes = list(dict.fromkeys(code for code in option_codes if code))
        if not codes:
            return {}

        out: dict[str, tuple[Decimal, Decimal]] = {}
        pending: list[str] = []
        now = time.time()
        for code in codes:
            hit = self._cache.get(("oq", code))
            if not fresh and hit and (now - hit[0]) < _OPTION_QUOTE_TTL_S:
                if hit[1] is not None:
                    out[code] = hit[1]
            else:
                pending.append(code)
        if not pending:
            return out

        def build():
            from alpaca.data.requests import OptionLatestQuoteRequest
            return self._oc().get_option_latest_quote(OptionLatestQuoteRequest(
                symbol_or_symbols=pending, feed=ALPACA_OPTION_FEED))

        raw = self._call_with_rate_limit_retry(("oq-batch", tuple(pending)), build)
        stamped = time.time()
        for code in pending:
            item = raw.get(code) if raw else None
            if not item or item.bid_price is None or item.ask_price is None:
                value = None      # 与单发一致：无报价也入缓存，避免每拍重复空拉
            else:
                value = (Decimal(str(item.bid_price)), Decimal(str(item.ask_price)))
            self._cache[("oq", code)] = (stamped, value)
            if value is not None:
                out[code] = value
        return out


def _strike_from_code(code: str) -> float | None:
    # OCC 代码尾部 8 位为 strike×1000，如 ...C00719000 → 719.0
    m = re.search(r"[CP](\d{8})$", code)
    return int(m.group(1)) / 1000.0 if m else None


def _option_root(ticker: str) -> str:
    ticker = ticker.upper()
    return _OPTION_ROOT_MAP.get(ticker, ticker)


def _parse_chain(chain, price: Decimal):
    cands = []
    for code, snap in chain.items():
        strike = _strike_from_code(code)
        if strike is None:
            continue
        q = getattr(snap, "latest_quote", None)
        if not q or q.bid_price in (None, 0) or q.ask_price in (None, 0):
            continue
        g = getattr(snap, "greeks", None)
        dv = getattr(g, "delta", None) if g is not None else None
        delta = abs(Decimal(str(dv))) if dv is not None else None
        cands.append((
            abs(strike - float(price)),
            code,
            Decimal(str(q.bid_price)),
            Decimal(str(q.ask_price)),
            delta,
        ))
    cands.sort(key=lambda item: item[0])
    return cands


def _candidate_eligible(
    candidate,
    *,
    min_delta: Decimal | None,
    max_spread_pct: Decimal | None,
    max_contract_cost: Decimal | None,
) -> bool:
    _dist, _code, bid, ask, delta = candidate
    if min_delta is not None and delta is not None and delta < min_delta:
        return False
    mid = (bid + ask) / 2
    spread = (ask - bid) / mid if mid > 0 else Decimal("1")
    if max_spread_pct is not None and spread > max_spread_pct:
        return False
    if max_contract_cost is not None and ask * _CONTRACT_MULTIPLIER > max_contract_cost:
        return False
    return True
