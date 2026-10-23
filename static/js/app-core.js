/* ----------------------------------------------------------------------------
 * App 基类：状态、挂载、定点刷新、闸门读数、键盘
 *
 * 整套 App 由本类打底，另外四个 mixin（app-trade / app-sync / app-settings /
 * render）逐层 extends 上去，最后在入口 static/app.js 组装成 class App。
 * 拆成 mixin 而不是 Object.assign(prototype)：class 体里的方法写法与缩进原样保留，
 * `this` 与 super 语义也和拆分前一致。
 * --------------------------------------------------------------------------*/

import { stageEl } from './shell.js';
import { scheduleRender, syncSignalBanner, requestScrollToSym } from './runtime.js';
import { isInteractiveFormControl } from './template.js';
import { BUY_SIDES, SELF_CHECK_ITEMS } from './self-check.js';
import { loadGexAlertEnabled } from './gex-alert.js';
import { HOTKEY_LABELS, loadHotkeys } from './hotkeys.js';
import { playSfx, playUiScroll } from './sfx.js';

export class AppCore {
  constructor() {
    const T0 = Date.now();
    this.TICKERS = [
      { sym: 'QQQ', hot: '1', st: 'long', vwap: '+0.4', reason: null, cLong: { name: 'QQQ C612', delta: '0.43', spread: '2.1%', bid: '1.98', mid: '2.04', ask: '2.10', qty: 2, amt: '$436' }, cShort: { name: 'QQQ P612', delta: '0.43', spread: '2.1%', bid: '1.98', mid: '2.04', ask: '2.10', qty: 2, amt: '$436' } },
      { sym: 'IWM', hot: '2', st: 'block', vwap: '+2.6', reason: '距VWAP +2.6×σ 追高拦截', cLong: { name: 'IWM C229', delta: '0.41', spread: '2.8%', bid: '1.12', mid: '1.16', ask: '1.20', qty: 3, amt: '$348' }, cShort: { name: 'IWM P229', delta: '0.41', spread: '2.8%', bid: '1.12', mid: '1.16', ask: '1.20', qty: 3, amt: '$348' } },
      { sym: 'SPY', hot: '3', st: 'short', vwap: '−0.5', reason: null, cLong: { name: 'SPY C583', delta: '0.45', spread: '1.7%', bid: '2.30', mid: '2.36', ask: '2.42', qty: 2, amt: '$472' }, cShort: { name: 'SPY P583', delta: '0.45', spread: '1.7%', bid: '2.30', mid: '2.36', ask: '2.42', qty: 2, amt: '$472' } },
      { sym: 'AAPL', hot: '', st: 'long', vwap: '+0.9', reason: null, cLong: { name: 'AAPL C232', delta: '0.42', spread: '2.4%', bid: '1.44', mid: '1.50', ask: '1.56', qty: 2, amt: '$300' }, cShort: { name: 'AAPL P232', delta: '0.42', spread: '2.4%', bid: '1.44', mid: '1.50', ask: '1.56', qty: 2, amt: '$300' } },
      { sym: 'NVDA', hot: '', st: 'short', vwap: '−1.2', reason: null, cLong: { name: 'NVDA C178', delta: '0.46', spread: '2.0%', bid: '2.02', mid: '2.08', ask: '2.14', qty: 2, amt: '$416' }, cShort: { name: 'NVDA P178', delta: '0.46', spread: '2.0%', bid: '2.02', mid: '2.08', ask: '2.14', qty: 2, amt: '$416' } },
      { sym: 'TSLA', hot: '', st: 'block', vwap: '+0.2', reason: '横盘震荡（效率比 0.16 < 0.36）拦截', cLong: { name: 'TSLA C338', delta: '0.40', spread: '3.1%', bid: '1.88', mid: '1.96', ask: '2.04', qty: 1, amt: '$196' }, cShort: { name: 'TSLA P338', delta: '0.40', spread: '3.1%', bid: '1.88', mid: '1.96', ask: '2.04', qty: 1, amt: '$196' } },
      { sym: 'MSFT', hot: '', st: 'long', vwap: '+1.1', reason: null, cLong: { name: 'MSFT C468', delta: '0.41', spread: '2.2%', bid: '1.62', mid: '1.68', ask: '1.74', qty: 2, amt: '$336' }, cShort: { name: 'MSFT P468', delta: '0.41', spread: '2.2%', bid: '1.62', mid: '1.68', ask: '1.74', qty: 2, amt: '$336' } },
      { sym: 'META', hot: '', st: 'short', vwap: '−0.8', reason: null, cLong: { name: 'META C702', delta: '0.44', spread: '2.6%', bid: '2.44', mid: '2.52', ask: '2.60', qty: 1, amt: '$252' }, cShort: { name: 'META P702', delta: '0.44', spread: '2.6%', bid: '2.44', mid: '2.52', ask: '2.60', qty: 1, amt: '$252' } },
      { sym: 'AMD', hot: '', st: 'block', vwap: '−3.1', reason: '距VWAP −3.1×σ 杀跌拦截', cLong: { name: 'AMD C162', delta: '0.43', spread: '2.9%', bid: '1.30', mid: '1.36', ask: '1.42', qty: 2, amt: '$272' }, cShort: { name: 'AMD P162', delta: '0.43', spread: '2.9%', bid: '1.30', mid: '1.36', ask: '1.42', qty: 2, amt: '$272' } },
      { sym: 'GOOG', hot: '', st: 'long', vwap: '+0.7', reason: null, cLong: { name: 'GOOG C196', delta: '0.42', spread: '2.5%', bid: '1.20', mid: '1.26', ask: '1.32', qty: 2, amt: '$252' }, cShort: { name: 'GOOG P196', delta: '0.42', spread: '2.5%', bid: '1.20', mid: '1.26', ask: '1.32', qty: 2, amt: '$252' } },
      { sym: 'AMZN', hot: '', st: 'short', vwap: '−0.6', reason: null, cLong: { name: 'AMZN C218', delta: '0.44', spread: '2.3%', bid: '1.74', mid: '1.80', ask: '1.86', qty: 2, amt: '$360' }, cShort: { name: 'AMZN P218', delta: '0.44', spread: '2.3%', bid: '1.74', mid: '1.80', ask: '1.86', qty: 2, amt: '$360' } },
      { sym: 'SMH', hot: '', st: 'long', vwap: '+1.0', reason: null, cLong: { name: 'SMH C282', delta: '0.43', spread: '2.7%', bid: '1.52', mid: '1.58', ask: '1.64', qty: 1, amt: '$158' }, cShort: { name: 'SMH P282', delta: '0.43', spread: '2.7%', bid: '1.52', mid: '1.58', ask: '1.64', qty: 1, amt: '$158' } },
    ];
    // 风控参数默认值（与后端模板默认对齐；mount 后由 /api/templates 当前生效模板覆盖）。
    // erThresh 已删：validator ER 子条件 2026-07-18 用户拍板移除（与 trend_score 重复）。
    this.svaluesDefault = {
      // 入场闸门
      blackoutMin: 10, cutoffMins: 150, minDelta: 0.40, maxSpreadPct: 3, maxTrades: 6,
      // 熔断与冷却
      dayLoss: 150, weekLoss: 300, cooldownMin: 10, dayTarget: 300,
      // 出场（止盈止损）
      stopPct: 20, dollarLine: 80, maxHold: 15,
      orderTimeout: 30,   // ⑥ 在途挂单超时自动撤单（秒，0=停用）
    };
    this.state = {
      page: 'trade', focus: 0, posFocus: 0,
      cancelTarget: null,  // Esc 只认明确点选的订单，或明确选择持仓对应的在途卖单
      orderBusy: false,   // 下单/平仓/加仓请求进行中：驱动按钮 disabled + 处理中态，防重复点击
      unmanagedPositions: [], reconciling: false,   // 券商账户里真实存在、REGISTRY 未登记的期权仓 + 对账请求中态
      // ⑥ 买入数量 · ⑩ 价格类型。默认 'auto'=按动作方向自动取可成交那一侧（开仓/加仓挂卖一、
      // 平仓/减仓挂买一），前端此时**不发** price_type，由后端各自的默认值决定。
      // 2026-07-25 修：原默认 'ask' 是买入语义，却被减仓/清仓原样发给 /api/close →
      // 平仓挂在卖一 = 被动排队、永不成交（QQQ 减半后券商仍 2 手、1 手挂着）。
      qty: 1, qtyPresets: [1, 2, 3, 5],
      stopMode: 'percent',   // 下单时二选一；阈值来自纪律模板，界面不允许改值
      priceType: 'auto', limitPrice: '',
      pnlShown: false, searchOpen: false,   // ⑧ H 切换显示盈亏 · ① TICKER 搜索开关
      risk2: {
        trades_today: 0, max_trades_per_day: 6,
        daily_pnl: null, weekly_pnl: null,
        consecutive_losses: 0, cooldown_remaining_s: 0,
        daily_profit_target: '300', max_daily_loss: '150', max_weekly_loss: '300',
        pnl_basis: null,
      },  // ⑨ /ws 推送的真实限额与已实现盈亏

      now: T0, pnlUntil: 0, toast: null, toastKind: 'ok', pending: null,
      hbMs: null, wsDown: false, // 浏览器 ↔ 本机后端应用层 Ping RTT / 连接状态
      alpacaConnected: false, alpacaPongMs: null, // 本机后端 ↔ Alpaca TradingStream 协议层 Pong RTT
      positions: [],
      // ⑦ 订单流由 /ws 交易状态快照提供。side: buy/sell，status: pending/filled/cancelled
      orders: [],
      svalues: { ...this.svaluesDefault },
      draft: { ...this.svaluesDefault },
      pendingKeys: [],
      configName: '',
      reason: '',
      sound: true,
      configs: [],    // 数据库逐行配置；加载/删除操作都从这里发起
      marketLocked: false,
      activeTemplate: '',
      pendingTemplate: null,
      loadedTemplate: '',
      loadedTemplateStatus: 'active',
      hotkeys: loadHotkeys(),
      hotkeyDraft: loadHotkeys(),
      walls: {},
      tide: null,
      marketSession: null,   // 'open' | 'closed'，来自 feed 快照
      gexAlert: loadGexAlertEnabled(),
      // 下单自查弹窗。null = 没开；开着时是 { dir, answers, openedAt, flips }。
      // 只在开仓时出现，平仓 / 止损 / 加仓 / 撤单一律不弹。
      selfCheck: null,
    };
    this._nid = 100;
    // 每票每道墙的破墙判定基准。刻意**不进 state** —— 它每 tick 都变、
    // 只喂给提醒逻辑，进了 state 会白白排渲染。
    this._gexBreach = Object.create(null);
    this._sectionRevisions = Object.create(null);
    this.onKey = this.onKey.bind(this);
    this.onSignalWheel = this.onSignalWheel.bind(this);
  }

