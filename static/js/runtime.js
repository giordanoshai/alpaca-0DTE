/* ----------------------------------------------------------------------------
 * 装配：渲染调度 / WS 接缝 / 命令式叠层 / 挂载
 *
 * 本文件**不 import 任何 app-*.js** —— App 类由入口 static/app.js 组装完再经
 * boot(AppClass) 传进来。这样依赖是单向的（app-* → runtime → shell），不成环。
 * --------------------------------------------------------------------------*/

import { app, tplEl, stageEl, bindShell } from './shell.js';
import { build, isInteractiveFormControl } from './template.js';
import { formatVwapDistanceForUi, pickBlockReason } from './format.js';
import { initSfx } from './sfx.js';

let rafId = null, pendingHard = false;
const pendingRegions = new Set();
let scrollToSymPending = null;   // setFocus 请求的"滚到该 ticker"，由 renderApp() 统一处理（见 #6 注释）
const persistentOverlays = [];  // 挂在交易页 topbar 内的手动叠层；stageEl 整体重建后需重新插回

// setFocus() 在别的模块里，改不了本文件的 scrollToSymPending（import 进去的是
// 只读活绑定），所以留一个写入口给它。拆分前是直接赋值，语义不变。
function requestScrollToSym(sym) {
  scrollToSymPending = sym;
}

// 指针按住期间冻结整树重建：replaceChildren 若落在 mousedown 与 mouseup 之间，
// 按下的元素已被换掉，浏览器不派发 click（实感：按钮第一下没反应，第二下才有用——
// 1s 时钟 + 2s/3s 轮询让重建随时可能撞上按压窗口）。抬起后经 rAF 补渲染，
// rAF 在该帧输入事件（含 click）派发之后执行，不影响本次点击。
let pointerHeld = false, renderHeld = false, formRenderHeld = false;
let replaceTargetInput = false;

function scheduleRender(opts) {
  const soft = !!(opts && opts.soft);
  const regions = opts && Array.isArray(opts.regions) ? opts.regions : [];
  if (!soft) pendingHard = true;
  regions.forEach((region) => pendingRegions.add(region));
  // 未声明 dirty 分区的旧调用保留硬更新语义；页面切换/模板结构变化仍安全全刷。
  if (soft && regions.length === 0 && !pendingHard && pendingRegions.size === 0) {
    pendingHard = true;
  }
  if (rafId) return;
  rafId = requestAnimationFrame(() => {
    rafId = null;
    if (pointerHeld) { renderHeld = true; return; }   // 按住中：挂起，pointerup 后补
    const ae = document.activeElement;
    const interacting = isInteractiveFormControl(ae);
    const scheduledRegions = pendingHard || pendingRegions.size === 0
      ? null
      : new Set(pendingRegions);
    // 目标分区在当前页面里根本不存在（如停在设置页时收到交易页的 tickers/
    // order-entry 软刷新）：renderApp 会因为找不到分区节点而退化成整页重建，
    // 一定会把聚焦中的输入框换成新 DOM 节点——必须当作"会影响到它"来处理，
    // 否则输入法正在拼字的组合态会被每次后台刷新打断（2026-08-17 设置页中文
    // 打不进去字，根因就在这一行漏判）。
    const formAffected = !scheduledRegions || Array.from(scheduledRegions).some((region) => {
      const root = stageEl && stageEl.querySelector(`[data-render-region="${region}"]`);
      return !root || root.contains(ae);
    });
    if (interacting && !pendingHard && formAffected) {
      formRenderHeld = true;
      return;   // 表单控件聚焦时挂起软重建，避免打断输入或关闭下拉框
    }
    const renderRegions = scheduledRegions;
    pendingHard = false;
    pendingRegions.clear();
    formRenderHeld = false;
    renderApp(renderRegions);
  });
}

