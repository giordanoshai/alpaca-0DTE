"""持仓卡按秒更新已持仓时长，并稳定显示最大允许时长。"""
from pathlib import Path
from frontend_src import app_js


ROOT = Path(__file__).resolve().parents[1]


def test_position_card_shows_elapsed_and_limit_as_mm_ss():
    js = app_js()
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")

    assert "label: '持仓时间'" in js
    assert "fmtHold(elapsedSec) + '/' + fmtHold(maxHoldSec)" in js
    assert "Math.round(p.maxSec / 60) + ' 分钟'" not in js
    assert 'data-hold-start="{{ p.holdStart }}"' in html
    assert 'data-hold-max="{{ p.holdMax }}"' in html


def test_hold_timer_refreshes_each_second_without_full_render():
    js = app_js()

    assert "this.refreshHoldTimers();" in js
    assert "this.state.now = Date.now();" in js
    assert "el.textContent = fmt(elapsedSec);" in js
    assert "el.textContent = fmt(elapsedSec) + '/' + fmt(maxSec);" not in js
    assert "holdSubText: fmtHold(maxHoldSec) + ' 上限'" in js
    assert "elapsedSec >= maxSec * 0.8" in js