  // React 风 setState：支持对象或更新函数；opts.soft=true 表示可在输入聚焦时跳过重建（定时/行情心跳用）
  setState(patch, opts) {
    const p = typeof patch === 'function' ? patch(this.state) : patch;
    const signalOfflineBefore = this.state.signalOffline === true;
    Object.assign(this.state, p);
    if (signalOfflineBefore !== (this.state.signalOffline === true)) syncSignalBanner();
    scheduleRender(opts);
  }

  acceptSectionRevision(name, revision) {
    const next = Number(revision);
    if (!Number.isFinite(next)) return true;  // 兼容人工注入的旧形状快照
    const previous = Number(this._sectionRevisions[name] || 0);
    if (next <= previous) return false;
    this._sectionRevisions[name] = next;
    return true;
  }

  // 高频时钟只改对应文本节点，避免为了 1 秒跳一次的秒针重建整棵页面 DOM。
  refreshLiveHeader() {
    if (!stageEl) return;
    const d = new Date(this.state.now);
    const clock = [(d.getUTCHours() + 8) % 24, d.getUTCMinutes(), d.getUTCSeconds()]
      .map((x) => String(x).padStart(2, '0')).join(':');
    const clockWrap = stageEl.querySelector('.bi-clock');
    if (clockWrap && clockWrap.parentElement) {
      const text = Array.from(clockWrap.parentElement.childNodes).find((n) => n.nodeType === 3);
      if (text) text.nodeValue = '北京 ' + clock;
    }
    this.refreshHoldTimers();
  }