window.addEventListener('pointerdown', (e) => {
  pointerHeld = true;
  if (e.target && e.target.matches && e.target.matches('[data-replace-whole-on-type]')) {
    replaceTargetInput = true;
  }
}, true);
window.addEventListener('focusin', (e) => {
  if (e.target && e.target.matches && e.target.matches('[data-replace-whole-on-type]')) {
    replaceTargetInput = true;
    e.target.select();
  }
}, true);
window.addEventListener('click', (e) => {
  if (e.target && e.target.matches && e.target.matches('[data-replace-whole-on-type]')) {
    replaceTargetInput = true;
  }
}, true);
window.addEventListener('beforeinput', (e) => {
  if (
    replaceTargetInput
    && e.target && e.target.matches
    && e.target.matches('[data-replace-whole-on-type]')
  ) {
    e.target.select();
    replaceTargetInput = false;
  }
}, true);
const _releasePointer = () => {
  if (!pointerHeld) return;
  pointerHeld = false;
  if (renderHeld) { renderHeld = false; scheduleRender({ soft: true }); }  // 硬渲需求已记录在 pendingHard
};
window.addEventListener('pointerup', _releasePointer, true);
window.addEventListener('pointercancel', _releasePointer, true);
window.addEventListener('focusout', () => {
  // 输入框结束操作后补上期间挂起的最后一帧，保证刷新只是延后而不会丢失。
  requestAnimationFrame(() => {
    if (formRenderHeld && !isInteractiveFormControl(document.activeElement)) {
      formRenderHeld = false;
      scheduleRender({ soft: true });
    }
  });
}, true);

function renderApp(regions) {
  const rStart = performance.now();
  const requested = regions && regions.size > 0 ? Array.from(regions) : [];
  let canPatchRegions = requested.length > 0 && Boolean(stageEl.firstElementChild);
  const regionTargets = [];
  if (canPatchRegions) {
    for (const region of requested) {
      const selector = `[data-render-region="${region}"]`;
      const current = stageEl.querySelector(selector);
      const source = tplEl.content.querySelector(selector);
      if (!current || !source) {
        canPatchRegions = false;
        break;
      }
      regionTargets.push({ region, current, source });
    }
  }

  // 分区更新只扫描被替换区域的滚动节点和焦点；全树更新才扫描整个 stage。
  const renderRoots = canPatchRegions
    ? regionTargets.map((target) => target.current)
    : [stageEl];
  const scrollStates = new Map();
  renderRoots.forEach((root) => {
    if (!root) return;
    if (root.id && (root.scrollLeft > 0 || root.scrollTop > 0)) {
      scrollStates.set(root.id, { left: root.scrollLeft, top: root.scrollTop });
    }
    root.querySelectorAll('[id]').forEach(el => {
      if (el.scrollLeft > 0 || el.scrollTop > 0) {
        scrollStates.set(el.id, { left: el.scrollLeft, top: el.scrollTop });
      }
    });
  });
  // 焦点保持：整树重建会把聚焦中的输入框换成新节点（实感：光标"过一会儿跳走"——
  // 典型触发是切换输入框时上一个框的 change 事件引发重建）。凡带 data-fkey 的
  // 输入控件，重建后按 key 找回新节点恢复焦点与光标位置。
  let focusKey = null, selStart = null, selEnd = null;
  const ae = document.activeElement;
  const focusAffected = ae && renderRoots.some((root) => root && root.contains(ae));
  if (focusAffected && ae.getAttribute && ae.getAttribute('data-fkey')) {
    focusKey = ae.getAttribute('data-fkey');
    try { selStart = ae.selectionStart; selEnd = ae.selectionEnd; } catch (e) {}
  }

  let renderMode = '全树';
  if (canPatchRegions) {
    const bindings = app.renderVals(regions);
    const pairs = [];
    let structureChanged = false;
    regionTargets.forEach(({ region, current, source }) => {
      const built = build(source, bindings);
      const next = built.find((node) => (
        node.nodeType === Node.ELEMENT_NODE
        && node.getAttribute('data-render-region') === region
      ));
      if (!next) structureChanged = true;
      else pairs.push([current, next]);
    });
    if (!structureChanged) {
      pairs.forEach(([current, next]) => current.replaceWith(next));
      renderMode = `分区:${requested.join(',')}`;
    } else {
      canPatchRegions = false;
    }
  }
  if (!canPatchRegions) {
      const bindings = app.renderVals(null);
      const frag = document.createDocumentFragment();
      for (const child of tplEl.content.childNodes) {
        for (const n of build(child, bindings)) frag.appendChild(n);
      }
      stageEl.replaceChildren(frag);
      persistentOverlays.forEach((fn) => fn());
  }

  scrollStates.forEach((pos, id) => {
    const el = stageEl.querySelector('#' + id);
    if (el) {
      el.scrollLeft = pos.left;
      el.scrollTop = pos.top;
    }
  });
  // #6：待处理的"滚到该 ticker"请求，在恢复完旧滚动位置之后再执行，覆盖掉上面对
  // #scroll-tickers 的 scrollLeft 复位——同一轮重建内完成，不给后续任何一次重建留下
  // 打断/冻结这次滚动的窗口。
  if (scrollToSymPending) {
    const sym = scrollToSymPending;
    scrollToSymPending = null;
    const el = stageEl.querySelector('[data-tsym="' + sym + '"]');
    if (el && el.scrollIntoView) el.scrollIntoView({ behavior: 'auto', inline: 'center', block: 'nearest' });
  }
  if (focusKey) {
    const el = stageEl.querySelector('[data-fkey="' + focusKey + '"]');
    if (el) {
      el.focus();
      if (selStart != null) { try { el.setSelectionRange(selStart, selEnd); } catch (e) {} }
    }
  }
}

