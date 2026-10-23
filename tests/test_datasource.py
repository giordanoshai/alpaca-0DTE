"""生产终端固定使用 WATCHER 数据源。"""
from __future__ import annotations

from app import datasource as ds
from app.services.data_source_registry import current_source


def test_production_source_is_watcher_only():
    assert current_source().name == "watcher"
    assert not hasattr(ds, "get_mode")
    assert not hasattr(ds, "set_mode")