  // 持仓计时器只改对应文本节点，每秒刷新，不为秒针重建整棵页面。
  // 主值只显示已持仓时长；最大持仓由独立副标题稳定显示，避免两种格式来回闪烁。
  refreshHoldTimers() {
    if (!stageEl) return;
    const fmt = (seconds) => {
      const total = Math.max(0, Math.floor(Number(seconds) || 0));
      return String(Math.floor(total / 60)).padStart(2, '0')
        + ':' + String(total % 60).padStart(2, '0');
    };
    stageEl.querySelectorAll('[data-hold-start][data-hold-max]').forEach((el) => {
      const startMs = Number(el.getAttribute('data-hold-start'));
      const maxSec = Math.max(0, Number(el.getAttribute('data-hold-max')) || 0);
      if (!Number.isFinite(startMs) || startMs <= 0 || maxSec <= 0) return;
      const elapsedSec = Math.max(0, Math.floor((this.state.now - startMs) / 1000));
      el.textContent = fmt(elapsedSec);
      el.style.color = elapsedSec >= maxSec
        ? '#d08a82'
        : (elapsedSec >= maxSec * 0.8 ? '#d8bd6f' : '#c3cee0');
    });
  }

  // 两段链路延迟只改固定文本节点，不为 5s 心跳重建整棵交易页面。
  refreshHeartbeat() {
    if (!stageEl) return;
    const update = (containerId, labelId, text, color, anim) => {
      const container = stageEl.querySelector(containerId);
      const label = stageEl.querySelector(labelId);
      if (container) {
        container.style.color = color;
        const dot = container.querySelector('span');
        if (dot) { dot.style.background = color; dot.style.animation = anim; }
      }
      if (label) label.textContent = text;
    };
    const pingDown = this.state.wsDown;
    const pingWaiting = this.state.hbMs == null;
    update(
      '#header-heartbeat',
      '#header-heartbeat-label',
      pingDown ? 'PING 断线' : (pingWaiting ? 'PING 等待中' : ('PING ' + this.state.hbMs + 'ms')),
      pingDown ? '#e0776c' : (pingWaiting ? '#d8bd6f' : '#5f9e83'),
      pingDown ? 'hbpulse 1s ease-in-out infinite' : 'none',
    );
    const pongDown = !this.state.alpacaConnected;
    const pongWaiting = this.state.alpacaPongMs == null;
    update(
      '#header-alpaca-pong',
      '#header-alpaca-pong-label',
      pongDown ? 'PONG 断线' : (pongWaiting ? 'PONG 等待中' : ('PONG ' + this.state.alpacaPongMs + 'ms')),
      pongDown ? '#e0776c' : (pongWaiting ? '#d8bd6f' : '#5f9e83'),
      pongDown ? 'hbpulse 1s ease-in-out infinite' : 'none',
    );
  }

