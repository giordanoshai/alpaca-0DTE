"""生产行情快照装配点。"""

from app.market_feed import MarketFeedHub
from app.services.data_source_registry import current_source
from app.services.feed_snapshot import build_feed_snapshot

MARKET_FEED = MarketFeedHub(
    source_provider=current_source,
    snapshot_builder=build_feed_snapshot,
)
