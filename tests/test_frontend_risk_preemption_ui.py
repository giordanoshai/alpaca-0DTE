"""07/29 前端：精确 ESC、撤单状态/音效、模板锁与分区刷新。"""
import re
from pathlib import Path
from frontend_src import app_js


ROOT = Path(__file__).resolve().parents[1]


def _sources():
    return (
        app_js(),
        (ROOT / "static" / "index.html").read_text(encoding="utf-8"),
        (ROOT / "static" / "app.css").read_text(encoding="utf-8"),
    )


def test_escape_uses_current_selected_position_when_target_order_is_stale():
    js, html, _ = _sources()
    start = js.index("cancelPending()")
    end = js.index("// ⑦ 订单流面板", start)
    body = js[start:end]

    assert "cancelTarget: null" in js
    assert "target.kind === 'order'" in body
    assert "target.kind === 'position'" in body
    assert "o.oid === target.orderId" in body
    assert "p.optionCode === target.optionCode && !!p.closeOrderId" in body
    assert "const focused = this.state.positions[focusedIndex];" in body
    assert "if (focused && focused.closeOrderId)" in body
    assert "this.cancelClose(focusedIndex);" in body
    assert ".find((o) => o.status === 'pending')" not in body
    assert "未选择可撤挂单" in body
    assert 'onClick="{{ o.onSelect }}"' in html


def test_position_selection_color_does_not_depend_on_open_order():
    js, _, _ = _sources()

    assert "const positionSelected = i === s.posFocus;" in js
    assert "borderColor: positionSelected ? '#6366f1'" in js
    assert "rowBg: positionSelected ? 'rgba(99,102,241,.08)'" in js
    assert "const cancelSelected =" not in js


def test_selected_position_auto_binds_close_order_created_after_selection():
    js, _, _ = _sources()

    assert "const recoveredCloseOrders =" in js
    assert "recoveredCloseOrders.length === 1" in js
    assert "String(o.clientOrderId || '').startsWith('ttt-')" in js
    assert "o.parentOrderId === p.order_id" in js
    assert "const closeOrderId = p.close_order_id" in js
    assert "orderId: p.closeOrderId || null" in js
    assert "const selected = positions.find((p) => p.optionCode === cancelTarget.optionCode);" in js
    assert "orderId: selected.closeOrderId || null" in js
    assert "&& !!p.closeOrderId" in js
    assert "posFocus: i" in js


def test_position_cancel_uses_exact_order_id_after_recovery():
    js, _, _ = _sources()
    start = js.index("async cancelClose(i)")
    end = js.index("closeAll()", start)
    body = js[start:end]

    assert "const closeOrderId = p.closeOrderId;" in body
    assert "'/api/orders/' + encodeURIComponent(closeOrderId) + '/cancel'" in body
    assert "'/api/close/cancel'" not in body


def test_sell_button_keeps_optimistic_closing_state_until_backend_catches_up():
    js, _, _ = _sources()

    assert "this._optimisticCloseContracts.add(p.optionCode);" in js
    assert "closing: backendClosing || optimisticClosing" in js
    assert "(backendClosing || p.close_error)" in js
    assert "this._optimisticCloseContracts.delete(p.optionCode);" in js


def test_position_alerts_build_baseline_without_replaying_after_refresh():
    js, _, _ = _sources()
    start = js.index("applyPositions(g, revision)")
    end = js.index("// 真·对账", start)
    body = js[start:end]

    assert "const positionBaselineDone = this._positionsSeenInit === true;" in body
    assert body.count("if (positionBaselineDone)") >= 2
    assert "this._closeErrShown.add(p.option_code);" in body
    assert "this._syncNoteShown.add(p.option_code);" in body


def test_cancel_busy_state_and_broker_confirmed_audio_mapping():
    js, html, _ = _sources()

    assert "撤单中…" in js
    assert "p.cancelRequested" in js
    assert "o.cancelOrigin" in js
    assert 'disabled="{{ o.cancelBusy }}"' in html
    assert 'disabled="{{ p.cancelBusy }}"' in html
    assert "cancelCause: e.cancel_cause || null" in js
    assert "e.cancel_cause === 'manual'" in js
    assert "e.cancel_cause === 'timeout'" in js
    assert "fallbackRiskFills" in js
    assert "_riskSoundContracts" in js
    assert "risk: 'stop_lose.mp3'" in js
    assert "cancel: 'cancel.mp3'" in js
    assert "e.action !== 'auto_close'" in js


def test_stop_type_badge_and_cancel_button_size():
    js, html, css = _sources()

    assert "stopModeBadge: p.stopMode === 'amount' ? 'A' : 'P'" in js
    assert 'title="{{ p.stopModeTitle }}"' in html
    assert ".position-stop-mode{" in css
    cancel_css = css[css.index(".position-cancel{"):css.index(".position-actions{")]
    assert "height:20px" in cancel_css
    assert "min-width:48px" in cancel_css