  // 0DTE 资金潮徽章的**唯一**取值口径：renderVals（整页渲染）与 refreshTide
  // （顶栏定点更新）两条路都从这里出，避免两处各写一份慢慢长歪。
  tideView() {
    const t = this.state.tide;
    const base = { label: '0DTE Tide', text: '--', color: '#8695ab', note: '', opacity: '1' };
    // seeded=false 时后端给的是 tide_view(0,0)，也就是「持平 $0.0M」——那和「盘中
    // 真的多空平衡」在徽章上长得一模一样。没数据就显式显示 '--'，别拿 0 冒充中性。
    if (!t || !t.seeded || t.diff == null) return base;
    const color = t.direction === 'bullish' ? '#5f9e83'
      : (t.direction === 'bearish' ? '#e0776c' : '#8695ab');
    // 非实时场次必须标出来：盘前握着的是**上一交易日终值**，不标的话和盘中实时值
    // 一模一样，看的人分不出自己盯的是今天还是昨天（后端 session_label 的用意）。
    const note = t.session === 'prev' ? '昨收' : (t.session === 'closed' ? '已收盘' : '');
    return { label: '0DTE Tide', text: t.label, color, note, opacity: note ? '.55' : '1' };
  }

  // 顶栏**不在任何 data-render-region 里**，软渲染够不着它；而硬渲染会重建整棵
  // DOM —— 那正是 2026-08-17「设置页中文打不进字」的成因。所以照 refreshHeartbeat()
  // 的老规矩：只改这几个固定节点，不排渲染。
  refreshTide() {
    if (!stageEl) return;
    const wrap = stageEl.querySelector('#header-tide');
    if (!wrap) return;                 // 设置页等不含顶栏交易态节点的形态
    const v = this.tideView();
    wrap.style.opacity = v.opacity;
    const val = stageEl.querySelector('#header-tide-value');
    if (val) { val.textContent = v.text; val.style.color = v.color; }
    const note = stageEl.querySelector('#header-tide-note');
    if (note) note.textContent = v.note;
  }

  // 市场时段徽章的唯一取值口径。休市时行情循环整体挂起（后端 market_gate），
  // 卡片冻在收盘时的值 —— 不标出来，看的人分不清盯的是实时价还是残影。
  sessionView() {
    const closed = this.state.marketSession === 'closed';
    return { display: closed ? 'inline-flex' : 'none', label: '休市 · 行情已暂停' };
  }

  // 与 refreshTide 同规矩：顶栏不在任何 data-render-region 里，只改固定节点、
  // 不排渲染（硬渲染会重建整棵 DOM，那是「设置页中文打不进字」的成因）。
  refreshSession() {
    if (!stageEl) return;
    const wrap = stageEl.querySelector('#header-session');
    if (!wrap) return;                 // 设置页等不含顶栏交易态节点的形态
    const v = this.sessionView();
    wrap.style.display = v.display;
    const label = stageEl.querySelector('#header-session-label');
    if (label) label.textContent = v.label;
  }