// WS 单一状态通道：行情 + 订单 + 持仓 + 风控同帧下发，前端纯消费。
let _wsFirstFrameLogged = false;
function connectWS() {
  const url = (location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws';
  let ws;
  let pingTimer = null, pingSeq = 0, pendingPingId = null, pendingPingAt = 0;
  let lastFrameSeq = 0;
  const sendPing = () => {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    pendingPingId = ++pingSeq;
    pendingPingAt = performance.now();
    ws.send(JSON.stringify({ type: 'ping', id: pendingPingId }));
  };
  console.log(`[TT-PERF-WS] [${(performance.now()).toFixed(1)}ms] 正在连接 WebSocket: ${url}`);
  try { ws = new WebSocket(url); } catch (e) { console.error('[TT-PERF-WS] WS 创建失败:', e); app.setState({ wsDown: true }); return; }
  ws.onopen = () => {
    console.log(`[TT-PERF-WS] [${(performance.now()).toFixed(1)}ms] WebSocket 连接建立成功 (onopen)`);
    app.setState({ wsDown: false }, { soft: true });
    sendPing();
    pingTimer = setInterval(sendPing, 5000);
  };
  ws.onmessage = (ev) => {
    const recvTs = performance.now();
    let msg; try { msg = JSON.parse(ev.data); } catch (e) { return; }
    if (msg.type === 'pong') {
      if (msg.id === pendingPingId && pendingPingAt > 0) {
        app.state.hbMs = Math.max(1, Math.round(recvTs - pendingPingAt));
        pendingPingId = null;
        pendingPingAt = 0;
        app.refreshHeartbeat();
      }
      return;
    }
    const schemaVersion = Number(msg.schema_version || 1);
    const frameSeq = Number(msg.seq || 0);
    if (schemaVersion >= 2) {
      if (msg.type === 'snapshot') {
        app._sectionRevisions = Object.create(null);
      } else if (
        msg.type === 'patch'
        && lastFrameSeq > 0
        && frameSeq !== lastFrameSeq + 1
      ) {
        console.warn(`[TT-PERF-WS] 帧序号断档 ${lastFrameSeq} → ${frameSeq}，重连取完整基线`);
        try { ws.close(4000, 'sequence gap'); } catch (e) {}
        return;
      }
      lastFrameSeq = frameSeq;
    }
    const revisions = msg.revisions || {};
    const trading = msg.trading || {};
    const hasOwn = (obj, key) => Object.prototype.hasOwnProperty.call(obj, key);
    if (!_wsFirstFrameLogged) {
      _wsFirstFrameLogged = true;
      console.log(`[TT-PERF-WS] [${recvTs.toFixed(1)}ms] 收到 WebSocket 💥第一帧数据:`, msg);
    }

    if (hasOwn(trading, 'broker_sync')) {
      app.applyBrokerSync(trading.broker_sync, revisions.broker_sync);
    }
    if (hasOwn(trading, 'risk')) app.applyRisk(trading.risk, revisions.risk);
    if (hasOwn(trading, 'orders')) app.applyOrders(trading.orders, revisions.orders);
    if (hasOwn(trading, 'positions')) app.applyPositions(trading.positions, revisions.positions);
    if (hasOwn(msg, 'options')) app.applyOptionPreviews(msg.options, revisions.options);

    const hasFeed = hasOwn(msg, 'ticks')
      || hasOwn(msg, 'pool')
      || hasOwn(msg, 'last_signal')
      || hasOwn(msg, 'signal_online');
    // walls 与 feed 是后端两条独立的 section，可以单独变。下面唯一排渲染的 setState
    // 在 feed 分支里，`!hasFeed` 那条 return 上面只有 refreshHeartbeat()（只改心跳
    // 文本节点、不排渲染）。所以只带 walls 的 patch 得自己补一次 —— 否则盘中靠 feed
    // 帧密集蹭上去，盘前与收盘后 feed 一静默，墙位就冻在最后一次 feed 帧上。
    if (hasOwn(msg, 'walls')) {
      app.state.walls = msg.walls;
      // 只带 walls 的帧也要跑一次破墙判定：跟上面 scheduleRender 同一个道理，
      // feed 一静默就再也蹭不上下面那次调用。带 feed 的帧交给下面统一跑，
      // 不在这里重复（重复也不会多响 —— 第二次跑时基准已经更新过了）。
      if (!hasFeed) {
        scheduleRender({ soft: true, regions: ['tickers'] });
        app.checkGexBreach();
      }
    }

    // 资金潮徽章走定点更新、**不排渲染**（见 refreshTide 的注释）。位置必须留在
    // 下面 `!hasFeed` 那条 return 之上 —— 它每分钟才变一次，盘前/收盘后 feed 一
    // 静默就再也蹭不到渲染，放在 return 下面等于整晚不更新。
    if (hasOwn(msg, 'tide')) {
      app.state.tide = msg.tide;
      app.refreshTide();
    }

    // 市场时段徽章同理走定点更新，且**必须留在下面那条 return 之上**：收盘后
    // feed 内容不再变化、revision 不涨，放在 return 下面就永远收不到「休市」——
    // 而那正是它最该显示的时候（与上面 tide 徽章同一个坑）。
    if (hasOwn(msg, 'market_session')) {
      app.state.marketSession = msg.market_session;
      app.refreshSession();
    }

    if (!hasFeed || !app.acceptSectionRevision('feed', revisions.feed)) {
      app.state.wsDown = false;
      app.refreshHeartbeat();
      return;
    }

    let updatedTicks = 0;
    if (Array.isArray(msg.ticks)) {
      for (const tk of msg.ticks) {
        const t = app.TICKERS.find((x) => x.sym === tk.sym);
        if (!t) continue;
        // 2026-09-01：`trend` / `er` 两个键随震荡闸删除，卡片上那一格已撤掉。
        if (tk.vwap != null) t.vwap = formatVwapDistanceForUi(tk.vwap);
        if (tk.price != null) t.price = tk.price;
        // 后端干跑闸门的真实结果（审查 2.5/2.6）：午后禁入、次数用尽、熔断…
        // 全在这儿。此前前端只能从 pool.tradable 推出"方向不明确"一句话。
        // {long, short} 两份是前端契约；2026-09-01 起两侧结论恒等（闸门全部与方向
        // 无关），但结构保留 —— 方向相关性将来若回来，这里不必再改。
        t.gates = tk.gates || null;
        updatedTicks++;
      }
    }
    
    let updatedPool = 0;
    // watcher 档：pool 现在只带数据质量，驱动红绿灯（st）。
    // 2026-09-01：`trend_label` / `trend_direction` / `er` 三个键随震荡闸、方向闸
    // 一并从后端删除，卡片上的方向标签与效率比那一格同时撤掉。
    if (Array.isArray(msg.pool)) {
      for (const p of msg.pool) {
        const t = app.TICKERS.find((x) => x.sym === p.sym);
        if (!t) continue;
        updatedPool++;
        // 红绿灯完全由后端双向干跑闸门决定 —— 那是唯一真会拦单的那份代码。
        // 前端不得二次推断。
        const pquality = p.data_quality_ok !== undefined ? p.data_quality_ok : p.tradable;
        const gl = t.gates && t.gates.long, gs = t.gates && t.gates.short;
        const okL = pquality && !!gl && gl.blocked === false;
        const okS = pquality && !!gs && gs.blocked === false;
        // 2026-09-01 方向闸删除后 **'both' 又变回常态**（08-21 到 09-01 之间它不可达，
        // 因为规则 6 必定拦掉一侧）。四个分支一个都不删：谁能买只认后端。
        t.st = (okL && okS) ? 'both' : okL ? 'long' : okS ? 'short' : 'block';
        t.reason = t.st === 'block'
          ? (pickBlockReason(gl, gs) || '盯盘：暂不可交易')
          : null;
      }
    }

    // 破墙提醒：现价与墙位都已是最新，在这里跑一次。纯读，不排渲染。
    app.checkGexBreach();

    // 卡片顺序**固定不排**：顺序只由 /api/tickers 的观察列表决定。
    //
    // 2026-08-21 删掉原来的四层排序（最新信号置顶 / 可交易在前 / 趋势分绝对值降序 /
    // 池子原序）。数字键 1~9、0 是按卡片**位置**绑的，位置一动，按 3 买到的就不是
    // 刚才看的那只票。趋势分那层每分钟变一次，「可交易在前」在规则 6 上线后更凶——
    // 1 分与 5 分不同向就 block，一只票一分钟能进出好几次。
    //
    // 「最新信号置顶」不必保留：信号来时下面那段已经把焦点跳到该票并滚到可视区，
    // 卡片不需要再挪到第一位，两者重复。
    //
    // 后端 `feed_snapshot._order_pool` 仍会按有无信号重排 `msg.pool`，但前端一律
    // 按符号找票（上面的 `TICKERS.find(x => x.sym === tk.sym)`），那个顺序对界面
    // 已无影响。守卫见 tests/test_frontend_no_card_sorting.py。

    // #5：watcher 信号一来，立即把该 ticker 带到聚焦位（居中滚动，见 setFocus/_scrollToSym）。
    // 首帧只记基线不跳转——避免连接建立时被"连接前就存在"的旧信号抢焦点。
    if (msg.last_signal && msg.last_signal.sym) {
      if (app._lastSignalTs == null) {
        app._lastSignalTs = msg.last_signal.ts;
      } else if (msg.last_signal.ts > app._lastSignalTs) {
        app._lastSignalTs = msg.last_signal.ts;
        const idx = app.TICKERS.findIndex((t) => t.sym === msg.last_signal.sym);
        if (idx !== -1) app.setFocus(idx);
      }
    }

    const patch = { wsDown: false };
    if (msg.signal_online != null) patch.signalOffline = (msg.signal_online === false);
    app.setState(patch, { soft: true, regions: ['tickers', 'order-entry'] });
    app.refreshHeartbeat();
  };
  ws.onclose = () => {
    if (pingTimer) clearInterval(pingTimer);
    console.warn(`[TT-PERF-WS] [${(performance.now()).toFixed(1)}ms] WebSocket 断开连接 (onclose)`);
    app.setState({ wsDown: true, hbMs: null });
    setTimeout(connectWS, 1500);
  };
  ws.onerror = (err) => { console.error(`[TT-PERF-WS] [${(performance.now()).toFixed(1)}ms] WebSocket 出错 (onerror):`, err); try { ws.close(); } catch (e) {} };
}

// ① TICKER 搜索小窗（命令式 overlay，不入模板——避免逐字输入触发模板重建而丢焦点）
let searchCtl = null;
function mountSearch() {
  const wrap = document.createElement('div');
  wrap.id = 'ticker-search';
  wrap.style.cssText = 'position:fixed;inset:0;z-index:10001;display:none;align-items:flex-start;justify-content:center;background:rgba(5,10,20,.55);backdrop-filter:blur(2px)';
  const box = document.createElement('div');
  box.style.cssText = 'margin-top:14vh;width:520px;max-width:92vw;background:#131d2e;border:1px solid #2a3852;border-radius:12px;box-shadow:0 24px 70px rgba(0,0,0,.6);overflow:hidden';
  const inp = document.createElement('input');
  inp.type = 'text';
  inp.placeholder = '输入 TICKER…（↑↓ 选择 · Enter 定位 · Esc 关）';
  inp.style.cssText = 'width:100%;height:52px;padding:0 18px;border:none;border-bottom:1px solid #22314a;background:transparent;color:#e6ecf3;font:600 16px "JetBrains Mono",monospace;outline:none;letter-spacing:.04em';
  const list = document.createElement('div');
  list.style.cssText = 'max-height:46vh;overflow-y:auto;padding:6px';
  box.appendChild(inp); box.appendChild(list); wrap.appendChild(box);
  document.body.appendChild(wrap);
  wrap.addEventListener('mousedown', (e) => { if (e.target === wrap) close(); });

  let items = [], sel = 0;
  const filtered = () => {
    const q = inp.value.trim().toUpperCase();
    return app.TICKERS.map((t, i) => ({ sym: t.sym, st: t.st, i })).filter((t) => !q || t.sym.toUpperCase().includes(q));
  };
  const paint = () => { Array.from(list.children).forEach((el, j) => { el.style.background = (Number(el.getAttribute('data-j')) === sel) ? 'rgba(99,102,241,.16)' : 'transparent'; }); };
  const ensureVisible = () => { const el = list.querySelector('[data-j="' + sel + '"]'); if (el && el.scrollIntoView) el.scrollIntoView({ block: 'nearest' }); };
  const pick = () => { const t = items[sel]; if (!t) return; app.setFocus(t.i); close(); };
  function render() {
    items = filtered();
    if (sel >= items.length) sel = Math.max(0, items.length - 1);
    if (!items.length) { list.innerHTML = '<div style="padding:16px;color:#5a6a82;font:500 13px sans-serif">无匹配标的</div>'; return; }
    list.innerHTML = items.map((t, j) => {
      const lc = t.st === 'block' ? '#8d99aa' : '#9bc7e8';
      return '<div data-j="' + j + '" style="display:flex;align-items:center;gap:11px;padding:9px 12px;border-radius:8px;cursor:pointer;background:' + (j === sel ? 'rgba(99,102,241,.16)' : 'transparent') + '">'
        + '<span style="width:9px;height:9px;border-radius:50%;background:' + lc + ';flex:0 0 auto"></span>'
        + '<span style="font:700 15px monospace;color:#e6ecf3;min-width:66px">' + t.sym + '</span>'
        + '<span style="font:500 12px sans-serif;color:#8695ab">' + (t.st === 'block' ? '禁止' : '可买入') + '</span></div>';
    }).join('');
    Array.from(list.children).forEach((el) => {
      const j = Number(el.getAttribute('data-j'));
      el.addEventListener('mouseenter', () => { sel = j; paint(); });
      el.addEventListener('mousedown', (e) => { e.preventDefault(); sel = j; pick(); });
    });
  }
  function open(seed) { inp.value = seed || ''; sel = 0; wrap.style.display = 'flex'; app.setState({ searchOpen: true }, { soft: true }); render(); setTimeout(() => { inp.focus(); const L = inp.value.length; try { inp.setSelectionRange(L, L); } catch (e) {} }, 0); }
  function close() { wrap.style.display = 'none'; inp.blur(); app.setState({ searchOpen: false }, { soft: true }); }
  inp.addEventListener('input', () => { sel = 0; render(); });
  inp.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowDown') { e.preventDefault(); if (items.length) { sel = Math.min(items.length - 1, sel + 1); paint(); ensureVisible(); } }
    else if (e.key === 'ArrowUp') { e.preventDefault(); if (items.length) { sel = Math.max(0, sel - 1); paint(); ensureVisible(); } }
    else if (e.key === 'Enter') { e.preventDefault(); pick(); }
    else if (e.key === 'Escape') { e.preventDefault(); close(); }
  });
  searchCtl = { open, close };
}

