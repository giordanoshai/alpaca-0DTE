"""规则模板：数据库一行一配置、保存/加载/删除分离。"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal as D

import pytest

import app.templates as T
from app.templates import Template, TemplateStore

# 2026-07-15 周三：12:00 UTC = 08:00 ET 盘前；15:00 UTC = 11:00 ET 盘中
_PREMARKET = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
_INTRADAY = datetime(2026, 7, 15, 15, 0, tzinfo=timezone.utc)


def tpl(name: str, stop_pct: str = "0.20") -> Template:
    return Template(
        name=name,
        max_daily_loss=D("150"), max_weekly_loss=D("300"), max_position_size=D("500"),
        opening_blackout_min=D("45"), min_delta=D("0.40"), max_spread_pct=D("0.03"),
        stop_pct=D(stop_pct), max_loss_per_trade=D("80"), max_hold_mins=D("15"),
        cooldown_seconds=600,
    )


def store() -> TemplateStore:
    meta = {
        "默认": {"id": 1, "reason": "系统默认", "created_at": "2026-01-01T00:00:00+00:00",
                 "updated_at": "2026-01-01T00:00:00+00:00"},
        "保守": {"id": 2, "reason": "系统默认", "created_at": "2026-01-02T00:00:00+00:00",
                 "updated_at": "2026-01-02T00:00:00+00:00"},
    }
    return TemplateStore(
        {"默认": tpl("默认"), "保守": tpl("保守", "0.15")},
        active_name="默认",
        record_meta=meta,
    )


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (datetime(2026, 7, 15, 13, 29, tzinfo=timezone.utc), False),  # 09:29 ET
        (datetime(2026, 7, 15, 13, 30, tzinfo=timezone.utc), True),   # 09:30 ET
        (datetime(2026, 7, 15, 19, 59, tzinfo=timezone.utc), True),   # 15:59 ET
        (datetime(2026, 7, 15, 20, 0, tzinfo=timezone.utc), False),   # 16:00 ET
        (datetime(2026, 7, 18, 15, 0, tzinfo=timezone.utc), False),   # 周六
    ],
)
def test_market_hours_lock_boundaries(now, expected):
    assert T.is_market_hours(now) is expected


def test_switch_premarket_is_immediate():
    s = store()
    assert s.switch("保守", frozen=False) == "active"
    assert s.active_name() == "保守"
    assert s.pending_name() is None


def test_switch_intraday_goes_pending_then_rolls():
    s = store()
    assert s.switch("保守", frozen=True) == "pending"
    assert s.active_name() == "默认"
    assert s.pending_name() == "保守"
    assert s.roll_session() is True
    assert s.active_name() == "保守"
    assert s.pending_name() is None


def test_records_are_one_template_per_row():
    rows = store().records()
    assert {row["id"] for row in rows} == {1, 2}
    assert {row["name"] for row in rows} == {"默认", "保守"}
    assert all("params" in row and "templates" not in row for row in rows)
    assert all("whitelist" not in row["params"] for row in rows)
    assert next(row for row in rows if row["id"] == 1)["status"] == "active"


def test_store_records_roundtrip_preserves_active_and_pending():
    s = store()
    s.switch("保守", frozen=True)
    restored = T._store_from_records(s.records())
    assert restored.active_name() == "默认"
    assert restored.pending_name() == "保守"
    assert restored.get("保守").stop_pct == D("0.15")


def test_payload_contains_only_one_configuration():
    row = next(row for row in store().records() if row["id"] == 1)
    payload = T._payload_for_record(row)
    assert set(payload) == {"params", "reason", "status", "created_at"}
    assert "templates" not in payload
    assert "name" not in payload["params"]
    assert "whitelist" not in payload["params"]
    assert payload["params"]["stop_pct"] == "0.20"


def test_load_reads_database_rows(monkeypatch):
    rows = store().records()
    rows[0]["status"] = "saved"
    rows[1]["status"] = "active"
    monkeypatch.setattr(T, "_db_load_records", lambda: rows)
    assert T._load_store().active_name() == rows[1]["name"]


def test_database_failure_does_not_fall_back_to_local_cache(monkeypatch):
    monkeypatch.setattr(T, "_db_load_records", lambda: None)
    with pytest.raises(T.TemplatePersistenceError, match="不使用本地配置缓存"):
        T._load_store()


def test_empty_database_is_not_auto_seeded(monkeypatch):
    monkeypatch.setattr(T, "_db_load_records", lambda: [])
    with pytest.raises(T.TemplatePersistenceError, match="至少一份配置"):
        T._load_store()


def test_to_rule_config_carries_thresholds():
    cfg = tpl("默认").to_rule_config()
    assert cfg.max_daily_loss == D("150")
    assert cfg.max_weekly_loss == D("300")
    assert cfg.whitelist == ()

def test_rejects_unknown_template_and_duplicate_name():
    s = store()
    with pytest.raises(KeyError):
        s.switch("不存在", frozen=False)
    with pytest.raises(ValueError):
        s.add(3, tpl("默认"), reason="重复")