  mount() {
    this.iv = setInterval(() => {
      this.state.now = Date.now();
      this.refreshLiveHeader();
    }, 1000);
    this.loadTickers();
    this.loadTemplateValues();
    window.addEventListener('keydown', this.onKey);
  }

  // 将数据库单行配置的参数快照转换成设置页输入框使用的字段和单位。
  templateToDraft(tpl) {
    const n = (v) => Number(v);
    return {
      blackoutMin: n(tpl.opening_blackout_min), cutoffMins: n(tpl.entry_cutoff_mins),
      minDelta: n(tpl.min_delta), maxSpreadPct: Math.round(n(tpl.max_spread_pct) * 1000) / 10,
      maxTrades: n(tpl.max_trades_per_day),
      dayLoss: n(tpl.max_daily_loss), weekLoss: n(tpl.max_weekly_loss),
      cooldownMin: Math.round(n(tpl.cooldown_seconds) / 60), dayTarget: n(tpl.daily_profit_target),
      stopPct: Math.round(n(tpl.stop_pct) * 100), dollarLine: n(tpl.max_loss_per_trade),
      maxHold: n(tpl.max_hold_mins),
      orderTimeout: n(tpl.order_timeout_s),
    };
  }

  // active 是交易运行时基准；配置是否可修改由美股交易时段硬锁控制。
  async loadTemplateValues() {
    try {
      const response = await fetch('/api/templates');
      const t = await response.json();
      if (!response.ok) throw new Error(t.detail || ('HTTP ' + response.status));
      const rows = t.templates || [];
      const activeTpl = rows.find((x) => x.id === t.active_id);
      if (!activeTpl) return;
      const editorTpl = rows.find((x) => x.id === t.pending_id) || activeTpl;
      const activeValues = this.templateToDraft(activeTpl);
      const draft = this.templateToDraft(editorTpl);
      const pendingKeys = t.pending_id
        ? Object.keys(draft).filter((k) => String(draft[k]) !== String(activeValues[k]))
        : [];
      const configs = rows.map((row) => {
        const active = row.status === 'active';
        const pending = row.status === 'pending';
        return {
          id: row.id,
          name: row.name,
          time: row.updated_at ? new Date(row.updated_at).toLocaleString('zh-CN', {
            month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
          }) : '',
          reason: row.reason || '（未填写）',
          summary: `止损 ${Math.round(Number(row.stop_pct) * 100)}% · 日亏 $${row.max_daily_loss} · 单笔 $${row.max_loss_per_trade}`,
          active,
          pending,
          saved: !active && !pending,
          canLoad: !active && !pending,
          canDelete: !active && !pending,
          onLoad: () => this.loadConfig(row.id),
          onDelete: () => this.deleteConfig(row.id, row.name),
        };
      });
      this.setState({
        svalues: { ...activeValues }, draft, pendingKeys, configs,
        marketLocked: t.market_locked === true,
        activeTemplate: t.active || '',
        pendingTemplate: t.pending || null,
        loadedTemplate: editorTpl.name || '',
        loadedTemplateStatus: t.pending_id ? 'pending' : 'active',
      }, { soft: true, regions: ['settings'] });
    } catch (e) { console.warn('加载模板参数失败', e); }
  }

  async loadTickers() {
    try {
      const resp = await (await fetch('/api/tickers')).json();
      if (resp && Array.isArray(resp.tickers) && resp.tickers.length > 0) {
        const newTickers = resp.tickers.map((t) => {
          return {
             // /api/tickers 是观察列表，不代表每只票都已有行情。等待首个
             // watcher tick 前必须 fail-closed，绝不能伪造成“趋势上涨、可做多”。
             sym: t.sym, hot: '', st: 'block', vwap: '—',
             reason: '等待行情数据',
             cLong: { name: '—', delta: '—', spread: '—', bid: '—', mid: '—', ask: '—', qty: 1, amt: '—' },
             cShort: { name: '—', delta: '—', spread: '—', bid: '—', mid: '—', ask: '—', qty: 1, amt: '—' }
          };
        });
        newTickers.forEach((t, i) => { if (i < 9) t.hot = String(i + 1); else if (i === 9) t.hot = '0'; else t.hot = '·'; });
        this.TICKERS = newTickers;
        this.setState(
          { focus: Math.min(this.state.focus, this.TICKERS.length - 1) },
          { soft: true, regions: ['tickers', 'order-entry'] },
        );
      }
    } catch (e) {
      console.warn("Failed to load tickers", e);
    }
  }

