"""模板驱动的期权合约选择服务。"""
from __future__ import annotations

from decimal import Decimal

from app.integrations.alpaca.data import SelectedContract
from app.rules.market import spread_pct
from app.templates import get_store
from app.validator import Contract


def shortlist_contracts(
    candidates,
    min_delta,
    max_spread_pct=None,
    max_contract_cost=None,
    *,
    limit: int = 3,
) -> list[SelectedContract]:
    """按 delta、点差和成本保留少量诊断候选。"""
    if not candidates or limit <= 0:
        return []
    pool = list(candidates)
    floor_ok = [
        candidate
        for candidate in pool
        if candidate[4] is not None and candidate[4] >= min_delta
    ]
    if floor_ok:
        pool = floor_ok
    if max_spread_pct is not None:
        spread_ok = [
            candidate
            for candidate in pool
            if spread_pct(candidate[2], candidate[3]) <= max_spread_pct
        ]
        if spread_ok:
            pool = spread_ok
    if max_contract_cost is not None:
        cost_ok = [
            candidate
            for candidate in pool
            if candidate[3] * Decimal("100") <= max_contract_cost
        ]
        if cost_ok:
            pool = cost_ok
    if floor_ok:
        pool.sort(key=lambda candidate: (abs(candidate[4] - min_delta), candidate[0]))
    else:
        pool.sort(key=lambda candidate: candidate[0])
    return [
        SelectedContract(
            code,
            bid,
            ask,
            delta=delta,
            delta_source="alpaca" if delta is not None else "approx",
        )
        for _distance, code, bid, ask, delta in pool[:limit]
    ]


def pick_contract(
    candidates,
    min_delta,
    max_spread_pct=None,
    max_contract_cost=None,
) -> SelectedContract | None:
    """纯候选列表入口；返回 shortlist 第一名，供诊断和单元测试复用。"""
    shortlist = shortlist_contracts(
        candidates,
        min_delta,
        max_spread_pct,
        max_contract_cost,
        limit=1,
    )
    return shortlist[0] if shortlist else None


def select_contract_shortlist(
    source,
    ticker: str,
    direction: str,
    price: Decimal,
    *,
    limit: int = 3,
) -> list[SelectedContract]:
    store = get_store()
    template = store.active()
    try:
        template_version = store.active_id()
    except AttributeError:
        template_version = getattr(template, "name", "active")
    max_contract_cost = template.max_position_size * (
        Decimal(1) + template.position_tolerance
    )
    candidates = source.contract_candidates(
        ticker,
        direction,
        price,
        min_delta=template.min_delta,
        max_spread_pct=template.max_spread_pct,
        max_contract_cost=max_contract_cost,
        template_version=template_version,
    )
    return shortlist_contracts(
        candidates,
        template.min_delta,
        template.max_spread_pct,
        max_contract_cost,
        limit=limit,
    )


def select_contract_full(
    source,
    ticker: str,
    direction: str,
    price: Decimal,
) -> SelectedContract | None:
    shortlist = select_contract_shortlist(source, ticker, direction, price, limit=1)
    return shortlist[0] if shortlist else None


def contract_for_validation(selected: SelectedContract | None) -> Contract | None:
    if selected is None:
        return None
    # delta 原样透传，缺失时保持 None。曾经在这里兜底 0.50，结果规则 9 的
    # 「delta ≥ 0.40」恒成立，一道空转的闸对外显示「通过」（2026-08-27 修）。
    return Contract(
        delta=selected.delta,
        spread_pct=selected.spread_pct,
        mid=selected.mid,
        ask=selected.ask,
    )