// 信号离线红色横幅（固定叠层，不属设计画布）：状态变化时直接同步，不再常驻轮询。
let signalBannerEl = null;
function syncSignalBanner() {
  if (!signalBannerEl) return;
  const offline = app && app.state && app.state.signalOffline === true;
  signalBannerEl.style.display = offline ? 'block' : 'none';
}

function mountSignalBanner() {
  const el = document.createElement('div');
  el.id = 'signal-banner';
  el.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:10000;display:none;'
    + 'text-align:center;font:700 14px "Inter",sans-serif;letter-spacing:.5px;'
    + 'padding:9px 0;background:#7a1f1a;color:#ffe3df;box-shadow:0 2px 14px rgba(0,0,0,.5);'
    + 'border-bottom:1px solid #e0776c';
  el.textContent = '⚠ 盯盘信号离线 — 下单已锁';
  document.body.appendChild(el);
  signalBannerEl = el;
  syncSignalBanner();
}

// 挂载。App 类由入口传进来，理由见文件头。
export function boot(AppClass) {
  bindShell(new AppClass(), document.getElementById('app-tpl'), document.getElementById('stage'));
  window.__app = app; // 调试用
  app.mount();
  renderApp();
  connectWS();
  mountSignalBanner();
  mountSearch();
  initSfx();
}

export {
  scheduleRender,
  renderApp,
  connectWS,
  mountSearch,
  mountSignalBanner,
  syncSignalBanner,
  searchCtl,
  requestScrollToSym,
};