  clamp(v, a, b) { return Math.max(a, Math.min(b, v)); }
  toast(msg, kind, durationMs) {
    if (kind === 'block') playSfx('block');
    let stack = document.getElementById('global-toast-stack');
    if (!stack) {
      stack = document.createElement('div');
      stack.id = 'global-toast-stack';
      stack.setAttribute('role', 'status');
      stack.setAttribute('aria-live', 'polite');
      stack.style.cssText = 'position:fixed;bottom:70px;left:50%;transform:translateX(-50%);z-index:10050;display:flex;flex-direction:column-reverse;align-items:center;gap:10px;width:min(92vw,760px);pointer-events:none;';
      document.body.appendChild(stack);
    }
    const tk = kind || 'ok';
    const isBlock = tk === 'block';
    const isWarn = tk === 'warn';
    const isFill = tk === 'fill';
    const toastBg = isBlock ? '#e33f3a' : isWarn ? '#df9127' : isFill ? '#2563a9' : '#23a06b';
    const toastBorder = isBlock ? '#c2312d' : isWarn ? '#c77e1e' : isFill ? '#4b86c6' : '#1a8256';
    const toastFg = '#ffffff';
    const toastIcon = isBlock ? 'bi-shield-fill-x' : isWarn ? 'bi-exclamation-circle-fill' : isFill ? 'bi-check2-circle' : 'bi-check-circle-fill';
    const el = document.createElement('div');
    el.style.cssText = 'max-width:100%;display:inline-flex;align-items:center;gap:12px;padding:15px 24px;border-radius:12px;background:' + toastBg + ';border:1px solid ' + toastBorder + ';color:' + toastFg + ';font:700 15px var(--font-sans);line-height:1.35;box-shadow:0 15px 45px rgba(0,0,0,.6),0 0 20px ' + toastBg + '40;animation:toastin .18s ease forwards;pointer-events:auto;';
    const icon = document.createElement('i');
    icon.className = 'bi ' + toastIcon;
    const text = document.createElement('span');
    // pre-line：保留换行符、照常折叠空格。破墙横幅是两行文案需要它；
    // 其余 toast 都是单行、不含换行符，行为不受影响。
    text.style.whiteSpace = 'pre-line';
    text.textContent = String(msg);
    el.append(icon, text);
    stack.appendChild(el);
    const visibleMs = Number.isFinite(Number(durationMs)) ? Number(durationMs) : 4800;
    setTimeout(() => {
      el.style.animation = 'toastout .18s ease forwards';
      el.addEventListener('animationend', () => {
        el.remove();
        if (!stack.children.length) stack.remove();
      }, { once: true });
    }, visibleMs);
  }

  flashHotkey(label, keyLetter) {
    const el = document.querySelector('[data-render-region="order-entry"]') || document.getElementById('stage');
    if (el) {
      el.classList.remove('hotkey-flash');
      void el.offsetWidth;
      el.classList.add('hotkey-flash');
      setTimeout(() => el.classList.remove('hotkey-flash'), 400);
    }
  }

  isMarketHours() {
    const d = new Date(this.state.now);
    const parts = Object.fromEntries(new Intl.DateTimeFormat('en-US', {
      timeZone: 'America/New_York', weekday: 'short',
      hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
    }).formatToParts(d).filter((p) => p.type !== 'literal').map((p) => [p.type, p.value]));
    if (parts.weekday === 'Sat' || parts.weekday === 'Sun') return false;
    const mins = Number(parts.hour) * 60 + Number(parts.minute);
    return mins >= 9 * 60 + 30 && mins < 16 * 60;
  }

