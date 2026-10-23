"""下单面板的止损模式只能二选一，默认百分比，不提供阈值输入。"""
from pathlib import Path
from frontend_src import app_js


ROOT = Path(__file__).resolve().parents[1]


def test_stop_mode_selector_follows_qty_input_and_defaults_percent():
    js = app_js()
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")

    assert "stopMode: 'percent'" in js
    assert "stop_mode: this.state.stopMode" in js
    assert "['percent', `比例 ${Number(s.svalues.stopPct).toFixed(0)}%`]" in js
    assert "['amount', `金额 $${Number(s.svalues.dollarLine).toFixed(0)}`]" in js
    assert html.index('data-fkey="qty"') < html.index('list="{{ stopModes }}"')


def test_stop_mode_selector_has_no_threshold_input():
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")

    selector = html[
        html.index('list="{{ stopModes }}"'):
        html.index("<!-- ⑩ 价格类型")
    ]
    assert "<button" in selector
    assert "<input" not in selector
