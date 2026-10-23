"""下单闸门服务 —— 唯一下单通道（README 铁律）。

UI、快捷键、未来的自动信号，全部只把 (ticker, direction) 交给这里；
数量/价格类型/strike 由程序算，外部无从越权。evaluate() 组装当前市场/
账户/合约状态 → validator.validate() → 通行证(ValidationResult)。

市场、账户与合约上下文由当前 WATCHER 数据源提供。

本模块同时内聚：动态白名单注入（user_watchlist）、生效配置组装（active_config）、
以及下单闸门主入口 gate_order（含数据源不可用→拒单的优雅降级 + 拦截日志）。
"""
from __future__ import annotations

import dataclasses
import json
import logging
from decimal import Decimal
from typing import Callable

from app import trade_recorder
from app.config import default_rule_config
from app.services.market_context import build_market_context
from app.services.market_context_types import MarketContext
from app.validator import (
    AccountState,
    Contract,
    RuleConfig,
    TickerSignal,
    TradeRequest,
    ValidationInput,
    ValidationResult,
    validate,
)

from app.core.dailylogger import get_logger

logger = get_logger("tt.gate")

_LONG = {"long", "l", "call", "做多", "buy"}
_SHORT = {"short", "s", "put", "做空", "sell"}


def normalize_direction(raw: str) -> str:
    key = raw.strip().lower()
    if key in _LONG:
        return "long"
    if key in _SHORT:
        return "short"
    raise ValueError(f"无法识别的方向: {raw!r}")


Provider = Callable[[str, str], MarketContext]


def existing_exposure(ticker: str) -> Decimal:
    """同标的**已占用**的仓位金额（入场成本口径），供 validator 规则 8 合并计算。

    计入 REGISTRY.active()（pending/open/closing）—— pending 是已提交未成交，这笔钱
    已经承诺出去了；不区分 CALL/PUT，同一只票上的钱都是这只票的敞口（跨式同样占额度）。
    取数失败 → 0（降级为旧的"只看本单"行为，绝不因登记异常把下单链路打死）。

    放在 order_gate 而非 datasource：交易层本来就该知道持仓，而数据接入层 import
    order_state 是审查第四节点名的反向依赖，不能再加深。
    """
    try:
        from app.position_registry import REGISTRY
        t = ticker.upper()
        return sum((p.entry_val for p in REGISTRY.active() if p.ticker.upper() == t), Decimal(0))
    except Exception as e:  # noqa: BLE001
        logger.warning(json.dumps({"event": "existing_exposure_error", "ticker": ticker,
                                   "error": str(e)}, ensure_ascii=False))
        return Decimal(0)


def evaluate_with_context(
    ticker: str,
    direction: str,
    *,
    provider: Provider,
    config: RuleConfig | None = None,
    qty: int = 1,
) -> tuple[ValidationResult, MarketContext]:
    """闸门评估，同时回传评估用的 MarketContext（拦截快照与决策同源，不重复取数）。"""
    cfg = config or default_rule_config()
    direction = normalize_direction(direction)
    ctx = provider(ticker, direction)
    inp = ValidationInput(
        request=TradeRequest(ticker=ticker.upper(), direction=direction, qty=qty),
        signal=ctx.signal,
        account=ctx.account,
        config=cfg,
        contract=ctx.contract,
        existing_exposure=existing_exposure(ticker),
    )
    return validate(inp), ctx


def evaluate(
    ticker: str,
    direction: str,
    *,
    provider: Provider,
    config: RuleConfig | None = None,
    qty: int = 1,
) -> ValidationResult:
    """闸门评估：组 ValidationInput → validate()。方向自动归一化。

    provider(ticker, direction) 返回当前市场/账户/合约；direction 让 live 源选 CALL/PUT。
    qty 为用户输入张数（⑥），validator 守上限；不传默认 1 张。
    """
    result, _ = evaluate_with_context(ticker, direction, provider=provider, config=config, qty=qty)
    return result


def _get_dynamic_whitelist():
    """交易白名单 = watcher 内存票池（正在盯的标的，排除 ^ 前缀指数）。

    不再每次 gate 打 DB —— 用户拍板「在盯盘票池里即白名单」（2026-07-23）。
    空池/取不到 → 空 tuple（fail-closed），绝不回退到模板里的历史固定票池。
    """
    try:
        from app.watcher.watcher_manager import GLOBAL_WATCHER
        states = GLOBAL_WATCHER.get_all_states()
        return tuple(t.upper() for t in states if not t.startswith("^"))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"获取动态白名单失败: {e}")
        return ()


def active_config():
    """当前生效模板 → RuleConfig，并始终用 WATCHER 当前票池覆盖交易白名单。"""
    from app.templates import get_store
    cfg = get_store().active().to_rule_config()
    return dataclasses.replace(cfg, whitelist=_get_dynamic_whitelist())


def gate_order(source, ticker: str, direction: str, qty: int) -> dict:
    """④ 闸门评估：过 validator → {allowed,reason,rule,qty}；数据源不可用优雅降级为拒单。

    拒单事件（含降级）落 ttt_intercept_log（best-effort）；放行不留痕。
    """
    try:
        result, ctx = evaluate_with_context(
            ticker,
            direction,
            provider=lambda symbol, side: build_market_context(source, symbol, side),
            config=active_config(),
            qty=qty,
        )
    except Exception as e:  # noqa: BLE001 — 数据源不可用/拦截 → 拒单而非 500
        logger.warning(json.dumps({"event": "order_gate_error", "source": source.name, "error": str(e)}, ensure_ascii=False))
        trade_recorder.record_intercept(
            rule="datasource", reason=f"数据源({source.name})不可用: {e}",
            ticker=ticker.upper(), direction=None, qty=qty, source_name=source.name,
            market_snapshot=None, template_snapshot=trade_recorder.template_snapshot())
        return {"allowed": False, "reason": f"数据源({source.name})不可用: {e}", "rule": "datasource", "qty": 0}
    logger.info(json.dumps({
        "event": "order_gate", "source": source.name,
        "ticker": ticker.upper(), "direction": normalize_direction(direction),
        "allowed": result.allowed, "rule": result.rule, "reason": result.reason, "qty": result.qty,
    }, ensure_ascii=False))
    if not result.allowed:
        trade_recorder.record_intercept(
            rule=result.rule, reason=result.reason,
            ticker=ticker.upper(), direction=normalize_direction(direction), qty=qty,
            source_name=source.name, market_snapshot=trade_recorder.market_snapshot(ctx),
            template_snapshot=trade_recorder.template_snapshot())
    return {"allowed": result.allowed, "reason": result.reason, "rule": result.rule, "qty": result.qty}