  // CALL / PUT 各自判可否，不再互斥。这一条从 2026-08-08 起就没变过，中间经历了
  // 规则 6 方向闸的删除（08-08）、复位（08-21）与再次删除（09-01）——
  // **绝不回到「三选一」**：可否只认后端 gates[方向].blocked，前端不做二次推断。
  // 这里若不同步，闸门放行了买入按钮却还是灰的，用户完全感受不到后端放宽；
  // 反过来也不许自作主张放宽。
  gate() {
    const f = this.TICKERS[this.state.focus];
    if (f.candidateHeld) return { call: false, put: false, mask: null, dormant: true, reason: '已持仓' };
    const gl = f.gates && f.gates.long, gs = f.gates && f.gates.short;
    // 2026-08-14：闸门只跑到规则 7。规则 8（资金）与 9（合约点差 / delta）需要
    // Contract，按设计不在那一帧里（33 票每秒干跑一次，带 Contract 会打爆券商）。
    // 于是这两种「其实下不了单」的情况按钮照样是亮的：
    //   · 该侧还没选到合约 → 金额渲染成 `$0`（现场 ASTS PUT「选约中…」）
    //   · 实时点差已超模板上限 → 规则 9 在下单那一刻必拒（现场 ASTS CALL 18.5%）
    // 这里只用前端**已经拿到**的合约数据自查，不新增任何后端调用。
    // 与 2026-08-10 被回退的那次不同：那次是「后端选约失败就拒单」，这里只是
    // 如实反映手上的数据，绝不因为拿不到阈值就收紧（拿不到 → 不判点差）。
    const sideBlocker = (c) => {
      if (!c || !c.optionCode) return '选约中…';
      const maxPct = Number((this.state.risk2 || {}).max_spread_pct);
      const pct = parseFloat(String(c.spread).replace('%', ''));
      if (isFinite(maxPct) && maxPct > 0 && isFinite(pct) && pct > maxPct * 100) {
        return '点差 ' + pct.toFixed(1) + '% 超上限';
      }
      return null;
    };
    const callBlocker = sideBlocker(f.cLong), putBlocker = sideBlocker(f.cShort);
    const call = !!gl && gl.blocked === false && callBlocker === null;
    const put = !!gs && gs.blocked === false && putBlocker === null;
    // 灰按钮要说得出为什么：闸门拦的说闸门理由，合约不具备的说合约理由。
    const callWhy = (gl && gl.blocked === false)
      ? callBlocker : ((gl && gl.reason) || '不可入场');
    const putWhy = (gs && gs.blocked === false)
      ? putBlocker : ((gs && gs.reason) || '不可入场');
    if (!call && !put) {
      const reason = callWhy || putWhy || f.reason || '当前不可入场';
      return { call: false, put: false, mask: null, dormant: true, reason };
    }
    // 只有一边被拦时，把被拦那边的真实原因说出来（用户点了灰按钮要知道为什么）。
    // 2026-09-01 起闸门全部与方向无关，正常情况下两侧同亮 —— 单边被拦只可能来自
    // 那一侧的合约问题（选约中 / 点差超上限），说清是哪一侧仍然有用。
    const reason = (call && put) ? '' : call ? ('PUT · ' + putWhy) : ('CALL · ' + callWhy);
    return { call, put, mask: null, dormant: false, reason };
  }

