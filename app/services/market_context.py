"""从原始数据源组装交易闸门所需上下文。"""
from __future__ import annotations

import dataclasses
import json
import time
from decimal import Decimal

from app.config import VWAP_SIGMA_ATR_FLOOR
from app.core.dailylogger import get_logger
from app.market_state import (
    atr,
    session_vwap,
    sigma_with_atr_floor,
    vwap_sigma,
)
from app.rules.market import format_data_age_zh
from app.services.account_state import build_account_state
from app.integrations.alpaca.data import SelectedContract
from app.services.contract_selection import (
    contract_for_validation,
    select_contract_full,
)
from app.services.market_context_types import MarketContext
from app.validator import Contract, TickerSignal

logger = get_logger("tt.market_context")

ATR_PERIOD = 14
#: bars 降级路径至少要凑够这么多根 1min 才敢组装快照（ATR14 要 15 根）。
MIN_BARS = ATR_PERIOD + 1
_PLACEHOLDER_CONTRACT = Contract(
    delta=Decimal("0.45"),
    spread_pct=Decimal("0.02"),
    mid=Decimal("2.00"),
)


def signal_from_payload(payload: dict, *, require_tradable: bool) -> TickerSignal:
    kind = payload.get("kind")
    if kind == "watcher":
        state = payload.get("state") or {}
        # 数据落后闸（2026-08-28）：状态里的 K 线比现在旧太多就不放行。
        # **必须排在质量闸之前** —— 数据落后时质量结论本身就是拿旧 K 线算出来的，
        # 那时报「不可交易」会把人引去查行情，而真因是数据没跟上（2026-08-28 崩溃
        # 重启现场：13 只票全报不可交易，实际是缓存停在盘前 19 分钟）。
        # 判据在 watcher.get_state 里算好，这里只读结论。缺键按不落后处理，
        # 与下面 tradable 的兜底同一条约定：滚动升级/老快照期间不误锁。
        if require_tradable and state.get("data_stale"):
            age = format_data_age_zh(state.get("data_age_s"))
            raise RuntimeError(
                f"盯盘数据落后 {age}，正在补齐" if age else "盯盘数据落后，正在补齐")
        trend = state.get("trend_score") or {}
        # 数据质量闸: 用 data_quality_ok(行情/流动性可用性), 不再用 tradable —— 后者
        # 在 trend_score 里的注释语义是"跨标的排序地板", 被当硬闸门会莫名禁交易。
        # 老快照缺该键时回退 tradable, 保证滚动升级期间不误锁。
        quality_ok = trend.get("data_quality_ok", trend.get("tradable", False))
        if require_tradable and not quality_ok:
            raise RuntimeError("watcher 指示当前不可交易")
        current_bar = state.get("current_bar") or {}
        price = current_bar.get("last_price", current_bar.get("close"))
        if price is None:
            raise RuntimeError("watcher 实时 state 缺少价格")
        price_dec = Decimal(str(price))
        vwap = state.get("vwap")
        atr_value = state.get("atr")
        sigma = state.get("vwap_sigma")
        # 2026-09-01：这里原本还搬运 er（规则 5）、ema_dir（规则 6）、
        # macd_hist_closed/peak/shrinking（stall），四条闸删除后一并去掉。
        # 下面这四个值没有一个进闸门，只进卡片的「距 VWAP ±x σ」和拒单留痕。
        return TickerSignal(
            price=price_dec,
            vwap=Decimal(str(vwap)) if vwap is not None else price_dec,
            atr=(
                Decimal(str(atr_value))
                if atr_value is not None
                else price_dec * Decimal("0.005")
            ),
            vwap_sigma=Decimal(str(sigma)) if sigma is not None else Decimal("0"),
        )

    bars = payload.get("bars") or []
    if len(bars) < MIN_BARS:
        raise RuntimeError("分钟线不足")
    price = bars[-1].close
    vwap = session_vwap(bars)
    # 提出变量：ATR 既要进 TickerSignal，也要作 σ 的地板（下同 watcher 口径）
    atr_value = atr(bars, ATR_PERIOD)
    return TickerSignal(
        price=price,
        vwap=vwap,
        atr=atr_value,
        # σ 的 ATR 地板：与 watcher 路径同口径，两条路不得漂移（spec 2026-08-08）。
        # 2026-09-01 起它只影响卡片显示的偏离倍数，不再有闸门读它。
        vwap_sigma=sigma_with_atr_floor(
            vwap_sigma(bars), atr_value, VWAP_SIGMA_ATR_FLOOR),
    )


def market_signal(
    source,
    ticker: str,
    *,
    require_tradable: bool = True,
) -> TickerSignal:
    return signal_from_payload(
        source.market_payload(ticker),
        require_tradable=require_tradable,
    )


