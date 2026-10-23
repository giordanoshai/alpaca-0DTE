/* ----------------------------------------------------------------------------
 * 下单与平仓：自查弹窗、开仓、撤单、平仓 / 减仓 / 加仓 / 目标价
 *
 * 一句纪律贯穿全文件：**平仓一律走后端原子接口**，前端绝不自行删除持仓或伪造成交。
 * --------------------------------------------------------------------------*/

import { scheduleRender } from './runtime.js';
import { SELF_CHECK_DEFAULT, selfCheckPassed } from './self-check.js';

export const withTrade = (Base) => class extends Base {
  // ── 下单自查弹窗 ──────────────────────────────────────────
  // 只挡开仓。平仓 / 止损 / 加仓 / 撤单一概不弹 —— 想跑的时候被弹窗挡一下是要命的。
  openSelfCheck(dir) {
    if (this.state.selfCheck || this.state.pending || this.state.orderBusy) return;
    // 信号离线时铁律锁单，根本不必开弹窗让人白拨三下（doOrder 里还会再拦一道）。
    if (this.state.signalOffline) { this.toast('盯盘信号离线 · 下单已锁', 'block'); return; }
    this.setState({
      selfCheck: { dir, answers: { ...SELF_CHECK_DEFAULT }, openedAt: Date.now(), flips: 0 },
    });
  }

  toggleSelfCheck(key) {
    const sc = this.state.selfCheck;
    if (!sc || !(key in sc.answers)) return;
    // flips 把反悔也算进去：拨过去又拨回来同样是一次犹豫，那正是事后要看的东西。
    this.setState({
      selfCheck: {
        ...sc,
        answers: { ...sc.answers, [key]: !sc.answers[key] },
        flips: sc.flips + 1,
      },
    });
  }

  closeSelfCheck() {
    if (this.state.selfCheck) this.setState({ selfCheck: null });
  }

  confirmSelfCheck() {
    const sc = this.state.selfCheck;
    if (!sc) return;
    if (!selfCheckPassed(sc.answers)) { this.toast('第 2 条「打架」必须答否才能提交', 'block'); return; }
    this.setState({ selfCheck: null });
    // dwell_ms / flips 比三个答案本身诚实：答案能骗自己，手速骗不了。0.8 秒拨完
    // 三个的那批单和停了 6 秒的那批，事后一比就知道这个弹窗还在起作用、
    // 还是已经退化成肌肉记忆了。
    this.doOrder(sc.dir, {
      ...sc.answers,
      dwell_ms: Math.max(0, Math.round(Date.now() - sc.openedAt)),
      flips: sc.flips,
    });
  }

  // 下单只走 WATCHER 执行链；市场与账户规则由后端 validator 判定。
  //
  // 没带 `selfCheck` 就先开自查弹窗，不直接下单。把这道卡放在函数入口而不是
  // 放在按钮上，是为了让将来新增的调用方也自动落在卡后面，不会有人从旁边绕过去。
  async doOrder(dir, selfCheck) {
    if (this.state.pending || this.state.orderBusy) return;
    if (!selfCheck) { this.openSelfCheck(dir); return; }
    const f = this.TICKERS[this.state.focus];

    // WATCHER 信号离线：铁律锁单（呼应「让错误的单子变难」）
    if (this.state.signalOffline) {
      this.toast('盯盘信号离线 · 下单已锁', 'block');
      return;
    }

    const qtyIn = Math.max(1, Math.floor(Number(this.state.qty) || 1));   // ⑥ 用户数量
    if (this.state.priceType === 'limit' && !String(this.state.limitPrice).trim()) { this.toast('限价单请先填限价', 'block'); return; }

    // WATCHER：真实下单（/api/execute-submit）—— 受理即返回，gate/选约/提交丢后台跑，
    // 前端靠 /api/execute-status 轮询回显最终结果（2026-07-23 簇B #2：原 /api/execute 同步等
    // 全链路最坏 ~2s 转圈，改成秒返回受理号，button 立即可再点）。
    this.setState({ orderBusy: true });
    let resp;
    // 服务端拿不到点击时刻，「点击 → trade_intent_accepted」那段一直是盲区。
    // 这里用**浏览器自己的钟**量 fetch 往返（performance.now 单钟自减，零时钟偏移；
    // 绝不能拿浏览器时间戳去减服务器时间戳，那是偏移与真实延迟叠在一起的假精度），
    // 再搭已有的 /api/execute-status 轮询顺路回传，由后端落一条 client_entry_timing。
    const submitT0 = performance.now();
    let submitRtt = null;
    try {
      const body = this._withPriceType({
        ticker: f.sym, direction: dir, qty: qtyIn, stop_mode: this.state.stopMode,
        // 后端只记录不强制（见 app/routers/orders.py 的 SelfCheck），
        // 落进 ttt_trades.template_snapshot 那个 jsonb 列。
        self_check: selfCheck,
      });
      resp = await (await fetch('/api/execute-submit', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })).json();
      submitRtt = performance.now() - submitT0;
    } catch (e) { this.setState({ orderBusy: false }); this.toast('下单请求失败', 'block'); return; }
    this.setState({ orderBusy: false });
    if (!resp || resp.accepted !== true) {
      this.toast('下单请求失败 · ' + ((resp && resp.detail) || '后端拒绝'), 'block');
      return;
    }
    this._pollExecuteIntent(resp.intent_id, 0, submitRtt);
  }
  // ⑤ 买入受理号轮询（簇B #2）：500ms 一拍，最多 ~15s；resolve 后照旧弹原有的成交/拒绝提示。
  async _pollExecuteIntent(intentId, attempt, submitRtt) {
    attempt = attempt || 0;
    let httpResp, resp;
    // 每拍都带上；后端只在**首次看到终态**那一拍落一条日志（BuyIntent 上有去重标记）。
    let url = '/api/execute-status/' + encodeURIComponent(intentId);
    if (submitRtt != null) {
      url += `?client_submit_ms=${submitRtt.toFixed(1)}&poll_attempt=${attempt}`;
    }
    try {
      httpResp = await fetch(url);
      resp = await httpResp.json();
    } catch (e) { return; }   // 网络抖动：静默丢失一次提示，仓位/订单流仍由 /ws 状态快照兜底
    if (httpResp.status === 404) {
      // BUY_INTENTS 是内存态，服务重启会清空；此时受理号本身已不存在，不是闸门拒绝，
      // 文案要跟"闸门拒绝"区分开，并提示去仓位/订单流核对真实结果（后端可能已提交成功）。
      this.toast('下单受理号已失效（服务重启？）· 请到订单流/持仓核对是否已成交', 'block');
      return;
    }
    if (!resp || resp.status === 'processing') {
      if (attempt < 30) { setTimeout(() => this._pollExecuteIntent(intentId, attempt + 1, submitRtt), 500); return; }
      // 15s 仍未 resolve：后端 execute_run 可能仍在跑，不代表失败——只是前端不再等，
      // 提示用户自行核对，而不是静默停止让人以为下单消失了。
      this.toast('下单受理超时（15s 未回执）· 请到订单流/持仓核对是否已成交', 'block');
      return;
    }
    if (resp.status === 'submitted') {
      const r = resp.result || {};
      // 2026-08-27：后端不再兜底 0.50 去跑 delta 闸，文案跟着改成实话——
      // 拿不到 greeks 时这道闸是**没判**，不是「用 0.50 判过了」。
      if (r.delta_source === 'approx') this.toast('Greeks 暂缺 · delta 闸未生效（选的是最贴平值合约）', 'warn');
      const qty = (r.gate && r.gate.qty) || 1;
      this.toast('实盘已提交 · ' + r.contract + ' ×' + qty + ' @' + r.limit + ' · 等待成交', 'ok');
      return;
    }
    // rejected / error
    const r = resp.result || {};
    const g = r.gate;
    this.toast('闸门拒绝 · ' + (resp.reason || (g && (g.reason || g.rule)) || r.error || '不可下单'), 'block');
  }
  selectPosition(index) {
    const p = this.state.positions[index];
    // 选中的是持仓本身，不是“此刻恰好存在的挂单”。先保留合约目标；
    // 若用户随后从该行提交止盈，applyPositions 会自动绑定新出现的 closeOrderId。
    const cancelTarget = p
      ? { kind: 'position', optionCode: p.optionCode, orderId: p.closeOrderId || null }
      : null;
    this.setState(
      { posFocus: Math.max(0, index), cancelTarget },
      { soft: true, regions: ['positions', 'orders'] },
    );
  }
  selectOrder(order) {
    if (!order || order.status !== 'pending') return;
    this.setState(
      { cancelTarget: { kind: 'order', orderId: order.oid } },
      { soft: true, regions: ['positions', 'orders'] },
    );
  }
  cancelPending() {
    const target = this.state.cancelTarget;
    if (target && target.kind === 'order') {
      const order = (this.state.orders || []).find(
        (o) => o.status === 'pending' && o.oid === target.orderId,
      );
      if (order) { this.cancelOrder(order); return; }
    }
    if (target && target.kind === 'position') {
      const index = this.state.positions.findIndex(
        (p) => p.optionCode === target.optionCode && !!p.closeOrderId,
      );
      if (index >= 0) { this.cancelClose(index); return; }
    }
    // 目标订单刚好终态/刷新后，蓝框持仓仍是用户当前看到的明确选中项。只回退到
    // 这一个持仓自己的在途平仓单，不按订单数组猜测其它订单。
    const focusedIndex = Math.max(
      0,
      Math.min(this.state.positions.length - 1, this.state.posFocus),
    );
    const focused = this.state.positions[focusedIndex];
    if (focused && focused.closeOrderId) {
      this.cancelClose(focusedIndex);
      return;
    }
    this.setState({ cancelTarget: null }, { soft: true, regions: ['positions', 'orders'] });
    this.toast('未选择可撤挂单', 'warn');
  }
  // ⑦ 订单流面板「撤单」：统一走后端并等待券商确认。
  async cancelOrder(o) {
    this._cancelBusy = this._cancelBusy || new Set();
    if (this._cancelBusy.has(o.oid)) return;
    this._cancelBusy.add(o.oid);
    scheduleRender({ soft: true, regions: ['orders', 'positions'] });
    try {
      const response = await fetch('/api/orders/' + encodeURIComponent(o.oid) + '/cancel', { method: 'POST' });
      const res = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(res.detail || ('HTTP ' + response.status));
      if (res.cancelled) this.toast('已撤单 · ' + o.sym, 'ok');
      else if (res.status === 'filled') this.toast('订单已成交，无法撤单 · ' + o.sym, 'warn');
      else if (res.accepted || res.status === 'cancel_pending') this.toast('撤单已受理，等待券商确认 · ' + o.sym, 'warn');
      else throw new Error(res.detail || '券商未确认撤单');
    } catch (e) {
      this.toast('撤单失败 · ' + o.sym + ' · ' + (e.message || '请求异常'), 'block');
    } finally {
      this._cancelBusy.delete(o.oid);
      scheduleRender({ soft: true, regions: ['orders', 'positions'] });
    }
  }

  // 平一笔仓统一走后端原子平仓；前端绝不自行删除持仓或伪造成交。
  _closeOne(p, reasonTag) {
    if (!p.optionCode) {
      this.toast('平仓请求被拒 · ' + p.sym + ' · 缺少真实合约代码', 'block');
      return Promise.resolve({ ok: false, p });
    }
    return fetch('/api/close', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(this._quickCloseBody(p, p.qty, reasonTag || '手动平仓')),
    }).then((r) => r.json()).then((res) => {
      if (res && res.accepted) return { ok: true, p };
      this.toast('平仓请求被拒 · ' + p.sym + ' · ' + ((res && res.error) || ''), 'block');
      return { ok: false, p };
    }).catch(() => { this.toast('平仓请求失败 · ' + p.sym, 'block'); return { ok: false, p }; });
  }
  closePos(i) {
    const p = this.state.positions[i];
    if (!p) return;
    if (p.closing) return;                    // 平仓中：吞掉重复点击
    if (this.state.orderBusy) return;
    this.setState({ orderBusy: true });
    this._closeOne(p, '手动平仓 F').then((res) => {
      this.setState({ orderBusy: false });
      if (!res.ok) return;
      // 受理即返回，卡标「平仓中」，成交/失败由 /ws 持仓状态回显。
      this.setState((s) => ({
        positions: s.positions.map((x) => (x.id === p.id ? { ...x, closing: true } : x)),
      }), { soft: true, regions: ['positions'] });
      this.toast('平仓已提交 · ' + p.sym + ' ' + p.type, 'ok');
    });
  }
  // 撤在途平仓挂单（券商侧被动卖单排队未成交时）：撤单 + 回退持仓 open，可重新平仓。
  async cancelClose(i) {
    const p = this.state.positions[i];
    if (!p || !p.closeOrderId) return;
    const closeOrderId = p.closeOrderId;
    this._cancelBusy = this._cancelBusy || new Set();
    if (this._cancelBusy.has(closeOrderId)) return;
    this._cancelBusy.add(closeOrderId);
    scheduleRender({ soft: true, regions: ['positions', 'orders'] });
    try {
      // 始终按界面明确展示的 Alpaca order_id 撤单。这样即使服务重启后持仓绑定尚未
      // 恢复，也能走通用订单撤单接口；绝不按数组第一单或仅凭 ticker 猜目标。
      const response = await fetch(
        '/api/orders/' + encodeURIComponent(closeOrderId) + '/cancel',
        { method: 'POST' },
      );
      const res = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(res.detail || ('HTTP ' + response.status));
      if (res.cancelled) this.toast('已撤平仓挂单 · ' + p.sym, 'ok');
      else if (res.status === 'filled') this.toast('平仓单已成交，无法撤单 · ' + p.sym, 'warn');
      else if (res.accepted || res.status === 'cancel_pending') this.toast('撤单已受理，等待券商确认 · ' + p.sym, 'warn');
      else throw new Error(res.detail || '券商未确认撤单');
    } catch (e) {
      this.toast('撤单失败 · ' + p.sym + ' · ' + (e.message || '请求异常'), 'block');
    } finally {
      this._cancelBusy.delete(closeOrderId);
      scheduleRender({ soft: true, regions: ['positions', 'orders'] });
    }
  }
  closeAll() {
    if (this.state.orderBusy) return;
    const targets = this.state.positions.slice();
    if (!targets.length) return;
    // 全平没有快捷键（2026-08-21 取消），按钮是唯一入口，所以挡在这里。
    // 必须写明笔数与标的：光问「确定全平吗」，看不出这一下是平 1 笔还是 8 笔。
    const summary = targets.map((p) => p.sym + ' ×' + p.qty).join('、');
    if (!window.confirm('确认全平当前 ' + targets.length + ' 笔持仓？\n\n'
                        + summary + '\n\n提交后无法撤回。')) return;
    this.setState({ orderBusy: true });
    Promise.all(targets.map((p) => this._closeOne(p, '全平'))).then((results) => {
      const ok = results.filter((r) => r.ok);
      const acceptedIds = new Set(ok.map((r) => r.p.id));
      this.setState((s) => {
        const positions = s.positions.map((x) => (acceptedIds.has(x.id) ? { ...x, closing: true } : x));
        return { positions,
                 posFocus: Math.max(0, Math.min(positions.length - 1, s.posFocus)), orderBusy: false };
      });
      const failed = results.length - ok.length;
      if (failed) this.toast(`全平：${failed} 笔请求被拒`, 'block'); else this.toast('全平已提交', 'ok');
    });
  }

  // 持仓行主卖出：卖价模式为 target 时提交 persistent 目标限价；否则按所选卖价卖指定张数。
  sellPosition(i) {
    if (this.state.orderBusy) return;
    const s = this.state, p = s.positions[i];
    if (!p) return;
    if (p.closing) return;            // 平仓中：吞掉重复点击
    const n = Math.min(p.qty, Math.max(1, Math.floor(Number(p.sellQty) || 1)));
    if (!p.optionCode) { this.toast('卖出请求被拒 · 缺少真实合约代码', 'block'); return; }
    const targetPct = Math.max(0, Number(p.targetPct) || 0);
    const targetMode = p.exitPriceType === 'target';
    const body = targetMode
      ? this._targetCloseBody(p, n, targetPct)
      : this._selectedCloseBody(p, n, n < p.qty ? '指定数量卖出' : '指定数量全平');
    this._optimisticCloseContracts = this._optimisticCloseContracts || new Set();
    this._optimisticCloseContracts.add(p.optionCode);
    // 点击持仓内的卖出按钮也代表明确选择该持仓；止盈单回到快照后会自动成为 ESC 目标。
    this.setState({
      orderBusy: true,
      posFocus: i,
      cancelTarget: {
        kind: 'position',
        optionCode: p.optionCode,
        orderId: p.closeOrderId || null,
      },
    }, { soft: true, regions: ['positions', 'orders'] });
    fetch('/api/close', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
      .then((r) => r.json()).then((res) => {
        this.setState({ orderBusy: false });
        if (res && res.accepted) {
          this.setState((st) => ({
            positions: st.positions.map((x) => (x.id === p.id ? { ...x, closing: true } : x)),
          }), { soft: true, regions: ['positions'] });
          this.toast(targetMode
            ? `目标限价已提交 · +${targetPct}% · ${n}张（持续挂单）`
            : `${this._exitPriceLabel(p.exitPriceType)}卖出已提交 · ${n}张`, 'ok');
        } else {
          this._optimisticCloseContracts.delete(p.optionCode);
          this.setState((st) => ({
            positions: st.positions.map(
              (x) => (x.id === p.id ? { ...x, closing: false } : x),
            ),
          }), { soft: true, regions: ['positions'] });
          this.toast('卖出请求被拒 · ' + ((res && res.error) || ''), 'block');
        }
      }).catch(() => {
        this._optimisticCloseContracts.delete(p.optionCode);
        this.setState((st) => ({
          orderBusy: false,
          positions: st.positions.map(
            (x) => (x.id === p.id ? { ...x, closing: false } : x),
          ),
        }), { soft: true, regions: ['positions'] });
        this.toast('卖出请求失败', 'block');
      });
  }

  // ⑥ 买入数量控件
  setQty(n) { const q = Math.max(1, Math.floor(Number(n) || 1)); this.setState({ qty: q }, { soft: true, regions: ['order-entry'] }); }
  onQtyInput(e) { const v = e.target.value; if (v === '') { this.setState({ qty: '' }, { soft: true, regions: ['order-entry'] }); return; } const q = Math.max(1, Math.floor(Number(v) || 1)); this.setState({ qty: q }, { soft: true, regions: ['order-entry'] }); }
  // 开仓风控止损模式：只选模式，不在下单面板修改纪律模板里的阈值。
  setStopMode(k) {
    if (k === 'percent' || k === 'amount') this.setState({ stopMode: k }, { soft: true, regions: ['order-entry'] });
  }
  // ⑩ 价格类型
  setPriceType(k) { this.setState({ priceType: k }, { soft: true, regions: ['order-entry'] }); }
  // ⑩ 把当前价格类型并入请求体。'auto'（默认）→ **不发** price_type，由后端按动作取可成交
  // 那一侧（开仓/加仓=卖一 ask，平仓/减仓=买一 bid）。显式选了就原样发，尊重用户意图。
  // 所有下单/平仓/加仓请求体必须经此构造，禁止各处自己拼 price_type——买入语义的默认值
  // 漏进平仓接口就是 2026-07-25 QQQ 减仓永不成交的根因。
  _withPriceType(body) {
    const s = this.state;
    if (!s.priceType || s.priceType === 'auto') return body;
    body.price_type = s.priceType;
    if (s.priceType === 'limit') body.limit_price = String(s.limitPrice || '').trim();
    return body;
  }
  onLimitInput(e) { this.setState({ limitPrice: e.target.value }, { soft: true, regions: ['order-entry'] }); }

  _quickCloseBody(p, qty, reason) {
    return {
      option_code: p.optionCode,
      reason,
      qty,
      price_type: 'bid',
      timeout_policy: 'quick',
    };
  }

  _selectedCloseBody(p, qty, reason) {
    const body = {
      option_code: p.optionCode,
      reason,
      qty,
      timeout_policy: 'quick',
    };
    const priceType = p.exitPriceType || 'bid';
    if (priceType === 'cost') {
      body.price_type = 'limit';
      body.limit_price = Math.max(0.01, Number(p.entryMid) || 0.01).toFixed(2);
    } else {
      body.price_type = priceType;
    }
    return body;
  }

  _exitPriceLabel(priceType) {
    return ({
      target: '限价 / 目标',
      bid: '快速 / 买一',
      cost: '成本价',
      mid: '中间价',
      ask: '卖一',
      market: '市价',
    })[priceType] || '限价 / 目标';
  }

  _parseTargetPct(raw) {
    let text = String(raw == null ? '' : raw).trim().replace(/％/g, '%');
    if (!text) return null;
    const multiplier = text.startsWith('*') || text.startsWith('×');
    if (multiplier) text = text.slice(1).trim();
    if (text.endsWith('%')) text = text.slice(0, -1).trim();
    const value = Number(text);
    if (!Number.isFinite(value)) return null;
    const pct = multiplier ? value * 100 : value;
    return Math.max(0, Math.min(1000, pct));
  }

  _formatTargetPct(pct) {
    return String(Number(Math.max(0, Number(pct) || 0).toFixed(2)));
  }

  _targetLimitPrice(p, targetPct) {
    return Math.max(0.01, Math.round(p.entryMid * (1 + targetPct / 100) * 100) / 100);
  }

  _updateTargetInput(i, raw, input, normalize) {
    const positions = [...this.state.positions];
    const current = positions[i];
    if (!current) return;
    const parsed = this._parseTargetPct(raw);
    if (parsed == null) {
      positions[i] = { ...current, targetInput: String(raw) };
      this.state.positions = positions;
      if (normalize) {
        const fallback = this._formatTargetPct(current.targetPct);
        positions[i].targetInput = fallback;
        input.value = fallback;
      }
      return;
    }
    const formatted = this._formatTargetPct(parsed);
    positions[i] = {
      ...current,
      exitPriceType: 'target',
      targetPct: parsed,
      targetInput: normalize ? formatted : String(raw),
    };
    this.state.positions = positions;
    if (normalize) input.value = formatted;
    const actions = input.closest('.position-actions');
    const priceText = actions && actions.querySelector('[data-role="sell-price"]');
    if (priceText) priceText.textContent = '$' + this._targetLimitPrice(positions[i], parsed).toFixed(2);
  }

  _targetCloseBody(p, qty, targetPct) {
    const limit = this._targetLimitPrice(p, targetPct);
    return {
      option_code: p.optionCode,
      reason: `目标限价 +${targetPct}%`,
      qty,
      price_type: 'limit',
      limit_price: limit.toFixed(2),
      timeout_policy: 'persistent',
    };
  }

  // ⑪ 按比例减仓（1/3、减半共用）：round(qty×fraction) 至少1手、不超持仓，末段清空
  reducePos(i, fraction, label) {
    const s = this.state, p = s.positions[i];
    if (!p) return;
    if (p.closing) return;            // 平仓中：吞掉重复点击
    const n = Math.max(1, Math.min(p.qty, Math.round(p.qty * fraction)));
    if (!p.optionCode) { this.toast('减仓请求被拒 · 缺少真实合约代码', 'block'); return; }
    const body = this._quickCloseBody(p, n, label + ' 减仓');
    fetch('/api/close', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
      .then((r) => r.json()).then((res) => {
        if (res && res.accepted) {
          // 受理即返回，卡标「平仓中」；成交后 /ws 反映新 qty 并带出卖单。
          this.setState((st) => ({
            positions: st.positions.map((x) => (x.id === p.id ? { ...x, closing: true } : x)),
          }), { soft: true });
          this.toast(label + ' 减仓已提交 ' + n + '张 · ' + p.optionCode, 'ok');
        } else this.toast('减仓请求被拒 · ' + ((res && res.error) || ''), 'block');
      }).catch(() => this.toast('减仓请求失败', 'block'));
  }
  thirdPos(i) { this.reducePos(i, 1 / 3, '1/3'); }
  halvePos(i) { this.reducePos(i, 0.5, '减半'); }

  addPos(i, qty) {
    if (qty <= 0 || this.state.orderBusy) return;
    const s = this.state, p = s.positions[i];
    if (!p) return;
    if (p.closing) return;
    if (!p.optionCode) { this.toast('加仓失败 · 缺少真实合约代码', 'block'); return; }
    this.setState({ orderBusy: true });
    const body = this._withPriceType({ option_code: p.optionCode, qty: qty });
    fetch('/api/add', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }).then(r => r.json()).then(res => {
      if (res && res.executed) {
         this.toast(`加仓提交 · ${p.optionCode} ×${qty}`, 'ok');
         this.setState((st) => { const pos = [...st.positions]; if(pos[i]) pos[i].addQty = 1; return { positions: pos, orderBusy: false }; });
      } else {
         this.setState({ orderBusy: false });
         this.toast(`加仓失败 · ${(res && (res.error || res.submit_error || (res.gate && res.gate.reason) || res.detail)) || ''}`, 'block');
      }
    }).catch(() => { this.setState({ orderBusy: false }); this.toast('加仓请求失败', 'block'); });
  }
};