  // 字母→TICKER 搜索；数字键→直接设置买入张数；交易动作统一走可配置的 Alt+字母。
  // Space/Esc/方向键保留；搜索框自身的键在 onSearchKey 处理。
  onKey(e) {
    if (isInteractiveFormControl(e.target)) return;
    if (this.state.searchOpen) return;   // 搜索开着时，交给搜索框（其 input 已聚焦）
    if (this.state.page !== 'trade') { if (e.key === 'Escape') this.gotoTrade(); return; }
    // 自查弹窗开着时它独占键盘：1/2/3 拨对应开关、空格提交、Esc 取消，其余吞掉。
    // 提交键 2026-09-03 由 Enter 改成空格（用户要求）。空格在主界面是「持仓减半」
    // （见下方 halvePos），但那一支排在本分支之后，弹窗开着时够不着，不会打架。
    // 这里的 preventDefault 同时挡住了浏览器的默认行为 —— 焦点停在某个拨杆按钮上时
    // 按空格本会再点一次那个按钮，吞掉之后空格在弹窗里只有「提交」一个含义。
    // 必须排在「数字键设张数」前面 —— 否则按 1 会跑去改下单张数。
    // Ctrl/Cmd 组合不吞：刷新、开发者工具这些照常可用。
    if (this.state.selfCheck) {
      if (e.ctrlKey || e.metaKey) return;
      e.preventDefault();
      if (e.key === 'Escape') { this.closeSelfCheck(); return; }
      if (e.key === ' ') { this.confirmSelfCheck(); return; }
      const idx = ['1', '2', '3'].indexOf(e.key);
      if (idx >= 0 && SELF_CHECK_ITEMS[idx]) this.toggleSelfCheck(SELF_CHECK_ITEMS[idx].key);
      return;
    }
    const k = e.key;
    // 数字键直接设置买入张数：1~9 对应 1~9 张，0 作为 10 张。
    // 输入框聚焦时已在上方 return，不会抢走正常编辑。
    if (/^[0-9]$/.test(k) && !e.ctrlKey && !e.metaKey && !e.altKey) {
      e.preventDefault();
      this.setQty(k === '0' ? 10 : Number(k));
      return;
    }
    // 不变：方向 / 减半 / 撤单
    if (k === 'ArrowDown') { e.preventDefault(); this.selectPosition(Math.min(this.state.positions.length - 1, this.state.posFocus + 1)); return; }
    if (k === 'ArrowUp') { e.preventDefault(); this.selectPosition(Math.max(0, this.state.posFocus - 1)); return; }
    if (k === ' ') { e.preventDefault(); this.halvePos(this.state.posFocus); return; }
    if (k === 'Escape') { this.cancelPending(); return; }
    // Alt+ 动作组合键。用 e.code / e.key 结合判定。
    // 「全部持仓全平」2026-08-21 起没有快捷键：与用户自己的键冲突，且按错一下就清空
    // 账户。它只剩持仓面板上的按钮一个入口，且点了要确认。
    if (e.altKey && !e.ctrlKey && !e.metaKey) {
      const letterFromCode = (e.code && e.code.startsWith('Key')) ? e.code.slice(3).toUpperCase() : '';
      const letterFromKey = (e.key && /^[a-zA-Z]$/.test(e.key)) ? e.key.toUpperCase() : '';
      const letter = letterFromCode || letterFromKey;
      const action = Object.keys(this.state.hotkeys).find((name) => this.state.hotkeys[name] === letter);
      if (action) {
        e.preventDefault();
        const actLabel = HOTKEY_LABELS[action] || action;
        this.flashHotkey(actLabel, letter);
        const i = this.state.posFocus;
        if (action === 'buy' || action === 'buyPut') {
          const g = this.gate();
          const side = BUY_SIDES.find((x) => x.hotkey === action);
          const allowed = side.dir === 'long' ? g.call : g.put;
          if (allowed) this.doOrder(side.dir);
          else this.toast(`${side.label}被拦 · ${g.reason || '闸门未放行 / 暂不可入场'}`, 'block');
        } else if (action === 'sell') {
          if (this.state.positions[i]) this.sellPosition(i);
          else this.toast('平仓提示 · 当前无可平持仓', 'warn');
        } else if (action === 'third') {
          if (this.state.positions[i]) this.thirdPos(i);
          else this.toast('1/3 平仓提示 · 当前无可平持仓', 'warn');
        } else if (action === 'half') {
          if (this.state.positions[i]) this.halvePos(i);
          else this.toast('半仓平仓提示 · 当前无可平持仓', 'warn');
        } else if (action === 'close') {
          if (this.state.positions[i]) this.closePos(i);
          else this.toast('全平提示 · 当前无可平持仓', 'warn');
        } else if (action === 'add') {
          const p = this.state.positions[i];
          if (p) this.addPos(i, p.addQty || 1);
          else this.toast('加仓提示 · 当前无可加持仓', 'warn');
        }
        return;
      }
      if (letter === 'P') {
        e.preventDefault();
        this.flashHotkey('显示/隐藏盈亏', 'P');
        this.toggleHide();
        return;
      }
      return;
    }
    // ① 无修饰字母 → 开 TICKER 搜索（以该字母起头）
    if (/^[a-zA-Z]$/.test(k) && !e.ctrlKey && !e.metaKey && !e.altKey) { e.preventDefault(); this.openSearch(k.toUpperCase()); return; }
  }

  setFocus(i) {
    if (i < 0 || i >= this.TICKERS.length) return;
    this.setState(
      { focus: i },
      { soft: true, regions: ['tickers', 'order-entry'] },
    );
    // ④ 滚动条跟随：把选中卡滚进可见区。renderApp() 每次重建都会把 #scroll-tickers 的
    // scrollLeft 摁回重建前的旧值（防止 1s/2s/3s 轮询触发的周期性重建把用户手动滚动的位置
    // 弹回去）——这与这里想要的"滚到新聚焦卡"直接冲突：若像之前那样另起 rAF 异步
    // scrollIntoView，很容易被下一次周期性重建的"摁回旧值"打断／冻结在半路（bug #6：滚动
    // 条不跟着走）。改成在 renderApp() 同一轮重建里、恢复旧滚动位置之后再处理这个待滚动
    // 请求，保证不会被后续任何一次重建打断；用 'auto'（非 smooth）确保一次性到位、不留可
    // 被打断的动画窗口。
    requestScrollToSym(this.TICKERS[i].sym);
  }
  onSignalWheel(e) { e.preventDefault(); const dir = e.deltaY > 0 ? 1 : -1; this.setFocus(this.clamp(this.state.focus + dir, 0, this.TICKERS.length - 1)); }
  onQtyWheel(e) {
    e.preventDefault();
    const current = Math.max(1, Math.floor(Number(this.state.qty) || 1));
    const next = Math.max(1, current + (e.deltaY > 0 ? -1 : 1));
    if (next === current) return;
    e.currentTarget.value = String(next);
    this.setState({ qty: next }, { soft: true, regions: ['order-entry'] });
    playUiScroll();
  }
}