def _selected_from_coordinator(ticker: str, direction: str, price: Decimal):
    """协调器已选中、且 Option WS 上有新鲜盘口的合约；拿不到返回 None（回落全量选约）。

    `option_candidates` 协调器本来就为每个 (ticker, direction) 维护选中合约并把它推进
    Option WS scope（`option_candidates.py:179 / :284`）。点击路径复用它可省掉整段
    `contract_ms`（2026-08-11 实测中位 330ms、p90 3836ms、最大 6889ms）。

    **新鲜度分两级，改动前务必读懂**：
      - 合约**身份**（strike/OCC）允许旧到协调器自己的换档阈值为止（15s / 标的漂 0.5%），
        判据即 `current_selection()` 内的 `_needs_refresh()`。
      - 合约**报价**必须现取（≤1s、当前连接已 ACK 订阅）。闸门要拿 mid 算同标的持仓
        上限与资金占用，用 15 秒前的价会低估成本、把风控放松掉——这是本函数唯一不能
        妥协的地方，所以 WS 拿不到新鲜价时宁可回落去拉链，也不用协调器的旧报价。

    **fail-open**：任何一环缺失或抛错都只是「没复用」，回落到 `select_contract_full`
    （即今天的行为），绝不因此拒单。

    返回 `(合约, 未命中原因)`；命中时原因为 `None`。原因只用于埋点分类
    （08-13 夜盘 19 次点击只命中 2 次，而埋点说不出为什么），
    取值见 `OptionCandidateCoordinator.selection_miss_reason` 加上本函数的
    `no_ws_quote` / `error` 两种。
    """
    try:
        from app.option_stream import fresh_entry_quote
        from app.services.option_candidates import OPTION_CANDIDATES

        sel = OPTION_CANDIDATES.current_selection(ticker, direction, price=price)
        if sel is None:
            return None, OPTION_CANDIDATES.selection_miss_reason(
                ticker, direction, price=price)
        hit = fresh_entry_quote(sel.option_code)
        if hit is None:
            # 身份有效但盘口不新鲜 —— 这类跟 0.5%/15s 两个阈值无关，必须分开统计。
            return None, "no_ws_quote"
        bid, ask = hit
        return SelectedContract(
            sel.option_code, bid, ask,
            delta=sel.delta, delta_source=sel.delta_source,
        ), None
    except Exception as e:  # noqa: BLE001 — 复用只是加速层，出问题就当没复用
        logger.warning(json.dumps(
            {"event": "entry_contract_reuse_error", "ticker": str(ticker).upper(),
             "direction": direction, "error": str(e)[:200]}, ensure_ascii=False))
        return None, "error"


def build_market_context(source, ticker: str, direction: str) -> MarketContext:
    """组装闸门上下文，并按子步骤埋点耗时。

    埋点缘由（2026-08-11 诊断，docs/reports/2026-08-11-下单卡顿与拦截诊断.md）：
    受理→券商提交在 `53f7d78` 后由 max 2.4s 劣化到 max 14.7s，而券商侧
    submitted→filled 仅 55–500ms —— 延迟全在提交之前，但本函数各子步骤
    （signal / 选约 / 账户）此前无任何埋点，14.7 秒的窗口里日志完全静默，无从定位。

    `finally` 里出事件而非仅成功路径：慢样本多数最终是拒单
    （如 08-10 21:43:47 IONQ 耗时 7.5s 后被闸门拒），只记成功会漏掉大半证据。
    """
    if not source.signal_online():
        raise RuntimeError("盯盘引擎未启动 — 信号离线，下单已锁")
    started = time.perf_counter()
    marks = {"signal_ms": 0.0, "contract_ms": 0.0, "account_ms": 0.0}
    contract_source = "chain"
    reuse_miss: str | None = None
    try:
        step = time.perf_counter()
        signal = market_signal(source, ticker, require_tradable=True)
        marks["signal_ms"] = (time.perf_counter() - step) * 1000

        step = time.perf_counter()
        # 先看协调器有没有现成的（并且 WS 上有新鲜盘口）；没有才回落全量选约。
        selected, reuse_miss = _selected_from_coordinator(
            ticker, direction, signal.price)
        contract_source = "coordinator" if selected is not None else "chain"
        if selected is None:
            selected = select_contract_full(source, ticker, direction, signal.price)
        marks["contract_ms"] = (time.perf_counter() - step) * 1000
        contract = contract_for_validation(selected) or _PLACEHOLDER_CONTRACT

        clock = source.clock()

        step = time.perf_counter()
        account = build_account_state(source, clock.minutes_since_open)
        marks["account_ms"] = (time.perf_counter() - step) * 1000

        return MarketContext(
            signal=signal,
            account=account,
            contract=contract,
            selected=selected,
        )
    finally:
        logger.info(json.dumps({
            "event": "entry_context_timing",
            "ticker": ticker.upper(),
            "direction": direction,
            # 只看 contract_ms 分不清「复用命中所以快」和「这次拉链正好不慢」，
            # 复用层的命中率必须能从日志直接统计出来。
            "contract_source": contract_source,
            # 命中率只说明「有多糟」，说不出「该动哪个旋钮」——0.5% 漂移、15s TTL、
            # 标的池覆盖、WS 订阅，四类原因修法完全不同。命中时为 None。
            "reuse_miss": reuse_miss,
            **{key: round(value, 1) for key, value in marks.items()},
            "total_ms": round((time.perf_counter() - started) * 1000, 1),
        }, ensure_ascii=False))


def entry_plan(source, ticker: str, direction: str):
    """选约（卡片预览与 watchdog 用）。与下单路径同款复用：协调器有现成的就不拉链。"""
    signal = market_signal(source, ticker, require_tradable=True)
    reused, _miss = _selected_from_coordinator(ticker, direction, signal.price)
    if reused is not None:
        return reused
    return select_contract_full(source, ticker, direction, signal.price)


def underlying_price(source, ticker: str) -> Decimal | None:
    try:
        return market_signal(source, ticker, require_tradable=False).price
    except Exception:  # noqa: BLE001 — watchdog 获取不到现价时跳过本拍
        return None
