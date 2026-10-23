"""③ 选合约纯逻辑：delta 随候选自 Alpaca 快照带出，pick 优先 delta≥min 中最贴平值者。

delta 口径：PUT 取绝对值（在 contract_candidates 组装时已 abs），故此处一律按正幅度比较。
feed 未填 greeks → delta=None → 回退最贴平值近似（delta_source=approx，原行为）。
"""
from decimal import Decimal

from app.integrations.alpaca.data import SelectedContract
from app.services.contract_selection import (
    contract_for_validation,
    pick_contract,
    shortlist_contracts,
)

D = Decimal
# (dist, code, bid, ask, delta) 按平值距离升序；delta 已由 contract_candidates 从 snap.greeks 取
CANDS = [
    (0.5, "QQQ260716C00500000", D("1.00"), D("1.02"), D("0.38")),
    (1.5, "QQQ260716C00499000", D("1.50"), D("1.53"), D("0.47")),
    (2.5, "QQQ260716C00498000", D("2.00"), D("2.04"), D("0.55")),
]


def test_pick_nearest_meeting_min_delta():
    """最贴平值者 delta=0.38<0.40 不达标，取下一个 0.47≥0.40。"""
    sel = pick_contract(CANDS, D("0.40"))
    assert sel.option_code == "QQQ260716C00499000"
    assert sel.delta == D("0.47") and sel.delta_source == "alpaca"


def test_pick_none_meets_returns_nearest_with_true_delta():
    """全不达标 → 最贴平值(带其真 delta，rule9 将以真值拒)。"""
    cands = [(0.5, "QQQ260716C00500000", D("1.00"), D("1.02"), D("0.38"))]
    sel = pick_contract(cands, D("0.40"))
    assert sel.option_code == "QQQ260716C00500000"
    assert sel.delta == D("0.38") and sel.delta_source == "alpaca"


def test_pick_delta_missing_falls_back_approx():
    """greeks 未填(indicative feed) → delta=None → 最贴平值近似。"""
    cands = [(0.5, "QQQ260716C00500000", D("1.00"), D("1.02"), None),
             (1.5, "QQQ260716C00499000", D("1.50"), D("1.53"), None)]
    sel = pick_contract(cands, D("0.40"))
    assert sel.option_code == "QQQ260716C00500000"
    assert sel.delta is None and sel.delta_source == "approx"


def test_pick_empty_cands_returns_none():
    assert pick_contract([], D("0.40")) is None


# ── 2026-07-23 簇A：选 delta 最贴近 min_delta 者，而非达标里最贴平值（bug #7）───────

def test_pick_targets_min_delta_not_atm():
    """平值(dist最小)候选 delta=0.50 已达标，但更贴近 min_delta=0.40 的是 dist=1.5 那档(0.41)；
    旧逻辑会直接返回平值 0.50，新逻辑应选 0.41。"""
    cands = [
        (0.5, "QQQ260716C00500000", D("1.00"), D("1.02"), D("0.50")),
        (1.5, "QQQ260716C00499000", D("1.50"), D("1.53"), D("0.41")),
        (2.5, "QQQ260716C00498000", D("2.00"), D("2.04"), D("0.35")),
    ]
    sel = pick_contract(cands, D("0.40"))
    assert sel.option_code == "QQQ260716C00499000"
    assert sel.delta == D("0.41")


def test_pick_prefers_candidate_within_spread_when_target_too_wide():
    """达标候选里，最贴近 min_delta 的那档 spread 超阈值（会被规则9拒）；另一档虽然 delta 略
    偏离目标，但 spread 合格 —— 应选 spread 合格的那档，而不是选一个必被下游拒掉的合约（bug #3）。"""
    cands = [
        (0.5, "QQQ260716C00500000", D("1.00"), D("1.02"), D("0.50")),      # spread ~2%，达标但delta偏离
        (1.5, "QQQ260716C00499000", D("1.00"), D("1.10"), D("0.41")),     # 最贴近 target 但 spread ~9.5% 超阈值
        (2.5, "QQQ260716C00498000", D("0.90"), D("0.94"), D("0.36")),     # 不达标(delta<0.40)
    ]
    sel = pick_contract(cands, D("0.40"), max_spread_pct=D("0.03"))
    assert sel.option_code == "QQQ260716C00500000"
    assert sel.delta == D("0.50")


def test_pick_falls_back_to_delta_only_when_no_candidate_meets_spread():
    """达标候选里没有一个满足 spread 阈值 → 退回只看 delta 最贴近（诊断真实，仍会被规则9拒）。"""
    cands = [
        (0.5, "QQQ260716C00500000", D("1.00"), D("1.20"), D("0.50")),   # spread 大, 达标
        (1.5, "QQQ260716C00499000", D("1.00"), D("1.15"), D("0.41")),   # spread 大, 达标, 更贴近target
    ]
    sel = pick_contract(cands, D("0.40"), max_spread_pct=D("0.03"))
    assert sel.option_code == "QQQ260716C00499000"
    assert sel.delta == D("0.41")


def test_to_contract_passes_delta_through_including_none():
    with_true = SelectedContract("X", D("1.00"), D("1.02"), delta=D("0.47"), delta_source="alpaca")
    assert contract_for_validation(with_true).delta == D("0.47")
    assert contract_for_validation(with_true).ask == D("1.02")
    # greeks 缺失时不再兜底 0.50：那个假值恒 ≥0.40，会把空转的规则 9 伪装成「通过」。
    # 保持 None，规则 9 才能把它标成「未评估」（2026-08-27 修）。
    approx = SelectedContract("X", D("1.00"), D("1.02"))
    assert contract_for_validation(approx).delta is None
    assert approx.delta_source == "approx"


def test_shortlist_keeps_three_in_memory_but_filters_spread_and_cost():
    cands = [
        (0.5, "A", D("1.00"), D("1.02"), D("0.42")),
        (1.0, "B", D("1.10"), D("1.13"), D("0.43")),
        (1.5, "C", D("5.00"), D("7.00"), D("0.44")),  # spread/cost 均不合格
        (2.0, "D", D("1.20"), D("1.24"), D("0.45")),
        (2.5, "E", D("1.30"), D("1.34"), D("0.46")),
    ]

    shortlist = shortlist_contracts(
        cands,
        D("0.40"),
        max_spread_pct=D("0.04"),
        max_contract_cost=D("600"),
        limit=3,
    )

    assert [item.option_code for item in shortlist] == ["A", "B", "D"]
