from pathlib import Path
from frontend_src import app_js


ROOT = Path(__file__).resolve().parents[1]


def test_position_row_always_shows_bid_based_market_price():
    js = app_js()
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")

    assert "const quoteBid = p.quote ? Number(p.quote.bid) : NaN" in js
    assert "marketPrice: hasMarketPrice ? quoteBid : null" in js
    assert "marketPriceText: p.marketPrice == null ? '—'" in js
    assert "当前买价</span><strong" in html
    assert "{{ p.marketPriceText }}" in html
    assert "按当前买一价（实际可卖出价）" in html
