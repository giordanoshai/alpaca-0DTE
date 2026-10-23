/* ----------------------------------------------------------------------------
 * 渲染绑定：把 state 算成模板要的那一份对象
 *
 * renderVals() 一次算出全页绑定，renderXBindings() 负责在分区软刷新时只挑该区要的键。
 * 这里只读 state、不改 state。
 * --------------------------------------------------------------------------*/

import { parseOptionCode, parseVwapDistance } from './format.js';
import { BUY_SIDES, SELF_CHECK_ITEMS, selfCheckPassed } from './self-check.js';
import { HOTKEY_DEFAULTS, HOTKEY_LABELS } from './hotkeys.js';
import { playUiScroll } from './sfx.js';

export const withRender = (Base) => class extends Base {

  _pickRenderBindings(bindings, keys) {
    const selected = {};
    keys.forEach((key) => { selected[key] = bindings[key]; });
    return selected;
  }

  renderCommonBindings(bindings) {
    return this._pickRenderBindings(bindings, [
      'isTrade', 'isSettings', 'heartbeatLost', 'clock', 'mode', 'hb', 'alpacaPong',
      'gotoSettings', 'gotoTrade', 'footerKeys',
    ]);
  }

  renderTickersBindings(bindings) {
    return this._pickRenderBindings(bindings, [
      'tickerCount', 'tickers', 'onSignalWheel',
    ]);
  }

  renderOrderEntryBindings(bindings) {
    return this._pickRenderBindings(bindings, [
      'focusT', 'order', 'qtyPresets', 'qtyValue', 'onQtyInput', 'onQtyWheel',
      'stopModes', 'priceTypes', 'showLimitInput', 'limitValue', 'onLimitInput',
    ]);
  }

  renderPositionsBindings(bindings) {
    return this._pickRenderBindings(bindings, [
      'posCount', 'posEmpty', 'positions', 'revealed', 'hidden', 'revealBtnLabel',
      'reconcile', 'hasUnmanaged', 'unmanagedCount', 'unmanagedSummary', 'reveal',
      'closeAll',
    ]);
  }

  renderOrdersBindings(bindings) {
    return this._pickRenderBindings(bindings, ['orderFlow']);
  }

  renderRiskBindings(bindings) {
    return this._pickRenderBindings(bindings, ['risk']);
  }

  renderSettingsBindings(bindings) {
    return this._pickRenderBindings(bindings, [
      'readonly', 'groups', 'hotkeys', 'fixedHotkeys', 'hotkeySave', 'hotkeyReset',
      'hotkeyHint', 'hotkeyHintColor', 'hotkeySaveDisabled', 'hotkeySaveOpacity',
      'activeTemplate', 'pendingTemplate', 'hasPendingTemplate', 'loadedTemplate',
      'loadedPending', 'loadedActive', 'configs', 'marketLockNotice', 'marketLocked',
      'configName', 'reason', 'save', 'toggleSound', 'soundBg', 'soundBorder',
      'soundKnob', 'toggleGexAlert', 'gexAlertBg', 'gexAlertBorder', 'gexAlertKnob',
    ]);
  }

  renderVals(regions) {
    const s = this.state;
    const wants = (name) => !regions || regions.has(name);
    const needTickers = wants('tickers');
    const needOrderEntry = wants('order-entry');
    const needPositions = wants('positions');
    const needOrders = wants('orders');
    const needRisk = wants('risk');
    const needSettings = wants('settings');
    const g = needOrderEntry ? this.gate() : {};
    const now = s.now, cb = this.clamp.bind(this);
    // clock (Beijing)
    const dd = new Date(now);
    const bh = (dd.getUTCHours() + 8) % 24, bm = dd.getUTCMinutes(), bs = dd.getUTCSeconds();
    const clock = [bh, bm, bs].map((x) => String(x).padStart(2, '0')).join(':');
    // mode
    const mode = { label: 'PAPER', bg: 'rgba(201,169,62,.14)', fg: '#d8bd6f', border: '#4d4320' };
    // 两段链路健康：PING=浏览器↔后端；PONG=后端↔Alpaca TradingStream。
    const hbLost = s.wsDown === true;
    const hb = hbLost
      ? { color: '#e0776c', label: 'PING 断线', anim: 'hbpulse 1s ease-in-out infinite' }
      : {
          color: s.hbMs == null ? '#d8bd6f' : '#5f9e83',
          label: s.hbMs == null ? 'PING 等待中' : ('PING ' + s.hbMs + 'ms'),
          anim: 'none',
        };
    const alpacaPong = !s.alpacaConnected
      ? { color: '#e0776c', label: 'PONG 断线', anim: 'hbpulse 1s ease-in-out infinite' }
      : {
          color: s.alpacaPongMs == null ? '#d8bd6f' : '#5f9e83',
          label: s.alpacaPongMs == null ? 'PONG 等待中' : ('PONG ' + s.alpacaPongMs + 'ms'),
          anim: 'none',
        };
    // tickers
    // 禁止保持中性灰；可买入用淡蓝，不恢复红绿交通灯刺激。
    const lc = (st) => st === 'block' ? '#8d99aa' : '#9bc7e8';
    // 2026-08-14：取消「可做多 / 可做空」，只剩「可买入 / 禁止」两态。
    // 方向不由系统判定 —— 2026-09-01 起震荡、追高、方向闸、动能闸四条全部删除，
    // 剩下的规则一条都不看方向，两侧结论恒等。两态标签是后端行为的如实反映。
    // st 四态保留：排序（st !== 'block'）和按钮放行（走 gate()，直接读
    // gates[方向].blocked）仍然需要它。
    const ll = (st) => st === 'block' ? '禁止' : '可买入';
    // 距 VWAP 标红的阈值。2026-09-01 追高闸删除后模板里已没有 σ 倍数这个参数，
    // 改成写死 2.0：它现在**只是个视觉标记**（「离 VWAP 挺远了」），不对应任何闸门，
    // 也不代表系统认为不该买。数值本身照常显示，标不标红都不影响能否下单。
    const VWAP_HIGHLIGHT_SIGMA = 2.0;
    const tickers = needTickers ? this.TICKERS.map((t, i) => {
      // 开盘初期 σ 可能极小，后端会把纯展示值封顶成 ≥99.9 / ≤−99.9。
      // 去掉封顶符号后再判色，确保极端值仍标红。
      const vwapNum = parseVwapDistance(t.vwap);
      const vwapHigh = isFinite(vwapNum) && Math.abs(vwapNum) >= VWAP_HIGHLIGHT_SIGMA;

      // 距墙：后端只推墙位绝对价（10s 一轮），百分比用**实时价**在这里算，
      // 这样墙位慢更新、百分比每 tick 都是活的。
      const w = (s.walls || {})[t.sym];
      const px = parseFloat(t.price);
      const wallPct = (raw) => {
        const v = parseFloat(raw);
        if (!isFinite(v) || !isFinite(px) || px <= 0) return null;
        return ((v - px) / px) * 100;
      };
      // 墙位价与百分比**分开出**，模板里各占一个网格列。合成一个字符串的话，
      // 价格位数不同（600.0 / 78.5）百分比就会参差不齐，两行对不上。
      const wallPx = (raw) => parseFloat(raw).toFixed(1);
      const wallPctText = (raw) => {
        const pct = wallPct(raw);            // toFixed 自带负号，只补正号
        return (pct >= 0 ? '+' : '') + pct.toFixed(2) + '%';
      };
      const hasCall = !!w && wallPct(w.call_wall) !== null;
      const hasPut = !!w && wallPct(w.put_wall) !== null;

      // Gamma Flip（UW gex-levels 本来就带的字段，不新增请求）。**只当普通参考价位显示**：
      // 曾经拿它跟现价比大小推 ±GEX，2026-08-21 实测证伪后删除（见文件上方
      // 【别再把 ±GEX 加回来】）。价位本身是真实数据，继续显示。
      const flipNum = w && w.gamma_flip != null ? parseFloat(w.gamma_flip) : NaN;
      const hasGammaFlip = isFinite(flipNum);

      // ±GEX：**直读后端下发的 net_gex**（后端取 spot-exposures 的
      // gamma_per_one_percent_move_dir 真值，见 UwClient.spot_gamma_dir）。
      // 禁止再拿现价跟 flip 比大小推 —— 那套在 −GEX 一侧只有 53.5% 准（≈抛硬币），
      // 偏偏那才是关心的状态。含义是「做市商放大波动（实测 1.46 倍）」，
      // **不指方向**，不等于「可以追」。
      const netGex = w ? w.net_gex : null;
      const hasNetGex = netGex === 'pos' || netGex === 'neg';

      return {
        sym: t.sym, hot: i < 9 ? String(i + 1) : (i === 9 ? '0' : '·'), vwap: t.vwap, reason: t.reason,
        // 现价：t.price 本来就随 WS 每 tick 下发（供距墙百分比/VWAP 距离用），
        // 卡片头部只是第一次真正显示它，没有新增任何数据源。
        priceText: isFinite(px) && px > 0 ? px.toFixed(2) : '—',
        // 卡片上「效率比」那一格与右上角的方向标签于 2026-09-01 随震荡闸、
        // 方向闸一并撤掉，只剩距 VWAP σ（纯参考，不拦单）。
        vwapColor: vwapHigh ? '#d08a82' : '#c3cee0',
        light: t.candidateHeld ? '#8695ab' : lc(t.st),
        lightLabel: t.candidateHeld ? '已持仓' : ll(t.st),
        glow: 'none',
        hasWall: hasCall || hasPut,
        wallCallPx: hasCall ? wallPx(w.call_wall) : '—',
        wallCallPct: hasCall ? wallPctText(w.call_wall) : '—',
        wallPutPx: hasPut ? wallPx(w.put_wall) : '—',
        wallPutPct: hasPut ? wallPctText(w.put_wall) : '—',
        // stale（本轮没拉到，显示的是上一轮的值）整块灰显
        wallColor: (w && w.stale) ? '#5a6a82' : '#8695ab',
        // 只有 flip 或只有 ±GEX 都要显示这一行 —— 两者来自不同接口，可能一个有一个没有
        hasGex: hasGammaFlip || hasNetGex,
        gammaFlipPx: hasGammaFlip ? flipNum.toFixed(1) : '—',
        netGexLabel: hasNetGex ? (netGex === 'pos' ? '+GEX' : '−GEX') : '—',
        // −GEX 暖色（波动会放大，值得多看一眼）、+GEX 冷色；stale 时与墙位一起灰显
        netGexColor: (w && w.stale) ? '#5a6a82' : (netGex === 'neg' ? '#d08a82' : '#7f9cc0'),
        gexColor: (w && w.stale) ? '#5a6a82' : '#8695ab',
        border: i === s.focus ? '#6366f1' : '#22314a', onSelect: () => this.setFocus(i),
      };
    }) : [];
    const f = this.TICKERS[s.focus];
    const focusT = needOrderEntry ? {
      sym: f.sym,
      light: f.candidateHeld ? '#8695ab' : lc(f.st),
      lightLabel: f.candidateHeld ? '已持仓' : ll(f.st),
    } : {};
    // order — 顶部模块只负责买入；所有卖出操作集中到持仓行。
    // CALL / PUT 并列各一个按钮：2026-08-08 起是这样，2026-08-21 到 2026-09-01
    // 之间因为规则 6 方向闸「两侧最多一边能过」合并成过一个，方向闸删除后恢复。
    const busy = s.orderBusy;   // 下单/平仓/加仓请求进行中：按钮禁用 + 转圈，给出「有过程」的反馈
    const spinStyle = 'animation:spin 1s linear infinite;display:inline-block';
    const mkBuyBtn = (enabled, label, icon, dir, kbd) => {
      if (busy) return { bg: '#1c2740', border: '#2a3852', fg: '#8695ab', cursor: 'not-allowed', label: '处理中…', icon: 'bi-arrow-repeat', action: null, kbd: '' };
      return enabled
        ? {
            bg: '#182130', border: '#46556b', fg: '#c3cee0', cursor: 'pointer', label, icon,
            action: () => {
              this.flashHotkey(label, kbd);
              this.doOrder(dir);
            },
            kbd
          }
        : {
            bg: '#141f31', border: '#22314a', fg: '#4d5c73', cursor: 'pointer', label, icon: 'bi-slash-circle',
            action: () => {
              this.flashHotkey(label, kbd);
              this.toast(`${label} · ${g.reason || '闸门未通过 / 暂不可入场'}`, 'block');
            },
            kbd
          };
    };
    const [callSide, putSide] = BUY_SIDES;
    const callBtn = needOrderEntry
      ? mkBuyBtn(g.call, callSide.label, callSide.icon, callSide.dir, s.hotkeys.buy) : {};
    const putBtn = needOrderEntry
      ? mkBuyBtn(g.put, putSide.label, putSide.icon, putSide.dir, s.hotkeys.buyPut) : {};
    const uq = Math.max(1, Math.floor(Number(s.qty) || 1));   // ⑥ 有效下单张数
    const cL = (needOrderEntry && f.cLong) || {}, cS = (needOrderEntry && f.cShort) || {};
    // 没有合约时 mid 是 '—'，`Number('—')||0` 会把金额渲染成 **$0** —— 一个看起来
    // 很确定的假数字（08-14 ASTS PUT）。拿不到就如实显示横杠。
    const amount = (c) => (c && Number(c.mid) > 0
      ? '$' + Math.round(Number(c.mid) * 100 * uq)
      : '—');
    const order = needOrderEntry ? {
      opacity: g.dormant ? '0.62' : '1',
      // CALL 侧
      callContract: cL.name, callDelta: cL.delta, callSpread: cL.spread,
      callBid: cL.bid, callMid: cL.mid, callAsk: cL.ask,
      // PUT 侧（合约明细保留两行：那是**信息**不是**动作**，另一边什么价位仍要看得见）
      putContract: cS.name, putDelta: cS.delta, putSpread: cS.spread,
      putBid: cS.bid, putMid: cS.mid, putAsk: cS.ask,
      // 两个买入按钮：左 CALL 右 PUT，各自的亮/灰只认后端对该方向干跑的结果。
      callBtnBg: callBtn.bg, callBtnBorder: callBtn.border, callBtnFg: callBtn.fg,
      callBtnCursor: callBtn.cursor, callBtnLabel: callBtn.label, callBtnIcon: callBtn.icon,
      onBuyCall: callBtn.action, callBtnKbd: callBtn.kbd, callAmount: amount(cL),
      putBtnBg: putBtn.bg, putBtnBorder: putBtn.border, putBtnFg: putBtn.fg,
      putBtnCursor: putBtn.cursor, putBtnLabel: putBtn.label, putBtnIcon: putBtn.icon,
      onBuyPut: putBtn.action, putBtnKbd: putBtn.kbd, putAmount: amount(cS),
      buyBtnIconStyle: busy ? spinStyle : '',
      qty: uq,
      // 被拦时把原因说出来。
      gateNote: g.reason || '',
      showBreaker: g.mask === 'breaker', showReconcile: g.mask === 'reconcile',
      pending: !!s.pending, pendingText: s.pending ? s.pending.text : '',
    } : {};
    // 下单自查弹窗。**不受 needOrderEntry 约束** —— 它挂在交易页根节点下、不在任何
    // data-render-region 里，只有整树硬渲染够得着；而 selfCheck 的每次变化都是硬渲染。
    const sc = s.selfCheck;
    const scPass = selfCheckPassed(sc && sc.answers);
    const scSide = sc ? BUY_SIDES.find((x) => x.dir === sc.dir) : null;
    const selfCheck = sc ? {
      open: true,
      title: (scSide ? scSide.label : '买入') + ' · ' + ((this.TICKERS[s.focus] || {}).sym || ''),
      items: SELF_CHECK_ITEMS.map((item, i) => {
        const yes = sc.answers[item.key] !== false;
        // 三条判据文字统一红色（用户 2026-09-02 要求「显眼红色」），但硬拦那条
        // 得再抢眼一档 —— 全一样红等于没有重点，而这三条里只有它能拦住人。
        // 红竖条 + 「必须答否」标签都由 blocking 决定，不硬编码 key。
        const blocking = !!item.blocking;
        return {
          n: String(i + 1), q: item.q, hint: item.hint,
          barColor: blocking ? '#ef4444' : 'transparent',
          mustText: blocking ? '必须答否' : '',
          mustDisplay: blocking ? 'inline-flex' : 'none',
          state: yes ? '是' : '否',
          bg: yes ? 'rgba(193,104,95,.14)' : 'rgba(74,222,128,.1)',
          border: yes ? '#663d43' : 'rgba(74,222,128,.28)',
          fg: yes ? '#d08a82' : '#4ade80',
          knobBg: yes ? '#c1685f' : '#4ade80',
          knobX: yes ? '2px' : '23px',
          onToggle: () => this.toggleSelfCheck(item.key),
        };
      }),
      okBg: scPass ? '#6366f1' : '#1c2740',
      okBorder: scPass ? '#6366f1' : '#2a3852',
      okFg: scPass ? '#fff' : '#4d5c73',
      okCursor: scPass ? 'pointer' : 'not-allowed',
      // 提示语只说硬拦那一条 —— 另外两条拨在哪边都能提交，写成「三个都拨到否」
      // 会让人以为它们也拦人，那正是这次要去掉的误会。
      okNote: scPass ? '「打架」已答否，可以提交' : '第 2 条「打架」必须答否才能提交',
      okNoteColor: scPass ? '#4ade80' : '#f87171',
      onOk: () => this.confirmSelfCheck(),
      onCancel: () => this.closeSelfCheck(),
    } : { open: false, items: [] };
    // ⑥ 数量控件 + ⑩ 价格类型选择器
    const selBg = 'rgba(99,102,241,.16)', selBorder = '#4649c4', selFg = '#a5b0fb';
    const qtyPresets = needOrderEntry ? s.qtyPresets.map((n) => ({
      n, onPick: () => this.setQty(n),
      bg: Number(s.qty) === n ? selBg : 'transparent', border: Number(s.qty) === n ? selBorder : '#2a3852', fg: Number(s.qty) === n ? selFg : '#8695ab',
    })) : [];
    const stopBg = 'rgba(193,104,95,.32)', stopBorder = '#b95750';
    const stopModes = needOrderEntry ? [
      ['percent', `比例 ${Number(s.svalues.stopPct).toFixed(0)}%`],
      ['amount', `金额 $${Number(s.svalues.dollarLine).toFixed(0)}`],
    ].map(([k, label]) => ({
      label, onPick: () => this.setStopMode(k),
      bg: s.stopMode === k ? stopBg : 'transparent',
      border: s.stopMode === k ? stopBorder : '#7a3f3f',
      fg: s.stopMode === k ? '#ffe0dc' : '#d08a82',
    })) : [];
    const priceTypes = needOrderEntry ? [['auto', '自动'], ['limit', '限价'], ['bid', '买一'], ['ask', '卖一'], ['mid', '中间价'], ['market', '市价']].map(([k, label]) => ({
      label, onPick: () => this.setPriceType(k),
      bg: s.priceType === k ? selBg : 'transparent', border: s.priceType === k ? selBorder : '#2a3852', fg: s.priceType === k ? selFg : '#8695ab',
    })) : [];
    // positions
    const revealed = s.pnlShown;   // Alt+P 切换
    const bar = (p) => p < 0.5 ? '#5f9e83' : p < 0.8 ? '#c2a15a' : '#c1685f';
    const sp = s.svalues.stopPct;
    const positions = needPositions ? s.positions.map((p, i) => {
      // 真实持仓（有 optionCode）：合约名/行权价来自 OCC 代码真实解析；练手仓沿用固定假后缀展示
      const real = parseOptionCode(p.optionCode);
      const suf = p.sym === 'SPY' ? '583' : p.sym === 'IWM' ? '229' : '612';
      let contract = real.typeText ? (p.sym + ' ' + real.typeText) : (p.sym + ' ' + (p.type === 'CALL' ? 'C' : 'P') + suf);
      if (p.syncNote) contract += ' ⚠';   // 张数曾与券商不一致（详情见 toast）
      const closing = !!p.closing;   // 平仓中：整行灰化 + 状态位显示「平仓中…」，按钮点击在各方法入口已吞
      const resting = closing && !!p.closeOrderId;   // 平仓挂单在券商排队未成交 → 可撤
      // 行选中与挂单状态完全解耦：cancelTarget 只决定 ESC 能否撤单。
      const positionSelected = i === s.posFocus;
      const cancelBusy = !!(
        p.closeOrderId
        && (
          p.cancelRequested
          || (this._cancelBusy && this._cancelBusy.has(p.closeOrderId))
        )
      );
      const fmtHold = (seconds) => {
        const total = Math.max(0, Math.floor(Number(seconds) || 0));
        return String(Math.floor(total / 60)).padStart(2, '0')
          + ':' + String(total % 60).padStart(2, '0');
      };
      const elapsedSec = Math.max(0, Math.floor((now - p.startTs) / 1000));
      const maxHoldSec = Math.max(0, Math.floor(Number(p.maxSec) || 0));
      const stopPrice = p.stopMode === 'amount'
        ? Math.max(0, p.entryMid - Number(p.dollarLine || 0) / (100 * Math.max(1, p.qty)))
        : Math.max(0, p.entryMid * (1 - Number(p.stopPct || sp) / 100));
      const targetPct = Math.max(0, Number(p.targetPct) || 0);
      const targetMode = p.exitPriceType === 'target';
      const targetPrice = this._targetLimitPrice(p, targetPct);
      const quickPrice = p.exitPriceType === 'ask' ? p.quoteAsk
        : p.exitPriceType === 'mid' ? p.quoteMid
          : p.exitPriceType === 'cost' ? p.entryMid
            : p.exitPriceType === 'market' ? null : p.quoteBid;
      const pendingOrder = s.orders
        .filter((o) => o.status === 'pending' && o.optCode === p.optionCode
          && ['add', 'reduce', 'close', 'auto_close'].includes(o.action))
        .sort((a, b) => b.ts - a.ts)[0] || null;
      const actionNames = { add: 'ADD', reduce: 'REDUCE', close: 'CLOSE', auto_close: 'CLOSE' };
      const pendingAction = pendingOrder ? (actionNames[pendingOrder.action] || 'CLOSE') : null;
      const fallbackAction = closing ? ((p.sellQty || p.qty) < p.qty ? 'REDUCE' : 'CLOSE') : null;
      const workingAction = pendingAction || fallbackAction;
      const workingQty = pendingOrder ? pendingOrder.qty : (closing ? (p.sellQty || p.qty) : 0);
      const pendingPx = pendingOrder && Number(pendingOrder.limitPrice);
      const workingPrice = Number.isFinite(pendingPx) && pendingPx > 0
        ? pendingPx : (closing ? (targetMode ? targetPrice : quickPrice) : null);
      const workingText = workingAction
        ? (workingAction + (Number.isFinite(workingPrice) ? ` $${workingPrice.toFixed(2)}×${workingQty}` : ` ×${workingQty}`))
        : 'Null';
      return {
        contract, type: p.type, qty: p.qty,
        marketPriceText: p.marketPrice == null ? '—' : ('$' + p.marketPrice.toFixed(2)),
        stopPriceText: '$' + stopPrice.toFixed(2),
        stopModeBadge: p.stopMode === 'amount' ? 'A' : 'P',
        stopModeTitle: p.stopMode === 'amount' ? '金额止损' : '比例止损',
        holdText: fmtHold(elapsedSec),
        holdSubText: fmtHold(maxHoldSec) + ' 上限',
        holdStart: String(p.startTs),
        holdMax: String(maxHoldSec),
        costText: revealed ? ('$' + p.entryMid.toFixed(2)) : '••••',
        costSubText: revealed ? ('$' + p.entryVal.toFixed(0)) : '已隐藏',
        pnlMainText: revealed
          ? ((p.unreal >= 0 ? '+$' : '−$') + Math.abs(p.unreal)
            + '/' + (p.realized >= 0 ? '+$' : '−$') + Math.abs(p.realized).toFixed(2))
          : '••••',
        pnlSubText: revealed ? ((p.pnlPct >= 0 ? '+' : '−') + Math.abs(p.pnlPct).toFixed(1) + '%') : '已隐藏',
        workingText,
        workingColor: workingAction ? '#d8bd6f' : '#5a6a82',
        sellQty: p.sellQty || 1,
        targetInput: p.targetInput != null ? p.targetInput : this._formatTargetPct(targetPct),
        targetDisabled: !targetMode,
        targetOpacity: targetMode ? '1' : '.38',
        targetPointer: targetMode ? 'auto' : 'none',
        targetPriceText: targetMode
          ? ('$' + targetPrice.toFixed(2))
          : (p.exitPriceType === 'market' ? '市价'
            : (Number.isFinite(quickPrice) ? ('$' + quickPrice.toFixed(2)) : '—')),
        sellTitle: targetMode
          ? '目标限价持续挂单：不受短超时限制'
          : `${this._exitPriceLabel(p.exitPriceType)}卖出指定张数：未成交按当前挂单超时设置自动撤单`,
        tradeActionOpacity: closing ? '.42' : '1',
        tradeActionPointer: closing ? 'none' : 'auto',
        priceMenuOpen: !!p.priceMenuOpen,
        priceMenuLabel: this._exitPriceLabel(p.exitPriceType),
        pricePickerZ: p.priceMenuOpen ? '60' : '1',
        priceOptions: ['target', 'bid', 'cost', 'mid', 'ask', 'market'].map((priceType) => ({
          label: this._exitPriceLabel(priceType),
          bg: p.exitPriceType === priceType ? 'rgba(99,102,241,.18)' : '#111b2c',
          color: p.exitPriceType === priceType ? '#c7d2fe' : '#c3cee0',
          border: p.exitPriceType === priceType ? '#6366f1' : '#22314a',
          onPick: (e) => {
            e.stopPropagation();
            const pos = this.state.positions.map((item, idx) => ({
              ...item,
              priceMenuOpen: false,
              ...(idx === i ? { exitPriceType: priceType } : {}),
            }));
            this.setState({ positions: pos });
          },
        })),
        resting,
        cancelLabel: cancelBusy ? '撤单中…' : '撤单',
        cancelBusy,
        onCancelClose: (e) => {
          e.stopPropagation();
          this.selectPosition(i);
          this.cancelClose(i);
        },
        typeBg: closing ? 'rgba(134,149,171,.14)' : (p.type === 'CALL' ? 'rgba(95,158,131,.14)' : 'rgba(193,104,95,.14)'),
        typeFg: closing ? '#8695ab' : (p.type === 'CALL' ? '#7fb79f' : '#d08a82'),
        unrealText: (p.unreal >= 0 ? '+$' : '−$') + Math.abs(p.unreal), pnlPctText: (p.pnlPct >= 0 ? '+' : '−') + Math.abs(p.pnlPct).toFixed(1) + '%', realizedText: (p.realized >= 0 ? '+$' : '−$') + Math.abs(p.realized).toFixed(2),
        pnlColor: !revealed ? '#e6ecf3' : closing ? '#5a6a82' : (p.unreal >= 0 ? '#7fb79f' : '#d08a82'),
        stops: [
          {
            label: '风控止损',
            remain: p.stopMode === 'amount'
              ? ('$' + Number(p.dollarLine != null ? p.dollarLine : s.svalues.dollarLine).toFixed(0))
              : ((p.stopPct != null ? p.stopPct.toFixed(0) : sp) + '%'),
            color: '#8695ab', holdStart: '', holdMax: '',
          },
          {
            label: '持仓时间',
            remain: fmtHold(elapsedSec) + '/' + fmtHold(maxHoldSec),
            color: elapsedSec >= maxHoldSec ? '#d08a82' : (elapsedSec >= maxHoldSec * 0.8 ? '#d8bd6f' : '#c3cee0'),
            holdStart: String(p.startTs), holdMax: String(maxHoldSec),
          },
        ],
        borderColor: positionSelected ? '#6366f1' : (closing ? '#3a4560' : 'transparent'),
        rowBg: positionSelected ? 'rgba(99,102,241,.08)' : (closing ? 'rgba(134,149,171,.05)' : 'transparent'),
        addQty: Math.max(1, Math.floor(Number(p.addQty) || 1)),
        hotkeys: { ...s.hotkeys },
        onSelect: () => {
          this.selectPosition(i);
          const idx = this.TICKERS.findIndex((t) => t.sym === p.sym);
          if (idx !== -1) this.setFocus(idx);
        },
        onSellQtyWheel: (e) => {
          e.preventDefault();
          e.stopPropagation();
          const pos = [...this.state.positions];
          if (!pos[i]) return;
          const current = pos[i].sellQty || 1;
          const d = e.deltaY > 0 ? -1 : 1;
          const next = Math.max(1, Math.min(pos[i].qty, current + d));
          if (next === current) return;
          pos[i] = { ...pos[i], sellQty: next };
          e.currentTarget.value = String(next);
          this.setState({ positions: pos }, { soft: true, regions: ['positions'] });
          playUiScroll();
        },
        onSellQtyChange: (e) => {
          const pos = [...this.state.positions];
          if (!pos[i]) return;
          const next = Math.max(1, Math.min(pos[i].qty, parseInt(e.target.value, 10) || 1));
          pos[i] = { ...pos[i], sellQty: next };
          e.currentTarget.value = String(next);
          this.setState({ positions: pos }, { soft: true, regions: ['positions'] });
        },
        onControlClick: (e) => e.stopPropagation(),
        onPriceMenuToggle: (e) => {
          e.stopPropagation();
          const pos = this.state.positions.map((item, idx) => ({
            ...item,
            priceMenuOpen: idx === i ? !item.priceMenuOpen : false,
          }));
          this.setState({ positions: pos });
        },
        onTargetInput: (e) => {
          e.stopPropagation();
          this._updateTargetInput(i, e.currentTarget.value, e.currentTarget, false);
        },
        onTargetChange: (e) => {
          e.stopPropagation();
          this._updateTargetInput(i, e.currentTarget.value, e.currentTarget, true);
          this.setState({ positions: this.state.positions }, { soft: true, regions: ['positions'] });
        },
        onTargetWheel: (e) => {
          e.preventDefault();
          e.stopPropagation();
          const current = this.state.positions[i];
          if (!current) return;
          const next = Math.max(0, Math.min(1000, (Number(current.targetPct) || 0) + (e.deltaY > 0 ? -5 : 5)));
          if (next === Number(current.targetPct)) return;
          this._updateTargetInput(i, String(next), e.currentTarget, true);
          this.setState({ positions: this.state.positions }, { soft: true, regions: ['positions'] });
          playUiScroll();
        },
        onTargetKeyDown: (e) => {
          if (e.key === 'Enter') { e.preventDefault(); e.currentTarget.blur(); return; }
        },
        onSell: (e) => { e.stopPropagation(); this.sellPosition(i); },
        onClose: (e) => { e.stopPropagation(); this.closePos(i); }, onHalve: (e) => { e.stopPropagation(); this.halvePos(i); }, onThird: (e) => { e.stopPropagation(); this.thirdPos(i); },
        onAddQtyWheel: (e) => {
          e.preventDefault();
          e.stopPropagation();
          const pos = [...s.positions];
          const current = Math.max(1, Math.floor(Number(pos[i].addQty) || 1));
          const next = Math.max(1, current + (e.deltaY > 0 ? -1 : 1));
          if (next === current) return;
          pos[i] = { ...pos[i], addQty: next };
          e.currentTarget.value = String(next);
          this.setState({ positions: pos }, { soft: true, regions: ['positions'] });
          playUiScroll();
        },
        onAddQtyChange: (e) => { const pos = [...s.positions]; pos[i].addQty = Math.max(1, parseInt(e.target.value) || 1); this.setState({ positions: pos }, { soft: true, regions: ['positions'] }); },
        onAddPos: (e) => { e.stopPropagation(); this.addPos(i, p.addQty || 1); },
      };
    }) : [];
    // risk
    // ⑨ 限额、交易次数和已实现盈亏均取自 /ws 风控快照，与服务端下单闸门同口径。
    let risk = {};
    if (needRisk) {
    const r2 = s.risk2 || {};
    const maxDL = Number(r2.max_daily_loss) || 150, maxWL = Number(r2.max_weekly_loss) || 300;
    const tpv = Number(r2.daily_profit_target) || 300, maxTrades = Number(r2.max_trades_per_day) || 6;
    const trades = Number(r2.trades_today) || 0;
    const pnlReady = r2.pnl_basis === 'alpaca_program_fills';
    const dayPnl = pnlReady && r2.daily_pnl != null ? Number(r2.daily_pnl) : NaN;
    const weekPnl = pnlReady && r2.weekly_pnl != null ? Number(r2.weekly_pnl) : NaN;
    const dayPnlReady = Number.isFinite(dayPnl), weekPnlReady = Number.isFinite(weekPnl);
    const streak = Math.max(0, Number(r2.consecutive_losses) || 0);
    const cooldown = Math.max(0, Number(r2.cooldown_remaining_s) || 0);
    const money = (v) => (v > 0 ? '+$' : (v < 0 ? '−$' : '$')) + Math.abs(v).toFixed(2);
    const cooldownText = cooldown > 0
      ? '冷却剩余 ' + Math.floor(cooldown / 60) + ':' + String(Math.floor(cooldown % 60)).padStart(2, '0')
      : '未触发';
    const positionCount = s.positions.length;
    const positionQty = s.positions.reduce(
      (total, p) => total + Math.max(0, Number(p.qty) || 0),
      0,
    );
    const positionCapital = s.positions.reduce((total, p) => {
      const entryValue = Number(p.entryVal);
      if (Number.isFinite(entryValue)) return total + Math.max(0, entryValue);
      return total + Math.max(0, (Number(p.entryMid) || 0) * (Number(p.qty) || 0) * 100);
    }, 0);
    const closingCount = s.positions.filter((p) => p.closing).length;
    const unmanagedCount = s.unmanagedPositions.length;
    const positionStatus = [
      `${positionCount}笔 · ${positionQty}张`,
      closingCount ? `平仓中 ${closingCount}` : '',
      unmanagedCount ? `未登记 ${unmanagedCount}` : '',
    ].filter(Boolean).join(' · ');
    const tradesRatio = cb(trades / Math.max(1, maxTrades), 0, 1), tradesFull = trades >= maxTrades;
    risk = {
      pnlText: revealed ? (dayPnlReady ? money(dayPnl) : '—') : '••••',   // ⑧ 连带今日盈亏隐藏
      pnlColor: revealed && dayPnlReady ? (dayPnl >= 0 ? '#7fb79f' : '#d08a82') : '#5a6a82',
      tpLine: '+$' + tpv.toFixed(2), slLine: '−$' + maxDL.toFixed(2),
      breakerW: revealed && dayPnlReady ? (cb(dayPnl >= 0 ? dayPnl / tpv : -dayPnl / maxDL, 0, 1) * 100).toFixed(0) + '%' : '0%',
      breakerColor: revealed && dayPnlReady ? (dayPnl >= 0 ? '#5f9e83' : bar(cb(-dayPnl / maxDL, 0, 1))) : 'transparent',
      tradesText: String(trades), tradesMax: String(maxTrades),
      tradesW: (tradesRatio * 100).toFixed(0) + '%', tradesColor: tradesFull ? '#c1685f' : bar(tradesRatio),
      tradesBg: tradesFull ? 'rgba(193,104,95,.1)' : '#172236', tradesBorder: tradesFull ? '#5c2620' : '#22314a',
      weekPnlText: revealed ? (weekPnlReady ? money(weekPnl) : '—') : '••••',
      weekColor: revealed && weekPnlReady ? (weekPnl >= 0 ? '#7fb79f' : bar(cb(-weekPnl / maxWL, 0, 1))) : '#5a6a82',
      weekLine: '−$' + maxWL.toFixed(2),
      weekW: revealed && weekPnlReady ? (cb(Math.max(0, -weekPnl) / maxWL, 0, 1) * 100).toFixed(0) + '%' : '0%',
      streak: String(streak), streakColor: streak >= 2 ? '#d08a82' : '#c3cee0',
      streakBg: streak >= 2 ? 'rgba(193,104,95,.1)' : '#172236',
      streakBorder: streak >= 2 ? '#5c2620' : '#22314a',
      cooldownText,
      positionCapitalText: revealed
        ? ('$' + positionCapital.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 }))
        : '••••',
      positionStatus,
      positionStatusColor: unmanagedCount ? '#d8bd6f' : '#5a6a82',
    };
    }
    const footerKeys = !regions ? [
      ['字母', 'TICKER搜索'],
      ['1~9 / 0', '张数 / 10张'],
      ['Alt+' + s.hotkeys.buy, '买入 CALL'],
      ['Alt+' + s.hotkeys.buyPut, '买入 PUT'],
      ['Alt+' + s.hotkeys.sell, '卖出'],
      ['Alt+' + s.hotkeys.third, '1/3'],
      ['Alt+' + s.hotkeys.half, '半仓'],
      ['Alt+' + s.hotkeys.close, '单仓全平'],
      ['Alt+' + s.hotkeys.add, '加仓'],
      ['Esc', '撤单'],
      ['Alt+P', '隐藏'],
    ] : [];
    // ---- order flow（⑦ 订单流面板）----
    const PRICE_TYPE_LABEL = { bid: '买一', ask: '卖一', mid: '中间价', market: '市价', limit: '限价' };
    const sideMeta = (side) => side === 'sell'
      ? { sideLabel: '卖', sideBg: 'rgba(193,104,95,.14)', sideFg: '#d08a82' }
      : { sideLabel: '买', sideBg: 'rgba(95,158,131,.14)', sideFg: '#7fb79f' };
    const bjTime = (ts) => { const d = new Date(ts); const h = (d.getUTCHours() + 8) % 24; return [h, d.getUTCMinutes(), d.getUTCSeconds()].map((x) => String(x).padStart(2, '0')).join(':'); };
    // 标明“谁发起 + 做什么”。后端给结构化字段；reason 只负责详细触发理由。
    const reasonTag = (o) => {
      const user = { bg: 'rgba(95,158,131,.14)', fg: '#7fb79f' };
      const system = { bg: 'rgba(216,189,111,.14)', fg: '#d8bd6f' };
      const neutral = { bg: 'rgba(134,149,171,.14)', fg: '#8695ab' };
      if (o.origin === 'user' && o.action === 'open') return { label: '手动开仓', ...user };
      if (o.origin === 'user' && o.action === 'add') return { label: '手动加仓', ...user };
      if (o.origin === 'user' && o.action === 'reduce') return { label: '手动减仓', ...user };
      if (o.origin === 'user' && o.action === 'close') return { label: '手动全平', ...user };
      if (o.origin === 'system' && o.action === 'auto_close') {
        const detail = String(o.reason || '').replace(/^watchdog:/, '');
        return { label: '系统' + (detail ? ' · ' + detail : '自动平仓'), ...system };
      }
      if (o.origin === 'external') return { label: '外部程序', ...neutral };
      if (o.origin === 'program') return { label: '本程序 · 动作不明', ...neutral };
      if (!o.reason) return { label: '来源不明', ...neutral };
      if (o.side === 'buy') return { label: o.reason, bg: 'rgba(93,132,174,.14)', fg: '#85a9cf' };
      if (/^Mock\?+/.test(o.reason)) return { label: 'Mock 平仓', bg: 'rgba(99,102,241,.14)', fg: '#a5b0fb' };
      if (o.reason.startsWith('watchdog:')) return { label: '系统 · ' + o.reason.slice(9), bg: 'rgba(216,189,111,.14)', fg: '#d8bd6f' };
      if (o.reason === 'external_closed') return { label: '外部平仓', bg: 'rgba(134,149,171,.14)', fg: '#8695ab' };
      if (o.reason === 'closing_confirmed') return { label: '系统平仓', bg: 'rgba(216,189,111,.14)', fg: '#d8bd6f' };
      return { label: o.reason, bg: 'rgba(95,158,131,.14)', fg: '#7fb79f' };   // 手动平仓/自定义理由
    };
    const ords = needOrders
      ? (s.orders || []).slice().sort((a, b) => b.ts - a.ts)
      : [];
    const orderPrice = (value) => {
      if (value == null || String(value).trim() === '') return '';
      const n = Number(value);
      return '$' + (Number.isFinite(n) ? n.toFixed(2) : String(value));
    };
    const orderActionLabel = (o) => ({
      open: '开仓', add: '加仓', reduce: '减仓', close: '全平', auto_close: '自动平',
    }[o.action] || (o.side === 'sell' ? '卖出' : '买入'));
    const elapsedText = (start, end) => {
      const ms = Math.max(0, Number(end) - Number(start));
      if (!Number.isFinite(ms)) return '—';
      if (ms < 1000) return Math.round(ms) + 'ms';
      if (ms < 60000) return (ms / 1000).toFixed(ms < 10000 ? 1 : 0) + 's';
      const minutes = Math.floor(ms / 60000);
      const seconds = Math.floor((ms % 60000) / 1000);
      return minutes < 60 ? `${minutes}m${seconds}s` : `${Math.floor(minutes / 60)}h${minutes % 60}m`;
    };
    const compactCancelReason = (o) => {
      const text = String(o.failReason || '');
      if (/系统追价换单|撤销上一档/.test(text)) return '追价换档，前单已撤';
      if (/挂单超时|超时.*自动撤/.test(text)) return '挂单超时，系统已撤';
      if (/拒绝|rejected/i.test(text)) return '券商拒单';
      if (o.cancelOrigin === 'user') return '手动撤单';
      if (/Alpaca.*确认取消|已确认取消/.test(text)) return '券商已取消';
      const unique = [...new Set(text.split(/\s*[·；]\s*/).filter(Boolean))];
      const short = unique.slice(0, 2).join(' · ');
      return short.length > 28 ? short.slice(0, 27) + '…' : (short || '已取消');
    };
    const mkOrd = (o, withCancel) => {
      const m = sideMeta(o.side);
      const tt = o.typeText || (o.type === 'CALL' ? 'CALL' : o.type === 'PUT' ? 'PUT' : '');
      const typeColor = o.type === 'CALL' ? '#5f9e83' : o.type === 'PUT' ? '#c1685f' : '#8695ab';
      const tag = reasonTag(o);
      const selected = !!(
        s.cancelTarget
        && s.cancelTarget.kind === 'order'
        && s.cancelTarget.orderId === o.oid
      );
      const cancelBusy = !!(
        o.cancelOrigin
        || (this._cancelBusy && this._cancelBusy.has(o.oid))
      );
      const it = {
        sym: o.sym, optCode: o.optCode ? o.optCode.replace(/^([a-zA-Z]+)\s*/, '') : '', qty: o.qty,
        targetText: `${o.sym} ${tt} ×${o.qty}`.trim(),
        sideLabel: m.sideLabel, sideBg: m.sideBg, sideFg: m.sideFg,
        actionLabel: orderActionLabel(o), actionColor: m.sideFg,
        timeText: bjTime(o.updatedTs || o.ts), latencyText: elapsedText(o.submittedTs, o.updatedTs),
        typeText: tt, typeColor, reasonTag: tag, reasonText: o.reason || (tag ? tag.label : '—'),
        rowBg: selected ? 'rgba(99,102,241,.13)' : 'rgba(194,161,86,.08)',
        rowBorder: selected ? '#6366f1' : '#4d4320',
        onSelect: () => this.selectOrder(o),
      };
      // 委托价只认实际发给券商的 limit_price；成交价只认券商回填的 price。
      // 市价单没有固定挂单价，明确展示“挂 市价”，绝不拿盘口估值代替。
      const priceTypeLabel = PRICE_TYPE_LABEL[o.priceType] || '';
      it.orderPriceValue = o.limitPrice != null ? orderPrice(o.limitPrice) : '—';
      it.priceTypeLabel = priceTypeLabel || '—';
      it.fillPriceValue = o.fillPrice != null ? orderPrice(o.fillPrice) : '—';
      it.orderPriceText = o.limitPrice != null
        ? `挂 ${orderPrice(o.limitPrice)}${priceTypeLabel ? ' · ' + priceTypeLabel : ''}`
        : o.priceType === 'market' ? '挂 市价' : '挂价 —';
      it.fillPriceText = o.fillPrice != null ? `成 ${orderPrice(o.fillPrice)}` : '成交价 —';
      it.cancelPriceText = o.priceType === 'market'
        ? '市价'
        : `${it.orderPriceValue}${priceTypeLabel ? ' · ' + priceTypeLabel : ''}`;
      const notional = Number(o.notional);
      it.notionalText = Number.isFinite(notional)
        ? '$' + notional.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
        : '—';
      it.failReason = o.failReason || '';
      it.cancelReasonText = compactCancelReason(o);
      it.cancelBusy = cancelBusy;
      it.cancelLabel = cancelBusy ? '撤单中…' : '撤单';
      it.cancelCursor = cancelBusy ? 'wait' : 'pointer';
      if (withCancel) it.onCancel = (e) => {
        if (e) e.stopPropagation();
        this.selectOrder(o);
        this.cancelOrder(o);
      };
      return it;
    };
    const oPend = ords.filter((o) => o.status === 'pending');
    const oFill = ords.filter((o) => o.status === 'filled');
    const oCanc = ords.filter((o) => o.status === 'cancelled');
    const orderFlow = {
      pending: oPend.map((o) => mkOrd(o, true)),
      filled: oFill.slice(0, 20).map((o) => mkOrd(o, false)),
      cancelled: oCanc.slice(0, 12).map((o) => mkOrd(o, false)),
      nPending: oPend.length, nFilled: oFill.length, nCancelled: oCanc.length,
      hasPending: oPend.length > 0, hasFilled: oFill.length > 0, hasCancelled: oCanc.length > 0,
      empty: ords.length === 0,
    };
    // ---- settings ----
    const ro = false;
    const STR_KEYS = new Set(['tp']);
    const parse = (k, v) => STR_KEYS.has(k) ? v : (v === '' ? '' : Number(v));
    const mkField = (k) => ({
      key: k,
      value: s.draft[k], onChange: (e) => this.setDraft(k, parse(k, e.target.value)),
      // 逐字输入：只改 state + 刷新保存按钮，不重建 DOM（重建会打断输入/中文输入法）
      onInput: (e) => { this.state.draft[k] = parse(k, e.target.value); this.refreshSaveUI(); },
      pending: s.pendingKeys.includes(k), border: s.pendingKeys.includes(k) ? '#4d4320' : '#22314a',
      opacity: ro ? '0.55' : '1',
      options: null, noOptions: true,
    });
    const item = (label, key, unit, hint) => ({
      label, unit, hint, labelFull: unit ? label + '（' + unit + '）' : label, field: mkField(key),
    });
    // 2026-08-26「趋势松紧档位」删除后设置页已无下拉型参数，enumItem 一并删掉。
    // index.html 那边的 <select> 渲染分支保留着（引擎通用能力），下次再加枚举参数
    // 时照 mkField 复制一份 { noOptions: false, options } 即可。
    // 风控参数区 = 所有阀门（入场闸门 / 熔断冷却 / 出场止盈止损），系统参数另卡（界面偏好等）
    const groups = needSettings ? [
      { title: '入场闸门', icon: 'bi-lightning-charge', iconColor: '#818cf8', items: [
        item('开盘禁入', 'blackoutMin', '分钟', '开盘后 N 分钟内拒绝入场'),
        item('午后禁入线', 'cutoffMins', '分钟', '开盘 N 分钟后不再开新仓'),
        // 2026-09-01 撤掉五个输入框：追高 σ 阈值、震荡闸门槛、5分钟死区、
        // 动能泄气上/下限。对应的四条闸同日删除，参数已从模板 schema 移除。
        item('最低 delta', 'minDelta', '',
             '合约 delta 低于即拒绝。⚠️ 当前行情档（免费 indicative）不提供 greeks，'
             + '拿不到真 delta，这项判据不生效——买的是最贴平值的合约，'
             + '平值 delta 本来就在 0.5 附近。签 OPRA 后自动恢复'),
        item('最大点差', 'maxSpreadPct', '%', '合约 spread 高于即拒绝'),
        item('日交易次数上限', 'maxTrades', '次', '达上限当日锁死下单'),
      ] },
      { title: '熔断与冷却', icon: 'bi-exclamation-triangle', iconColor: '#f87171', items: [
        item('日亏熔断', 'dayLoss', '$', '当日锁死下单'),
        item('周亏上限', 'weekLoss', '$', '触及暂停本周'),
        item('连亏冷却', 'cooldownMin', '分钟', '连亏 2 单后强制停手'),
        item('日止盈锁利', 'dayTarget', '$', '达标后锁定当日利润'),
      ] },
      { title: '出场（止盈止损）', icon: 'bi-box-arrow-right', iconColor: '#4ade80', items: [
        item('单笔止损', 'stopPct', '%', '触发即市价平仓'),
        item('金额止损线', 'dollarLine', '$', '权利金亏损跌破即平'),
        item('最长持仓', 'maxHold', '分钟', '超时强制平仓'),
        item('挂单超时撤单', 'orderTimeout', '秒', 'watchdog：在途单挂满即撤（0=停用）'),
        item('止盈规则', 'tp', '', '非移动止盈，盘中不追不改', '220px'),
      ] },
    ] : [];
    const hotkeyError = needSettings ? this.hotkeyErrors(s.hotkeyDraft) : '';
    const hotkeys = needSettings ? Object.keys(HOTKEY_DEFAULTS).map((action) => ({
      action,
      act: HOTKEY_LABELS[action],
      value: s.hotkeyDraft[action],
      onInput: (e) => this.setHotkeyDraft(action, e.currentTarget.value, e.currentTarget),
    })) : [];
    const changed = needSettings ? this.changedKeys().length : 0;
    const hasConfigName = !!s.configName.trim();
    const hasReason = !!s.reason.trim();
    const canSave = !ro && hasConfigName && hasReason && changed > 0;
    const save = {
      onSave: () => this.save(), bg: canSave ? '#6366f1' : '#1c2740', border: canSave ? '#6366f1' : '#22314a', fg: canSave ? '#fff' : '#4d5c73', cursor: canSave ? 'pointer' : 'not-allowed',
      hint: ro ? '美股盘中锁定' : (changed === 0 ? '无改动' : (!hasConfigName ? '需填写配置名称' : (!hasReason ? '需填写修改理由' : String(changed) + ' 项改动 · 仅保存'))),
      hintColor: (changed > 0 && (!hasConfigName || !hasReason)) ? '#d08a82' : '#5a6a82',
    };
    const bindings = {
      isTrade: s.page === 'trade', isSettings: s.page === 'settings', heartbeatLost: hbLost,
      clock, mode, hb, alpacaPong, tide: this.tideView(),
      session: this.sessionView(),
      tickerCount: this.TICKERS.length, tickers, focusT, order, selfCheck,
      posCount: s.positions.length, posEmpty: s.positions.length === 0, positions, revealed, hidden: !revealed,
      revealBtnLabel: revealed ? '隐藏盈亏' : '显示盈亏',
      reconcile: {
        onClick: () => this.reconcile(), label: s.reconciling ? '对账中…' : '对账',
        icon: s.reconciling ? 'bi-arrow-repeat' : 'bi-arrow-repeat',
        iconStyle: s.reconciling ? 'animation:spin 1s linear infinite;display:inline-block' : '',
      },
      hasUnmanaged: s.unmanagedPositions.length > 0, unmanagedCount: s.unmanagedPositions.length,
      unmanagedSummary: s.unmanagedPositions.map((u) => u.ticker + ' ' + u.type + ' ×' + u.qty).join('、'),
      qtyPresets, qtyValue: s.qty, onQtyInput: (e) => this.onQtyInput(e), onQtyWheel: (e) => this.onQtyWheel(e), stopModes,
      priceTypes, showLimitInput: s.priceType === 'limit', limitValue: s.limitPrice, onLimitInput: (e) => this.onLimitInput(e),
      orderFlow,
      risk, footerKeys,
      gotoSettings: () => this.gotoSettings(), gotoTrade: () => this.gotoTrade(),
      reveal: () => this.reveal(), closeAll: () => this.closeAll(), onSignalWheel: this.onSignalWheel,
      readonly: ro, groups, hotkeys,
      fixedHotkeys: [
        { key: '1–0', act: '设置买入张数（0 = 10张）' },
        { key: '1 / 2 / 3', act: '下单自查弹窗：拨对应开关（Enter 提交 · Esc 取消）' },
        { key: '↑ / ↓', act: '选择持仓' },
        { key: 'Esc', act: '撤销选中的在途单' },
        { key: 'Alt+P', act: '显示 / 隐藏盈亏' },
      ],
      hotkeySave: () => this.saveHotkeys(),
      hotkeyReset: () => this.resetHotkeys(),
      hotkeyHint: hotkeyError || '保存后立即生效，并保存在当前浏览器',
      hotkeyHintColor: hotkeyError ? '#d08a82' : '#5f9e83',
      hotkeySaveDisabled: !!hotkeyError,
      hotkeySaveOpacity: hotkeyError ? '.45' : '1',
      activeTemplate: s.activeTemplate || '—', pendingTemplate: s.pendingTemplate || '',
      hasPendingTemplate: !!s.pendingTemplate,
      loadedTemplate: s.loadedTemplate || '—',
      loadedPending: s.loadedTemplateStatus === 'pending',
      loadedActive: s.loadedTemplateStatus === 'active',
      configs: needSettings ? s.configs.map((v) => ({
        ...v,
        canLoad: v.canLoad,
        canDelete: !ro && v.canDelete,
        loadDisabled: s.marketLocked,
        loadTitle: s.marketLocked ? '美股盘中不可加载' : '加载并立即生效',
        loadCursor: s.marketLocked ? 'not-allowed' : 'pointer',
        loadOpacity: s.marketLocked ? '.45' : '1',
      })) : [],
      marketLockNotice: s.marketLocked ? '美股盘中不可加载 · 可继续保存候选配置' : '',
      marketLocked: s.marketLocked,
      configName: {
        value: s.configName, onChange: (e) => this.setState({ configName: e.target.value }),
        onInput: (e) => { this.state.configName = e.target.value; this.refreshSaveUI(); },
        opacity: ro ? '0.55' : '1',
      },
      reason: {
        value: s.reason, onChange: (e) => this.setState({ reason: e.target.value }),
        onInput: (e) => { this.state.reason = e.target.value; this.refreshSaveUI(); },
        opacity: ro ? '0.55' : '1',
      }, save,
      toggleSound: () => this.toggleSound(),
      soundBg: s.sound ? '#6366f1' : '#1c2740', soundBorder: s.sound ? '#6366f1' : '#22314a', soundKnob: s.sound ? '22px' : '2px',
      toggleGexAlert: () => this.toggleGexAlert(),
      gexAlertBg: s.gexAlert ? '#6366f1' : '#1c2740', gexAlertBorder: s.gexAlert ? '#6366f1' : '#22314a', gexAlertKnob: s.gexAlert ? '22px' : '2px',
    };
    if (!regions) return bindings;
    const factories = {
      tickers: this.renderTickersBindings,
      'order-entry': this.renderOrderEntryBindings,
      positions: this.renderPositionsBindings,
      orders: this.renderOrdersBindings,
      risk: this.renderRiskBindings,
      settings: this.renderSettingsBindings,
    };
    const selected = this.renderCommonBindings(bindings);
    regions.forEach((region) => {
      const factory = factories[region];
      if (factory) Object.assign(selected, factory.call(this, bindings));
    });
    return selected;
  }
};
