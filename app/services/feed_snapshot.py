"""行情卡片与票池快照装配。"""
from __future__ import annotations

import dataclasses
from decimal import Decimal

from app.core import market_gate
from app.order_gate import existing_exposure
from app.services.account_state import build_account_state
from app.services.market_context import market_signal, signal_from_payload
from app.templates import get_store
from app.validator import TradeRequest, ValidationInput
from app.rules.market import format_data_age_zh
from app.validator.core import validate_market

_FEED_TICKERS = ("QQQ", "IWM", "SPY")
_VWAP_DISTANCE_DISPLAY_LIMIT = Decimal("99.9")


def _format_vwap_distance(distance: Decimal) -> str:
    """格式化卡片上的 VWAP σ 距离，并限制开盘初期的无意义超大读数。

    RTH 刚开盘时样本很少，σ 可能是一个极小正数；真实闸门仍使用未截断值，
    这里只限制 UI 文案，避免几十万倍的数字撑坏卡片。
    """
    if distance > _VWAP_DISTANCE_DISPLAY_LIMIT:
        return "≥99.9"
    if distance < -_VWAP_DISTANCE_DISPLAY_LIMIT:
        return "≤−99.9"
    return f"{float(distance):+.1f}"


def _pool_meta(state: dict | None) -> dict:
    """票池行的展示数据。

    **2026-09-01 起只剩数据质量一个字段。** 卡片上的方向标签（上涨/下跌/震荡）与
    效率比数字随规则 5、规则 6 一并删除 —— 那两格原本是闸门判据的镜像，闸门没了
    再显示就成了「系统建议」，而系统现在对方向不做任何判断，买 CALL 还是 PUT 完全
    由用户在下单面板上选。

    缺键 fail-closed（质量 False）：宁可全灰, 不可让一个不存在的状态被渲染成"可买"。
    """
    st = state or {}
    trend = st.get("trend_score") or {}
    # 数据落后时质量一律置假（2026-08-28）：按钮此时被 _dry_gates 灰掉了，卡片
    # 再写「可买入」就是本项目反复踩的「卡片与按钮说两套话」。
    quality = bool(trend.get("data_quality_ok", False)) and not st.get("data_stale")

    return {
        "tradable": quality,                  # 旧键保留, 前端灰度期间仍在读
        "data_quality_ok": quality,
    }


def _order_pool(
    tickers: list[str],
    last_signal_ts: dict[str, float],
) -> list[str]:
    def key(ticker: str):
        if ticker in last_signal_ts:
            return (0, -last_signal_ts[ticker])
        return (1, 0.0)

    return sorted(tickers, key=key)


def _frame_config(states: dict):
    config = get_store().active().to_rule_config()
    whitelist = tuple(
        ticker.upper() for ticker in states if not ticker.startswith("^")
    )
    return dataclasses.replace(config, whitelist=whitelist)


def _dry_gates(source, symbol: str, state: dict, config, account) -> dict | None:
    """每票 CALL/PUT 各干跑一次闸门，返回 {"long": {...}, "short": {...}}。

    **2026-09-01 起两侧结果恒等**：震荡、追高、方向闸、动能闸四条闸删除后，
    `validate_market` 里没有一条规则读 `req.direction`。仍按两个方向各跑一遍 ——
    通道保留，方向相关性以后要是回来这里不必再改（2026-08-14 到 08-21 之间也
    正好演过一次「恒等 → 又不恒等」）。

    本函数自己**不做任何判断**：震荡拦截、方向判定这些一律只在 validator 里，
    这里多加一条就是第二套判据 —— 2026-08-08 那次「闸门放行了、卡片却显示横盘
    震荡、买入按钮是灰的」就是这么来的。
    """
    try:
        # 数据落后（2026-08-28）：**排在质量闸之前**，理由同 market_context 那处 ——
        # 数据没跟上时说「暂不可交易」会把人引去查行情。rule 名独立成 "stale"：
        # 拦截日志按 rule 分类统计，混进 watcher/chop 会污染历史口径。
        if state.get("data_stale"):
            age = format_data_age_zh(state.get("data_age_s"))
            blocked = {"blocked": True, "rule": "stale",
                       "reason": f"盯盘：数据落后 {age}，补齐中" if age
                                 else "盯盘：数据落后，补齐中"}
            return {"long": dict(blocked), "short": dict(blocked)}
        trend = state.get("trend_score") or {}
        if not trend.get("data_quality_ok", False):
            blocked = {"blocked": True, "rule": "watcher", "reason": "盯盘：暂不可交易"}
            return {"long": dict(blocked), "short": dict(blocked)}
        signal = signal_from_payload(
            {"kind": "watcher", "state": state},
            require_tradable=True,
        )
        exposure = existing_exposure(symbol)
        gates: dict[str, dict] = {}
        for direction in ("long", "short"):
            rejection = validate_market(ValidationInput(
                request=TradeRequest(
                    ticker=symbol.upper(),
                    direction=direction,
                    qty=1,
                ),
                signal=signal,
                account=account,
                config=config,
                existing_exposure=exposure,
            ))
            gates[direction] = (
                {"blocked": False, "rule": "", "reason": ""}
                if rejection is None
                else {"blocked": True, "rule": rejection.rule, "reason": rejection.reason}
            )
        return gates
    except Exception:  # noqa: BLE001 — 干跑仅用于展示
        return None


