"""持仓行自定义数量卖出与快捷减仓必须保持两套独立语义。"""
from pathlib import Path
from frontend_src import app_js


ROOT = Path(__file__).resolve().parents[1]


def test_position_pnl_appends_realized_value_without_changing_title():
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    js = app_js()
    css = (ROOT / "static" / "app.css").read_text(encoding="utf-8")

    assert '<span class="position-label">PNL</span>' in html
    binding = js[js.index("pnlMainText:"):js.index("pnlSubText:")]
    assert "Math.abs(p.unreal)" in binding
    assert "+ '/' +" in binding
    assert "Math.abs(p.realized).toFixed(2)" in binding
    assert "position-cost-total" not in html + css
    assert "position-pnl-percent" not in html + css
    assert ".position-sub{color:#8796ad;font:600 9.5px var(--font-mono);white-space:nowrap}" in css
    assert "grid-template-columns:190px 62px 90px 86px 96px 96px 136px 190px" in css


def test_sell_quantity_uses_one_bounded_wheel_input_without_plus_minus():
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    js = app_js()

    assert 'max="{{ p.qty }}"' in html
    assert 'onWheel="{{ p.onSellQtyWheel }}"' in html
    assert "onSellMinus" not in html
    assert "onSellPlus" not in html
    assert "onSellMinus" not in js
    assert "onSellPlus" not in js
    assert "e.currentTarget.value = String(next)" in js


def test_custom_sell_has_full_price_menu_and_cost_limit_mapping():
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    js = app_js()
    source = html + js

    for option in ("限价 / 目标", "快速 / 买一", "成本价", "中间价", "卖一", "市价"):
        assert option in source
    assert "position-price-trigger" in html
    assert "position-price-menu" in html
    assert "if (priceType === 'cost')" in js
    assert "body.price_type = 'limit'" in js
    assert "body.limit_price" in js


def test_target_limit_is_default_and_accepts_wheel_or_manual_expression():
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    js = app_js()

    assert "exitPriceType: existing ? (existing.exitPriceType || 'target') : 'target'" in js
    assert "? Math.max(0, Number(existing.targetPct)) : 20" in js.replace("\n", " ")
    assert 'onWheel="{{ p.onTargetWheel }}"' in html
    assert 'onInput="{{ p.onTargetInput }}"' in html
    assert 'onChange="{{ p.onTargetChange }}"' in html
    assert "const multiplier = text.startsWith('*') || text.startsWith('×')" in js
    assert "p.entryMid * (1 + targetPct / 100)" in js
    assert "(e.deltaY > 0 ? -5 : 5)" in js
    assert 'disabled="{{ p.targetDisabled }}"' in html


def test_sell_quantity_is_immediately_left_of_sell_button():
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")

    qty = html.index('aria-label="卖出张数')
    sell = html.index('class="position-sell"')
    quick = html.index('class="position-quick"')
    assert qty < sell < quick
    assert sell - qty < 700


def test_quick_fraction_buttons_always_use_bid_and_ignore_custom_target():
    js = app_js()

    quick_start = js.index("_quickCloseBody(p, qty, reason)")
    quick_end = js.index("_selectedCloseBody(p, qty, reason)")
    quick_body = js[quick_start:quick_end]
    assert "price_type: 'bid'" in quick_body
    assert "exitPriceType" not in quick_body
    assert "targetPct" not in quick_body
    assert "this._quickCloseBody(p, n, label + ' 减仓')" in js


def test_price_selection_remains_available_while_close_order_is_pending():
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")

    assert '<div class="position-actions">' in html
    assert 'class="position-sell"' in html
    assert 'style="opacity:{{ p.tradeActionOpacity }};pointer-events:{{ p.tradeActionPointer }}"' in html


def test_custom_price_menu_keeps_selection_in_state_across_market_refresh():
    js = app_js()
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")

    assert 'value="{{ p.exitPriceType }}"' not in html
    assert "priceMenuOpen: existing ? !!existing.priceMenuOpen : false" in js
    assert "priceMenuOpen: !!p.priceMenuOpen" in js
    assert "onPriceMenuToggle" in js


def test_sound_only_plays_on_real_broker_fill_not_submit():
    js = app_js()

    assert "fill: 'submit.mp3'" in js
    assert "uiScroll: 'ui_scoll.mp3'" in js
    assert "playSfx('buy')" not in js
    assert "playSfx('sell')" not in js
    assert "newFills.some((e) => e.action !== 'auto_close')" in js
    assert "e.reason !== 'external_closed'" in js
    assert "!orderId.endsWith(':ext')" in js


def test_numeric_wheels_play_scroll_sound_only_after_value_changes():
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    js = app_js()

    assert 'onWheel="{{ onQtyWheel }}"' in html
    assert "onQtyWheel(e)" in js
    assert js.count("playUiScroll();") == 4
    assert "if (next === current) return;" in js
    assert "now - lastUiScrollSfxAt < 40" in js


def test_sell_actions_share_neutral_red_outline_and_divider_precedes_add():
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    css = (ROOT / "static" / "app.css").read_text(encoding="utf-8")

    sell = html.index('class="position-sell"')
    quick = html.index('class="position-quick"')
    divider = html.index('class="position-actions-divider"')
    add = html.index('class="position-add"')
    assert sell < quick < divider < add
    assert ".position-sell,\n.position-quick button{" in css
    assert "border:1px solid #703e3d" in css
    assert "background:#141b28" in css
    assert 'class="position-action-buttons"' in html
    assert ".position-action-buttons{" in css
    assert "height:auto" in css
    assert ".position-quick{display:flex;align-self:stretch;height:auto;gap:6px}" in css

    target_style = css[css.index(".position-percent{"):css.index(".position-price-picker")]
    assert "#f0c674" not in target_style
    assert "#4d4320" not in target_style
    assert ".position-add button" in css
    assert "border:1px solid #355f4d" in css


def test_signal_permission_uses_neutral_tone_without_red_green_glow():
    js = app_js()
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")

    assert "const lc = (st) => st === 'block' ? '#8d99aa' : '#9bc7e8'" in js
    assert "glow: 'none'" in js
    assert "bg: '#182130', border: '#46556b', fg: '#c3cee0'" in js
    signal_reason = html[html.index('<sc-if value="{{ t.reason }}"'):html.index("</sc-if>", html.index('<sc-if value="{{ t.reason }}"'))]
    assert "#d08a82" not in signal_reason
    assert "rgba(193,104,95" not in signal_reason
