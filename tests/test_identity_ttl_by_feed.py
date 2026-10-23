# -*- coding: utf-8 -*-
"""identity TTL 按 option feed 分档：拿不到 greeks 时，定时重拉链买不到任何东西。

清单 §3（docs/reports/2026-08-14-未修缺陷清单.md）：66 槽 × `_IDENTITY_TTL_S`(15s)
→ 160–264 次选约/分钟，而 Alpaca 额度 **200/min**，结构性超限 → 选约被拖到 3.5s、
扫完一轮 ≈58s ≫ 15s TTL → identity 永远是过期的，点击必然回落全量拉链
（08-14 重启后 7/7 次点击 `miss=identity_stale`，复用命中率 **0%**）。

清单说这个定时器只买两样东西：**盘口新鲜度**（bid/ask 已全走 WS，纯属白花）与
**delta 漂移**（规则 9 要用），并要求「拉到多少需先离线标定 delta 漂移，不盘中试」。

**但在 indicative 档下第二样也是零**：Alpaca 免费档不填 greeks（`data.py:331`），
`contract_selection.py:57` 于是恒判 `delta_source="approx"`（`log/ttt.events.jsonl`
实测 23/23 全是 approx、零条 alpaca），`_candidate_eligible`（`data.py:552`）因
`delta is not None` 短路而整条跳过 delta 判据。**重拉一次链拿回来的 delta 还是 None** ——
所以此档下拉长 TTL 是纯收益，不需要任何标定。

签下 OPRA 后 delta 变真，TTL 必须回到 15s 原值等标定，故**按 feed 分档而不是直接调大**。
`test_coordinator_miss_reason.py` 的「盲调阈值是最坏的做法」在此仍然成立：
动的不是判据本身，而是「该判据在无 greeks 档位下本就买不到东西」。
"""
from __future__ import annotations

import time
from decimal import Decimal as D

from app.integrations.alpaca.data import SelectedContract
from app.services.option_candidates import (
    _IDENTITY_TTL_S,
    OptionCandidateCoordinator,
    identity_ttl_s,
)

_CODE = "APLD260814C00030000"


def _pin_feed(monkeypatch, feed: str):
    monkeypatch.setattr("app.config.ALPACA_OPTION_FEED", feed)


def _coord(*, price=D("100"), age_s=0.0):
    coord = OptionCandidateCoordinator()
    state = coord._sides("APLD")["long"]
    # 无 greeks 档的真实形态：delta=None / delta_source="approx"
    state.selected = SelectedContract(
        _CODE, D("1.00"), D("1.04"), delta=None, delta_source="approx")
    state.shortlist = [state.selected]
    state.anchor_price = price
    state.template_version = "tpl-1"
    state.last_discovery_at = time.monotonic() - age_s
    coord._template_version = lambda: "tpl-1"
    return coord


def test_opra档保持15秒原值不动(monkeypatch):
    """delta 变真之后，判据必须回到原样等标定 —— 这条防止改动被当成"永久放宽"。"""
    _pin_feed(monkeypatch, "opra")
    assert identity_ttl_s() == _IDENTITY_TTL_S


def test_indicative档放宽到分钟级(monkeypatch):
    _pin_feed(monkeypatch, "indicative")
    assert identity_ttl_s() >= 60.0


def test_indicative下超过15秒不再报identity_stale(monkeypatch):
    """正是这一条把 08-14 的 0% 复用命中率救回来：扫描周期 58s 不再必然踩过 TTL。"""
    _pin_feed(monkeypatch, "indicative")
    coord = _coord(age_s=_IDENTITY_TTL_S + 5)
    assert coord.selection_miss_reason("APLD", "long", price=D("100")) == "hit"


def test_opra下超过15秒仍报identity_stale(monkeypatch):
    _pin_feed(monkeypatch, "opra")
    coord = _coord(age_s=_IDENTITY_TTL_S + 5)
    assert coord.selection_miss_reason("APLD", "long", price=D("100")) == "identity_stale"


def test_indicative下价格漂移仍然换约(monkeypatch):
    """放宽的只是定时器。行权价该不该换由 `_PRICE_MOVE_RATIO`(0.5%) 覆盖，不得被削弱。"""
    _pin_feed(monkeypatch, "indicative")
    coord = _coord(price=D("100"), age_s=1.0)
    assert coord.selection_miss_reason("APLD", "long", price=D("100.6")) == "price_moved"


def test_indicative下换模板仍然重选(monkeypatch):
    _pin_feed(monkeypatch, "indicative")
    coord = _coord(age_s=1.0)
    coord._template_version = lambda: "tpl-2"
    assert coord.selection_miss_reason("APLD", "long", price=D("100")) == "template_changed"
