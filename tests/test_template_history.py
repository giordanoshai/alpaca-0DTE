"""配置行的保存、加载与删除行为。"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal as D

import pytest

import app.templates as tmpl


def _premarket() -> datetime:
    return datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)


def _intraday() -> datetime:
    return datetime(2026, 7, 20, 14, 0, tzinfo=timezone.utc)


def test_save_creates_independent_row_without_loading():
    before_name = tmpl.get_store().active_name()
    before_stop = tmpl.get_store().active().stop_pct
    result = tmpl.save_template(
        "收紧止损", {"stop_pct": "0.25"}, reason="回测后调整", now=_premarket(),
    )
    assert result["status"] == "saved"
    assert result["template"]["name"] == "收紧止损"
    assert result["template"]["params"]["stop_pct"] == "0.25"
    assert tmpl.get_store().active_name() == before_name
    assert tmpl.get_store().active().stop_pct == before_stop


def test_save_writes_each_template_as_a_separate_record(monkeypatch):
    tmpl.get_store()
    seen = []
    monkeypatch.setattr(tmpl, "_db_save_records", lambda rows: seen.extend(rows) or True)
    tmpl.save_template("新配置", {"stop_pct": "0.26"}, reason="标定", now=_premarket())
    assert len(seen) == 2
    assert all("params" in row and "templates" not in row for row in seen)
    assert next(row for row in seen if row["name"] == "新配置")["params"]["stop_pct"] == "0.26"


def test_save_requires_unique_name_and_rejects_iron_law_key():
    tmpl.save_template("唯一名称", {"stop_pct": "0.25"}, reason="第一次", now=_premarket())
    with pytest.raises(ValueError, match="已存在"):
        tmpl.save_template("唯一名称", {"stop_pct": "0.30"}, reason="重复", now=_premarket())
    with pytest.raises(KeyError):
        tmpl.save_template("非法配置", {"max_position_size": "1000"}, reason="越权", now=_premarket())


def test_load_premarket_applies_full_snapshot():
    saved = tmpl.save_template(
        "盘前加载", {"stop_pct": "0.25", "max_hold_mins": "20"},
        reason="确认采用", now=_premarket(),
    )
    result = tmpl.load_template(saved["template"]["id"], now=_premarket())
    assert result["status"] == "active"
    assert tmpl.get_store().active_name() == "盘前加载"
    assert tmpl.get_store().active().stop_pct == D("0.25")
    assert tmpl.get_store().active().max_hold_mins == D("20")


def test_load_intraday_is_locked_without_pending_switch():
    saved = tmpl.save_template(
        "盘前已保存", {"stop_pct": "0.25"}, reason="候选配置", now=_premarket(),
    )
    before = tmpl.get_store().active_name()
    with pytest.raises(tmpl.TemplateLockedError, match="盘中不可加载"):
        tmpl.load_template(saved["template"]["id"], now=_intraday())
    assert tmpl.get_store().active_name() == before
    assert tmpl.get_store().pending_name() is None


def test_save_and_delete_are_available_intraday():
    saved = tmpl.save_template(
        "盘中候选", {"stop_pct": "0.25"}, reason="盘中调整", now=_intraday(),
    )
    result = tmpl.delete_template(saved["template"]["id"], now=_intraday())
    assert result["deleted"] is True
    assert "盘中候选" not in tmpl.get_store().list_names()


def test_delete_saved_row():
    saved = tmpl.save_template("待删除", {"stop_pct": "0.25"}, reason="临时", now=_premarket())
    result = tmpl.delete_template(saved["template"]["id"], now=_premarket())
    assert result == {"deleted": True, "id": saved["template"]["id"], "name": "待删除"}
    assert "待删除" not in tmpl.get_store().list_names()


def test_delete_blocks_active_and_pending_rows():
    store = tmpl.get_store()
    with pytest.raises(ValueError, match="不能删除"):
        tmpl.delete_template(store.active_id(), now=_premarket())


def test_persistence_failure_rolls_back_save_and_load(monkeypatch):
    store = tmpl.get_store()
    before_names = store.list_names()
    monkeypatch.setattr(tmpl, "_db_save_records", lambda rows: False)
    with pytest.raises(tmpl.TemplatePersistenceError):
        tmpl.save_template("写库失败", {"stop_pct": "0.25"}, reason="失败", now=_premarket())
    assert store.list_names() == before_names

    # 加载失败也必须恢复当前状态。
    monkeypatch.setattr(tmpl, "_db_save_records", lambda rows: True)
    saved = tmpl.save_template("可保存", {"stop_pct": "0.25"}, reason="成功", now=_premarket())
    before_active = store.active_name()
    monkeypatch.setattr(tmpl, "_db_save_records", lambda rows: False)
    with pytest.raises(tmpl.TemplatePersistenceError):
        tmpl.load_template(saved["template"]["id"], now=_premarket())
    assert store.active_name() == before_active
