"""生产数据源装配点。"""
from __future__ import annotations

from app.datasource.mode import configure_source, current_source as _current_source
from app.datasource.watcher import WatcherDataSource
from app.watcher.watcher_manager import GLOBAL_WATCHER

configure_source(WatcherDataSource(GLOBAL_WATCHER))


def current_source():
    return _current_source()
