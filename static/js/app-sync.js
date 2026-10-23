/* ----------------------------------------------------------------------------
 * /ws 分区数据落地：风控、券商对账、订单、持仓、合约预览
 *
 * 只做「后端下发的一帧 → 前端 state」这一步，不发请求、不做交易决策。
 * 每个 apply* 都先过 acceptSectionRevision() 挡住乱序旧帧。
 * --------------------------------------------------------------------------*/

import { scheduleRender } from './runtime.js';
import { candidateNote, parseOptionCode } from './format.js';
import { playSfx } from './sfx.js';

export const withSync = (Base) => class extends Base {
  // ⑨ 应用 /ws 风控快照（今日交易次数真实 + 各熔断线）
  applyRisk(d, revision) {
    if (!d || typeof d !== 'object') return;
    if (!this.acceptSectionRevision('risk', revision)) return;
    this.setState(
      { risk2: d },
      { soft: true, regions: ['risk', 'tickers', 'order-entry'] },
    );
  }
  // Alpaca TradingStream 已由后端维持协议层 Ping/Pong；这里只消费连接状态和最近 RTT。
  applyBrokerSync(d, revision) {
    if (!d || typeof d !== 'object') return;
    if (!this.acceptSectionRevision('broker_sync', revision)) return;
    const pongMs = Number(d.pong_ms);
    const baselineReady = !!(
      d.order_baseline && d.order_baseline.last_synced_at
    );
    this._orderBaselineReady = baselineReady;
    this.state.alpacaConnected = d.connected === true;
    this.state.alpacaPongMs = Number.isFinite(pongMs) && pongMs > 0 ? Math.round(pongMs) : null;
    this.refreshHeartbeat();
  }
  // 应用 /ws 真实订单流。
  applyOrders(g, revision) {
    if (!g) return;
    if (!this.acceptSectionRevision('orders', revision)) return;
    let flat = [];
    const push = (arr, status) => (arr || []).forEach((e) => {
        const { type: pType, typeText: pTypeText } = parseOptionCode(e.option_code);
        const type = pType || e.type || '';
        flat.push({
          oid: e.order_id, sym: e.ticker, optCode: e.option_code || '', type: type, typeText: pTypeText || type, side: e.side, qty: e.qty, status,
          reason: e.reason || null,
          origin: e.origin || 'unknown', action: e.action || 'unknown',
          clientOrderId: e.client_order_id || null,
          parentOrderId: e.parent_order_id || null,
          cancelOrigin: e.cancel_origin || null, cancelCause: e.cancel_cause || null,
          brokerStatus: e.broker_status || null,
          // ⑦ 审查 2.9：待成交面板要看得出挂的什么价、多少钱；未成交要看得出为什么
          priceType: e.price_type || null, limitPrice: e.limit_price || null,
          // filled 的 price 是券商确认的实际成交均价；不可用委托价或盘口价代替。
          fillPrice: e.price || null,
          notional: e.notional || null, failReason: e.fail_reason || null,
          submittedTs: Date.parse(e.ts) || Date.now(),
          updatedTs: Date.parse(e.updated_ts || e.ts) || Date.now(),
          ts: Date.parse(e.updated_ts || e.ts) || Date.now(),
        });
    });
    push(g.pending, 'pending'); push(g.filled, 'filled'); push(g.cancelled, 'cancelled');
      // 兼容修复前已留在内存日志里的重复平仓：close_run 的真实卖单与
      // watchdog 旧版 :ext 合成卖单若是同合约/同张数/近时刻，只展示真实单。
    const realFilledSells = flat.filter((o) =>
      o.status === 'filled' && o.side === 'sell' && !String(o.oid).endsWith(':ext'));
    flat = flat.filter((o) => !(
      o.status === 'filled' && o.side === 'sell' && o.reason === 'closing_confirmed'
      && String(o.oid).endsWith(':ext')
      && realFilledSells.some((r) =>
        r.optCode === o.optCode && r.qty === o.qty && Math.abs(r.ts - o.ts) <= 5000)
    ));
      // 成交音：买入/加仓/减仓/平仓统一只认后端订单流里**券商已确认成交**的新订单。
      // :ext / external_closed 是持仓对账合成事件，不是 Alpaca 订单成交，不播放。
    // 首拍只建基线（不为重启前的历史成交补响一堆声音）。
    this._seenFilledOrders = this._seenFilledOrders || new Set();
    const baselineReady = this._orderBaselineReady === true;
    const baselineDone = this._filledSeenInit === true;
    const newFills = [];
    (g.filled || []).forEach((e) => {
      const orderId = String(e.order_id || '');
      const brokerFill = orderId && !orderId.endsWith(':ext') && e.reason !== 'external_closed';
      if (!baselineReady || !brokerFill || this._seenFilledOrders.has(orderId)) return;
      this._seenFilledOrders.add(orderId);
      if (baselineDone) newFills.push(e);
    });
    if (baselineReady) this._filledSeenInit = true;
    if (newFills.length) {
      // 风险退出有时会在同一后端拍内完成，持仓快照来不及暴露 transient
      // risk_exit_state；用新出现的 auto_close 终态补响一次，并按合约去重。
      this._riskSoundContracts = this._riskSoundContracts || new Set();
      const fallbackRiskFills = newFills.filter((e) =>
        e.action === 'auto_close'
        && /(止损|超时)/.test(String(e.reason || ''))
        && !this._riskSoundContracts.has(e.option_code));
      if (fallbackRiskFills.length) {
        fallbackRiskFills.forEach((e) => this._riskSoundContracts.add(e.option_code));
        playSfx('risk');
      }
      // 自动风险退出在触发时已经播放止损音；终态成交只弹提示，不重复播放普通成交音。
      if (newFills.some((e) => e.action !== 'auto_close')) playSfx('fill');
      newFills.forEach((e) => {
        const actionLabel = e.action === 'add' ? '加仓成交'
          : e.action === 'reduce' ? '减仓成交'
            : (e.action === 'close' || e.action === 'auto_close') ? '平仓成交'
              : e.side === 'sell' ? '卖出成交' : '买入成交';
        const parsed = parseOptionCode(e.option_code);
        const contract = parsed.typeText || e.option_code || '';
        const qty = Math.max(0, Number(e.qty) || 0);
        const price = e.price != null && String(e.price) !== '' ? ' @$' + e.price : '';
        this.toast(`${actionLabel} · ${e.ticker || ''}${contract ? ' ' + contract : ''} ×${qty}${price}`, 'fill', 6000);
      });
    }
    // 取消音只依据后端结构化 cancel_cause，并且只在券商终态首次出现时播放。
    // risk_preempt 是止损流程的内部“先撤止盈”，不额外出声。
    this._seenCancelledOrders = this._seenCancelledOrders || new Set();
    const cancelBaselineDone = this._cancelledSeenInit === true;
    const newCancels = [];
    (g.cancelled || []).forEach((e) => {
      const orderId = String(e.order_id || '');
      if (!baselineReady || !orderId || this._seenCancelledOrders.has(orderId)) return;
      this._seenCancelledOrders.add(orderId);
      if (cancelBaselineDone) newCancels.push(e);
    });
    if (baselineReady) this._cancelledSeenInit = true;
    if (newCancels.some((e) => e.cancel_cause === 'manual')) playSfx('cancel');
    if (newCancels.some((e) => e.cancel_cause === 'timeout')) playSfx('risk');

    let cancelTarget = this.state.cancelTarget;
    if (
      cancelTarget
      && cancelTarget.kind === 'order'
      && !flat.some((o) => o.status === 'pending' && o.oid === cancelTarget.orderId)
    ) {
      cancelTarget = null;
    }
    this.setState(
      { orders: flat, cancelTarget },
      { soft: true, regions: ['orders', 'positions'] },
    );
  }
  // 应用 /ws 真实持仓 + 实时报价。
  applyPositions(g, revision) {
    if (!g || !Array.isArray(g.positions)) return;
    if (!this.acceptSectionRevision('positions', revision)) return;
    const prev = this.state.positions;
    const positionBaselineDone = this._positionsSeenInit === true;
    const riskExitContracts = [];
    this._closeErrShown = this._closeErrShown || new Set();
    const positions = g.positions.map((p) => {
        const type = p.direction === 'long' ? 'CALL' : 'PUT';
        const entryMid = Number(p.entry_mid) || 0;
        const qty = p.qty;
        // 浮盈亏按**可变现价值(bid)**算，与 watchdog 止损同口径（审查 3.1）。
        // 用 mid 的话会出现"界面显示 −17%、系统却按 −22% 触发了止损"，用户会以为系统乱来。
        // entry_mid 那边已回填券商真实成交价（≈ask，见 3.2），两头合起来就是真实往返盈亏。
        const quoteBid = p.quote ? Number(p.quote.bid) : NaN;
        const quoteAsk = p.quote ? Number(p.quote.ask) : NaN;
        const quoteMid = p.quote ? Number(p.quote.mid) : NaN;
        const hasMarketPrice = Number.isFinite(quoteBid);
        const px = hasMarketPrice ? quoteBid : entryMid;
        const unreal = Math.round((px - entryMid) * 100 * qty);
        const pnlPct = entryMid > 0 ? ((px / entryMid - 1) * 100) : 0;
        const entryVal = Math.round(entryMid * 100 * qty);
        const existing = prev.find((x) => x.optionCode === p.option_code);
        // 服务重启后 REGISTRY 可能先恢复持仓、后恢复 close_order_id。仅当订单流里存在
        // 唯一一张同合约、本程序语义的在途卖单时才临时回绑；多张时绝不猜。
        const recoveredCloseOrders = (this.state.orders || []).filter((o) =>
          o.status === 'pending'
          && o.side === 'sell'
          && o.optCode === p.option_code
          && ['user', 'system', 'program'].includes(o.origin)
          && ['reduce', 'close', 'auto_close'].includes(o.action)
          && String(o.clientOrderId || '').startsWith('ttt-')
          && o.parentOrderId === p.order_id
        );
        const recoveredCloseOrder = recoveredCloseOrders.length === 1
          ? recoveredCloseOrders[0] : null;
        const closeOrderId = p.close_order_id
          || (recoveredCloseOrder ? recoveredCloseOrder.oid : null);
        const backendClosing = p.status === 'closing' || !!recoveredCloseOrder;
        const optimisticClosing = !!(
          this._optimisticCloseContracts
          && this._optimisticCloseContracts.has(p.option_code)
          && !p.close_error
        );
        if (
          (backendClosing || p.close_error)
          && this._optimisticCloseContracts
        ) {
          // 后端已接管状态或明确失败；不再需要本地锁。
          this._optimisticCloseContracts.delete(p.option_code);
        }
        if (
          positionBaselineDone
          && p.risk_exit_state
          && !String(p.risk_exit_reason || '').includes('触墙止盈')
          && !(existing && existing.riskExitState)
        ) {
          riskExitContracts.push(p.option_code);
        }
        // 首份持仓快照只建立基线：强刷不能把刷新前已经存在的错误重新当成新事件弹 Toast。
        // 后续由无错误→有错误的真实状态变化仍会红色提醒一次。
        if (p.close_error && !this._closeErrShown.has(p.option_code)) {
          this._closeErrShown.add(p.option_code);
          if (positionBaselineDone) {
            this.toast('平仓未成交，请重试 · ' + p.ticker + ' · ' + p.close_error, 'block');
          }
        }
        if (!p.close_error) this._closeErrShown.delete(p.option_code);  // 新平仓清了错误→允许下次再弹
        // 张数与券商对不上并已按券商校正 → 必须让人看见，不能悄悄错着（审查 7.4③）
        this._syncNoteShown = this._syncNoteShown || new Set();
        if (p.sync_note && !this._syncNoteShown.has(p.option_code)) {
          this._syncNoteShown.add(p.option_code);
          if (positionBaselineDone) {
            this.toast('持仓与 Alpaca 不一致 · ' + p.ticker + ' · ' + p.sync_note, 'block');
          }
        }
        if (!p.sync_note) this._syncNoteShown.delete(p.option_code);
        return {
          id: existing ? existing.id : (this._nid++),
          sym: p.ticker, type, qty, optionCode: p.option_code,
          entryOrderId: p.order_id,
          marketPrice: hasMarketPrice ? quoteBid : null,
          quoteBid: Number.isFinite(quoteBid) ? quoteBid : null,
          quoteAsk: Number.isFinite(quoteAsk) ? quoteAsk : null,
          quoteMid: Number.isFinite(quoteMid) ? quoteMid : null,
          entryMid,
          pnlPct, unreal, realized: Number(p.realized_pnl || 0), val: entryVal + unreal, entryVal,
          startTs: Date.parse(p.entry_time) || Date.now(),
          maxSec: Number(p.max_hold_s) || 900,
          stopPct: p.stop_pct != null ? Number(p.stop_pct) * 100 : null,   // 该笔入场时冻结的止损%（非当前设置值）
          dollarLine: p.dollar_line != null ? Number(p.dollar_line) : null,
          stopMode: p.stop_mode === 'amount' ? 'amount' : 'percent',
          addQty: existing && Number.isFinite(Number(existing.addQty))
            ? Math.max(1, Math.floor(Number(existing.addQty))) : 1,
          sellQty: existing ? Math.max(1, Math.min(qty, existing.sellQty || 1)) : 1,
          exitPriceType: existing ? (existing.exitPriceType || 'target') : 'target',
          targetPct: existing && Number.isFinite(Number(existing.targetPct))
            ? Math.max(0, Number(existing.targetPct)) : 20,
          targetInput: existing && existing.targetInput != null
            ? String(existing.targetInput) : '20',
          priceMenuOpen: existing ? !!existing.priceMenuOpen : false,
          // HTTP 已受理到后端快照确认之间，忽略一拍较旧的 open，避免按钮灰→亮→灰。
          closing: backendClosing || optimisticClosing,
          closeOrderId,   // 非空=平仓挂单在券商排队（含重启后唯一确定回绑，可撤）
          cancelRequested: (
            p.cancel_requested === true
            || !!(recoveredCloseOrder && recoveredCloseOrder.cancelOrigin)
          ),
          closeTimeoutPolicy: p.close_timeout_policy || 'quick',
          riskExitState: p.risk_exit_state || null,
          riskExitReason: p.risk_exit_reason || null,
          syncNote: p.sync_note || null,            // 非空=张数曾与券商不一致、已按券商校正
        };
    });
      // 【2026-07-25 删除】此处原有"成交检测"：上一拍 closing、这一拍从列表消失就判定已成交，
      // 伪造一条 status:'filled'、qty=整仓 的卖单塞进订单流并播成交音。
      // 但"仓位从 /ws 快照消失"根本不等于券商确认成交——进程重启清空 REGISTRY、
      // watchdog 判 external_closed、甚至一次空响应，都会让浏览器凭空造出"已卖出"记录
      // （QQQ 事件：界面说卖了，Alpaca 仍 2 手、1 手挂着）。
      // 成交只能由后端从券商确认后经 /ws 订单流给出，前端不得推断、不得伪造。
    this._positionsSeenInit = true;
    this._riskSoundContracts = this._riskSoundContracts || new Set();
    const newRiskSounds = riskExitContracts.filter(
      (optionCode) => !this._riskSoundContracts.has(optionCode),
    );
    if (newRiskSounds.length) {
      newRiskSounds.forEach((optionCode) => this._riskSoundContracts.add(optionCode));
      playSfx('risk');
    }
    const activeContracts = new Set(positions.map((p) => p.optionCode));
    if (this._optimisticCloseContracts) {
      this._optimisticCloseContracts.forEach((optionCode) => {
        if (!activeContracts.has(optionCode)) this._optimisticCloseContracts.delete(optionCode);
      });
    }
    this._riskSoundContracts.forEach((optionCode) => {
      if (!activeContracts.has(optionCode)) this._riskSoundContracts.delete(optionCode);
    });
    this.setState((s) => {
      let cancelTarget = s.cancelTarget;
      if (cancelTarget && cancelTarget.kind === 'position') {
        const selected = positions.find((p) => p.optionCode === cancelTarget.optionCode);
        cancelTarget = selected
          ? {
              kind: 'position',
              optionCode: selected.optionCode,
              orderId: selected.closeOrderId || null,
            }
          : null;
      }
      return {
        positions,
        cancelTarget,
        unmanagedPositions: Array.isArray(g.unmanaged) ? g.unmanaged : [],
        posFocus: positions.length ? Math.max(0, Math.min(positions.length - 1, s.posFocus)) : 0,
      };
    }, { soft: true, regions: ['positions', 'risk'] });
  }
  // 真·对账：扫真实券商账户，做两件事——
  //   ① 把"券商已成交、系统还当未成交"的仓转成持仓（2026-08-25：watchdog 卡死 8 分 48 秒，
  //      一笔真实成交的 QQQ 在持仓总览里空窗 4 分钟，看不到也砍不掉；当时点对账没用，
  //      因为老逻辑只找孤儿仓，而这笔仓登记表里是有的，只是状态没更新）；
  //   ② 把 REGISTRY 漏登记的孤儿仓找回来（重启/热重载清过内存态后常见）。
  // 跟顶部「演示」里的"对账中"是两回事——那个只是设计稿状态预览，不是真动作。
  reconcile() {
    if (this.state.reconciling) return;
    this.setState({ reconciling: true });
    fetch('/api/reconcile', { method: 'POST' }).then((r) => r.json()).then((res) => {
      this.setState({ reconciling: false });
      if (!res) { this.toast('对账请求失败', 'block'); return; }
      // fetch 对 4xx/5xx 不会 reject：后端拒绝时 res 是 {detail:"..."}，
      // 两个计数都会是 0，不拦的话会弹一条「持仓与券商一致」的**成功**提示。
      if (!Array.isArray(res.recovered)) {
        this.toast('对账被拒绝 · ' + (res.detail || '后端未返回结果'), 'block');
        return;
      }
      const n = res.recovered.length;
      const fixed = (res.resolved_pending || []).length;
      // 两类结果要分开说：找回孤儿仓 vs 补上漏掉的成交，用户排查方向完全不同。
      if (fixed > 0 && n > 0) {
        this.toast(`对账：补上 ${fixed} 笔漏掉的成交 · 找回 ${n} 笔未登记持仓`, 'warn');
      } else if (fixed > 0) {
        this.toast(`对账：补上 ${fixed} 笔已成交但没显示的持仓`, 'warn');
      } else if (n > 0) {
        this.toast(`对账找回 ${n} 笔未登记持仓`, 'warn');
      } else {
        this.toast('对账完成，持仓与券商一致', 'ok');
      }
    }).catch(() => { this.setState({ reconciling: false }); this.toast('对账请求失败', 'block'); });
  }
  // ③ 合约预览随 /ws options.previews 下发；REST 仅保留人工诊断，不进入前端实时循环。
  // 2026-08-08 起每票下发 sides={long,short} 两份，前端各存一份（t.cLong / t.cShort）。
  applyOptionPreviews(options, revision) {
    // 无合约可显示时，把「为什么没有」说清楚，见下方 candidateNote。
    const previews = options && options.previews;
    if (!previews || typeof previews !== 'object') return;
    if (!this.acceptSectionRevision('options', revision)) return;
    for (const [sym, preview] of Object.entries(previews)) {
      const t = this.TICKERS.find((item) => item.sym === sym);
      if (!t) continue;
      t.candidateHeld = !!preview && preview.held === true;
      const sides = (preview && preview.sides) || {};
      t.cLong = this._sideContract(sym, sides.long, t.cLong, t.candidateHeld);
      t.cShort = this._sideContract(sym, sides.short, t.cShort, t.candidateHeld);
    }
    scheduleRender({ soft: true, regions: ['tickers', 'order-entry'] });
  }
  // 把一侧的候选快照翻译成卡片展示对象。返回值形状恒定，渲染层不必判空。
  _sideContract(sym, side, old, held) {
    const prev = old || {};
    if (!side || !side.selected) {
      // 后端一直下发 status/error/gate，此前全被丢掉，DISCOVERING（还没轮到选约）
      // 与 ERROR（选不出合约）渲染成同一串横杠，用户无从判断该等还是该换标的。
      // 选约是异步的：票池 30+ 个标的 × 两个方向、单次约 0.85s，新放行的标的
      // 排到队尾可能要等十几秒才有合约（2026-08-03 IREN 即此）。
      return {
        ...prev, name: sym + candidateNote(side, held), delta: '—', spread: '—',
        bid: '—', mid: '—', ask: '—', optionCode: null,
      };
    }
    const meta = side.selected_contract
      || (Array.isArray(side.shortlist)
        ? side.shortlist.find((item) => item.option_code === side.selected)
        : null)
      || {};
    const quote = side.quote || meta;
    const { typeText } = parseOptionCode(side.selected);
    return {
      ...prev,
      name: sym + ' ' + (typeText || (side.direction === 'long' ? 'C?' : 'P?')),
      // delta 拿不到时显示「—」而不是「≈0.50」：那个数是编的，不该摆在卡片上
      // 冒充一个读数（平值合约 delta 本来就在 0.5 附近，写出来也没增加信息）。
      delta: meta.delta == null ? '—' : Number(meta.delta).toFixed(2),
      spread: quote.spread_pct == null
        ? '—'
        : (Number(quote.spread_pct) * 100).toFixed(1) + '%',
      bid: quote.bid != null ? quote.bid : '—',
      mid: quote.mid != null ? quote.mid : '—',
      ask: quote.ask != null ? quote.ask : '—',
      optionCode: side.selected,
    };
  }
};