def _live_snapshot(source) -> dict:
    ticks = []
    for symbol in _FEED_TICKERS:
        try:
            signal = market_signal(source, symbol, require_tradable=False)
        except Exception:  # noqa: BLE001
            continue
        distance = (
            (signal.price - signal.vwap) / signal.vwap_sigma
            if signal.vwap_sigma
            else Decimal(0)
        )
        tick = {
            "sym": symbol,
            # 非 watcher 源(live/db)根本不算 trend_score, 这里此前把 VWAP σ 距离
            # 填进 `trend` —— 卡片上"趋势 +2.60"其实是"距 VWAP 2.6σ", 与右边的
            # "VWAP 2.6×σ"是同一个数, 纯属误导。没有就报没有。
            "trend": "—",
            "vwap": _format_vwap_distance(distance),
            "price": str(signal.price),
        }
        ticks.append(tick)
    return {"hbMs": 0, "signal_online": source.signal_online(), "ticks": ticks}


def build_feed_snapshot(source) -> dict:
    if source.name != "watcher":
        return _live_snapshot(source)

    online = source.signal_online()
    states = source.watcher_states()
    tickers = list(dict.fromkeys([*states.keys(), *_FEED_TICKERS]))
    last_signal_ts = source.last_signal_timestamps()
    pool_order = _order_pool(tickers, last_signal_ts)
    last_signal = None
    if last_signal_ts:
        symbol = max(last_signal_ts, key=last_signal_ts.get)
        last_signal = {"sym": symbol, "ts": last_signal_ts[symbol]}

    gate_context = None
    try:
        gate_context = (
            _frame_config(states),
            build_account_state(source, source.clock().minutes_since_open),
        )
    except Exception:  # noqa: BLE001
        pass

    ticks = []
    for symbol in pool_order:
        state = states.get(symbol) or {}
        # 2026-08-26 趋势分下线：**这里不再下发 `trend` 键**。
        # 2026-09-01 效率比 `er` 也不再下发（规则 5 删除，卡片那一格随之撤掉）。
        tick = {"sym": symbol}
        if gate_context is not None and not symbol.startswith("^"):
            gates = _dry_gates(source, symbol, state, *gate_context)
            if gates is not None:
                tick["gates"] = gates
        try:
            signal = signal_from_payload(
                {"kind": "watcher", "state": state},
                require_tradable=False,
            )
        except Exception:  # noqa: BLE001
            signal = None
        if signal is not None:
            tick["price"] = str(signal.price)
            if signal.vwap_sigma:
                distance = (signal.price - signal.vwap) / signal.vwap_sigma
                tick["vwap"] = _format_vwap_distance(distance)
        ticks.append(tick)

    return {
        "hbMs": 0,
        "signal_online": online,
        # 非盘中行情循环整体挂起（见 app/core/market_gate.py），卡片会冻在收盘时
        # 的值。必须显式告诉前端，否则看的人分不清盯的是实时价还是几小时前的
        # 残影 —— 和 tide 徽章标「已收盘」是同一个用意。
        "market_session": market_gate.session_state(),
        "ticks": ticks,
        "pool": [
            {"sym": ticker, **_pool_meta(states.get(ticker))}
            for ticker in pool_order
        ],
        "last_signal": last_signal,
    }
