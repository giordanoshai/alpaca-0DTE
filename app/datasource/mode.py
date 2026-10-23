"""数据源引用容器；具体生产装配由上层服务完成。"""
from __future__ import annotations

from app.datasource._common import DataSource

_SOURCE: DataSource | None = None


def configure_source(source: DataSource) -> None:
    global _SOURCE
    _SOURCE = source


def current_source() -> DataSource:
    if _SOURCE is None:
        raise RuntimeError("生产数据源尚未装配")
    return _SOURCE
