from pathlib import Path
from frontend_src import app_js


ROOT = Path(__file__).resolve().parents[1]


def test_capped_vwap_distance_still_drives_threshold_color_and_has_hint():
    js = app_js()
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")

    assert "function formatVwapDistanceForUi(value)" in js
    assert "t.vwap = formatVwapDistanceForUi(tk.vwap)" in js
    assert "const vwapNum = parseVwapDistance(t.vwap)" in js
    assert "开盘初期极端值封顶显示" in html
