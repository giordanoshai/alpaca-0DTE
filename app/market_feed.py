"""行情共享快照运行器：单生产者，多消费者。

本层只负责任务调度和快照原子替换。生产装配由 ``services.market_feed`` 注入
数据源提供器与业务快照构造器；未注入时保留旧数据源 ``feed_snapshot`` 兼容路径。
"""
from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable

from app import datasource
from app.core.dailylogger import get_logger

logger = get_logger("tt.market_feed")


def _empty_snapshot(source: str | None = None) -> dict:
    return {
        "hbMs": 9999,
        "signal_online": False,
        "ticks": [],
        "pool": [],
        "last_signal": None,
        "source": source,
        "stale": True,
        "updated_at": None,
        "feed_error": "行情快照尚未就绪",
    }


def _legacy_snapshot_builder(source) -> dict:
    return source.feed_snapshot()


class MarketFeedHub:
    """全进程唯一行情快照生产者。"""

    def __init__(
        self,
        *,
        interval_s: float = 1.0,
        source_provider: Callable[[], object] | None = None,
        snapshot_builder: Callable[[object], dict] | None = None,
    ) -> None:
        if interval_s <= 0:
            raise ValueError("MarketFeedHub interval 必须大于 0")
        self.interval_s = interval_s
        self._source_provider = source_provider or datasource.current_source
        self._snapshot_builder = snapshot_builder or _legacy_snapshot_builder
        self._snapshot = _empty_snapshot()
        self._source_name: str | None = None
        self._generation = 0

    @property
    def generation(self) -> int:
        return self._generation

    def snapshot(self) -> dict:
        """返回当前原子快照引用；调用方不得原地修改。"""
        return self._snapshot

    async def refresh_once(self) -> bool:
        """刷新一次。成功返回 True；失败保留安全快照并返回 False。"""
        source = self._source_provider()
        source_name = str(getattr(source, "name", type(source).__name__))
        source_changed = source_name != self._source_name
        if source_changed:
            self._source_name = source_name
            self._snapshot = _empty_snapshot(source_name)

        try:
            raw = await asyncio.to_thread(self._snapshot_builder, source)
            current = self._source_provider()
            if current is not source:
                # 取数期间切源：结果属于旧源，绝不发布。
                self._snapshot = _empty_snapshot(
                    str(getattr(current, "name", "unknown"))
                )
                return False
            self._snapshot = {
                **raw,
                "source": source_name,
                "stale": False,
                "updated_at": time.time(),
                "feed_error": None,
            }
            self._generation += 1
            return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — 保留上一成功帧，显式 stale
            self._snapshot = {
                **self._snapshot,
                "source": source_name,
                "stale": True,
                "feed_error": str(exc),
            }
            logger.warning(json.dumps(
                {
                    "event": "market_feed_refresh_error",
                    "source": source_name,
                    "error": str(exc),
                },
                ensure_ascii=False,
            ))
            return False

    async def run(self) -> None:
        """生命周期后台循环；由 ``app.main`` 启停。"""
        while True:
            await self.refresh_once()
            await asyncio.sleep(self.interval_s)