def test_market_lock_keeps_load_button_visible_but_disabled():
    js, html, _ = _sources()

    assert "marketLocked: t.market_locked === true" in js
    assert "美股盘中不可加载配置模板" in js
    assert "loadDisabled: s.marketLocked" in js
    assert 'disabled="{{ v.loadDisabled }}"' in html
    assert "{{ marketLockNotice }}" in html


def test_ws_updates_use_dirty_regions_and_approx_delta_is_non_blocking():
    js, html, _ = _sources()

    for region in ("tickers", "order-entry", "positions", "orders", "risk", "settings"):
        assert f'data-render-region="{region}"' in html
    assert "function renderApp(regions)" in js
    assert "current.replaceWith(next)" in js
    assert "regions: ['orders', 'positions']" in js
    assert "regions: ['positions']" in js
    assert "regions: ['positions', 'risk']" in js
    assert "regions: ['risk', 'tickers', 'order-entry']" in js
    assert "regions: ['tickers', 'order-entry']" in js
    # 2026-08-27：后端不再兜底 0.50 跑 delta 闸，提示语随之改成「未生效」。
    # 旧文案「delta 使用≈0.50」会让人以为闸判过了，是这次要消掉的那个假象本身。
    assert "Greeks 暂缺 · delta 闸未生效（选的是最贴平值合约）', 'warn'" in js
    assert "delta 使用≈0.50" not in js
    assert "UW 离线 · delta 用近似值" not in js
    # 2026-08-15 新增距墙展示（纯展示，不进闸门）
    assert "wallCallPx" in js


def test_距墙CALL和PUT分两行且价格与百分比分列():
    """并排一行时 `↑ 185.0 +2.35% · ↓ 178.0 -1.52%` 放不下 236px 卡片，会横向溢出。

    价格与百分比必须是两个独立绑定：合成一个字符串的话，价格位数不同
    （600.0 / 78.5）百分比就参差不齐，两行对不上。
    """
    js, html, _ = _sources()

    for key in ("wallCallPx", "wallCallPct", "wallPutPx", "wallPutPct"):
        assert key in js, key
        assert "{{ t.%s }}" % key in html, key
    # 旧的合成字符串绑定不得残留
    assert "t.wallCall }}" not in html and "t.wallPut }}" not in html
    # 两行靠网格对列
    assert "grid-template-columns:auto auto 1fr" in html


def test_卡片开overflow_hidden兜住罕见的三行reason():
    """卡片 height:172px 是 border-box（app.css 里 *{box-sizing:border-box}），
    内容区只有 146px（2026-08-17 中途试过拉高到 198px，复核后确认原高度够用、改回 172px）。
    加了两行墙之后余量变薄，溢出时宁可截掉也不能画到隔壁卡片上。"""
    _, html, _ = _sources()
    card = html[html.index('data-tsym="{{ t.sym }}"'):]
    card = card[:card.index(">")]
    assert "height:172px" in card and "overflow:hidden" in card


def test_只带walls的帧也要重绘卡片():
    """walls 与 feed 是 trading_ws 里两条独立的 section。

    onmessage 里唯一排渲染的 setState 在 feed 分支后面，`!hasFeed` 那条 return
    上面只有 refreshHeartbeat()（它只改心跳那几个文本节点、不排渲染）。所以只带
    walls 的 patch 必须自己补一次 tickers 渲染，否则盘中靠 feed 帧密集蹭上去、
    盘前与收盘后 feed 一静默，墙位就冻在最后一次 feed 帧上。
    """
    js, _, _ = _sources()

    # 按结构断言而不是钉死那行字面写法：2026-08-21 加 GEX 破墙提醒时，这里从
    # 一行变成了代码块（同一个 !hasFeed 分支里多跑一次 checkGexBreach），
    # 不变量没变 —— 只带 walls 的帧仍然会补一次 tickers 渲染。
    branch = re.search(r"if \(hasOwn\(msg, 'walls'\)\) \{[\s\S]*?\n    \}", js)
    assert branch, "找不到 walls 分支"
    assert "if (!hasFeed)" in branch.group(0), "只带 walls 的帧没有补渲染"
    assert "scheduleRender({ soft: true, regions: ['tickers'] })" in branch.group(0), \
        "只带 walls 的帧没有补渲染"
    # 赋值必须排在 hasFeed 算出来之后，否则拿不到它做判断
    assert js.index("const hasFeed = hasOwn(msg, 'ticks')") < js.index("app.state.walls = msg.walls")

