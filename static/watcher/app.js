/**
 * 4in1 Watcher - Main Application
 * Modularized version using Alpine.js + ESM
 */

import { Utils } from './js/utils.js';
import { ApiService } from './js/api.js';
import { ChartManager } from './js/charts.js';
import { WSManager } from './js/websocket.js';

// 快讯在横条上留多久。用户 2026-09-10：「不要整天的都显示，只要显示前 30 分钟的」。
//
// **必须跟后端 news_feed_service.NEWS_SHOW_MAX_AGE_S 相等**（那边 30*60 秒），
// tests/test_frontend_news_ticker.py 锁着。两边不等的症状都是无声的：这边大了，
// 刷新一下旧消息就没了、不刷新还在；这边小了，后端刚推来的条目立刻被自己剔掉，
// 表现成「有新闻却什么都不显示」。
const NEWS_SHOW_MAX_AGE_MS = 30 * 60 * 1000;

document.addEventListener('alpine:init', () => {
    Alpine.data('app', () => ({
        // ==================== 状态 ====================
        connected: false,
        tickers: [],
        newTicker: '',
        addingTicker: false,   // 加票期间后端在同步补 K 线（几秒），界面置忙
        addTickerError: '',    // 加票失败的原因，显示在输入框旁，几秒后自动消失
        // 盘中快讯（只有后端判为最高档的才会推过来，一天十几条）
        newsItems: [],
        newsIndex: 0,
        searchQuery: '',
        detailTicker: null,
        confirmDeleteTicker: null,
        wsManager: null,
        currentTime: '--:--:--',
        soundEnabled: true,
        watcherRunning: true,
        showMiniMA: true,

        // 信号记录抽屉
        signalDrawerOpen: false,
        signalRecords: [],
        signalUnreadCount: 0,

        // 事件日志抽屉
        eventLogOpen: false,
        eventLogs: [],
        eventUnreadCount: 0,

        // 策略提醒开关
        alertSwitches: { v7: true, a: true, b: true, c: true, d: true, e: true, f: true, g: true, h: true, c2: true, c3: true },

        // 策略参数设置弹窗
        strategyParamsModal: false,
        strategyKeys: ['A', 'B', 'D', 'E', 'F', 'G', 'H', 'C2', 'C3'],
        strategyParams: {},
        strategyDefaults: {},
        strategyDraft: {},
        activeStrategyTab: 'H',
        enumOptions: { slope_src: ['fast', 'slow', 'both'] },
        strategyMsg: '',

        // H 策略档位 / 全局 spread 系数滑杆（TODO P2.5，2026-07-13）
        hGears: {},
        hGearStats: {},
        globalSpreadFactor: 1.0,
        gearMsg: '',

        // 止损止盈弹窗
        positionAlertModal: false,
        positionAlert: null,
        positionAlertQueue: [],

        // 排序节拍 + 排序后的列表（手动维护，避免每 tick 重排）
        sortTick: 0,
        sortedTickersList: [],

        // 'update' 消息分帧处理队列：所有 1m bar 在同一分钟边界同时收盘，
        // 35 个 ticker 的推送会挤在同一瞬间，不分帧会在一帧内同步跑 35 次图表更新，
        // 卡住点击等输入响应（"慢半拍"的根因）。见 _scheduleFlushUpdates/_flushUpdatesChunk。
        _pendingUpdates: new Map(),
        _flushingUpdates: false,

        // 图表实例管理
        charts: {},
        detailCharts: {},
        chartData: {},

        // 视口虚拟化：只给滚进视口的卡片建/更新迷你图，滚出视口销毁，
        // 把常驻的 canvas 实例数从"全部 ticker"降到"实际可见的十几个"。
        _chartObserver: null,
        _chartDestroyTimers: {},

        // 实时帧跳过整根分钟时重取 /api/history 的节流表（symbol → 上次重取的 ms）。
        // 见 refillChartGap()：缺口可能连着几分钟反复触发，一次重取 300 根，必须压。
        _gapRefillAt: {},
        _GAP_REFILL_THROTTLE_MS: 15000,

        // UW Tide hover popover 状态（见 Market Tide 交叉提醒设计）
        _tidePopoverOpen: false,
        _tideChart: null,

        // 市场概览
        marketInfo: [
            { label: 'SPY', value: '--', change: 0 },
            { label: 'QQQ', value: '--', change: 0 },
            { label: '^VIX', value: '--', change: 0 },
            // UW 0DTE Market Tide 展示徽章（纯展示，不参与任何策略计算，见 market_tide_service.py）
            // session：累计值属于哪一场 live/closed/prev(昨收)/empty，决定要不要标注+置灰
            { label: '0DTE Tide', direction: 'flat', text: '--', diff: null, series: [], session: 'empty' },
        ],

        // ==================== 初始化 ====================
        init() {
            const savedShowMiniMA = localStorage.getItem('showMiniMA');
            if (savedShowMiniMA !== null) this.showMiniMA = savedShowMiniMA === 'true';

            this.wsManager = new WSManager(
                (data) => this.handleMessage(data),
                (status) => this.connected = status
            );
            this.wsManager.connect();

            this.loadWatchlist();
            this.loadWatcherStatus();
            this.loadAlertSwitches();
            this.loadHGears();
            this.loadGlobalSpreadFactor();
            Utils.requestNotificationPermission();
            Utils.startClock((time) => this.currentTime = time);
            this._startSortTimer();
            this.$watch('sortTick', () => this._applySort());
        },

        // ==================== 状态控制 ====================
        async loadWatcherStatus() {
            try {
                const data = await ApiService.getWatcherStatus();
                this.watcherRunning = data.running;
            } catch (e) {
                console.error('获取状态失败', e);
            }
        },

        async toggleWatcher() {
            try {
                const newStatus = !this.watcherRunning;
                const data = await ApiService.setWatcherStatus(newStatus);
                this.watcherRunning = data.running;
            } catch (e) {
                console.error('切换状态失败', e);
            }
        },

        toggleSound() {
            this.soundEnabled = !this.soundEnabled;
        },

        toggleMiniMA() {
            this.showMiniMA = !this.showMiniMA;
            localStorage.setItem('showMiniMA', String(this.showMiniMA));
            Object.values(this.charts).forEach(seriesObj => {
                ChartManager.setMaVisible(seriesObj, this.showMiniMA);
            });
        },

        async loadAlertSwitches() {
            try {
                const res = await fetch('/api/alerts/switches');
                this.alertSwitches = await res.json();
            } catch (e) {}
        },

        async toggleAlertSwitch(strategy) {
            const newVal = !this.alertSwitches[strategy];
            try {
                await fetch(`/api/alerts/switches/${strategy}?enabled=${newVal}`, { method: 'POST' });
                this.alertSwitches[strategy] = newVal;
            } catch (e) {
                console.error('切换提醒开关失败', e);
            }
        },

        // ==================== 策略参数设置弹窗 ====================
        async openStrategyParams() {
            this.strategyMsg = '';
            try {
                const res = await fetch('/api/strategy_params');
                const data = await res.json();
                this.strategyParams = data.params || {};
                this.strategyDefaults = data.defaults || {};
                if (!this.strategyParams[this.activeStrategyTab]) {
                    this.activeStrategyTab = this.strategyKeys.find(k => this.strategyParams[k]) || 'H';
                }
                this.loadStrategyDraft();
                this.strategyParamsModal = true;
            } catch (e) {
                console.error('加载策略参数失败', e);
            }
        },

        setStrategyTab(k) {
            this.activeStrategyTab = k;
            this.strategyMsg = '';
            this.loadStrategyDraft();
        },

        loadStrategyDraft() {
            // 深拷贝当前策略参数为可编辑草稿
            this.strategyDraft = JSON.parse(JSON.stringify(this.strategyParams[this.activeStrategyTab] || {}));
        },

        async saveStrategyParams(key) {
            try {
                const res = await fetch(`/api/strategy_params/${key}`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(this.strategyDraft),
                });
                const data = await res.json();
                if (data.ok) {
                    this.strategyParams[key] = data.params;
                    this.loadStrategyDraft();
                    this.strategyMsg = `✓ 已保存并生效（${data.reloaded || 0} 个ticker热重载）`;
                } else {
                    this.strategyMsg = '保存失败: ' + (data.error || '未知错误');
                }
            } catch (e) {
                this.strategyMsg = '保存失败: ' + e;
            }
        },

        async resetStrategyParams(key) {
            try {
                const res = await fetch(`/api/strategy_params/${key}/reset`, { method: 'POST' });
                const data = await res.json();
                if (data.ok) {
                    this.strategyParams[key] = data.params;
                    this.loadStrategyDraft();
                    this.strategyMsg = `✓ 已恢复默认（${data.reloaded || 0} 个ticker热重载）`;
                } else {
                    this.strategyMsg = '恢复失败: ' + (data.error || '未知错误');
                }
            } catch (e) {
                this.strategyMsg = '恢复失败: ' + e;
            }
        },

        // ==================== H 策略 gear / 全局 spread 系数（TODO P2.5）====================
        async loadHGears() {
            try {
                const res = await fetch('/api/h-gears');
                const data = await res.json();
                this.hGears = data.gears || {};
                this.hGearStats = data.stats || {};
            } catch (e) {
                console.error('加载H档位失败', e);
            }
        },

        async loadGlobalSpreadFactor() {
            try {
                const res = await fetch('/api/global-spread-factor');
                const data = await res.json();
                if (data.factor !== undefined) this.globalSpreadFactor = data.factor;
            } catch (e) {
                console.error('加载全局spread系数失败', e);
            }
        },

        async setTickerGear(symbol, gear) {
            this.gearMsg = '';
            try {
                const res = await fetch(`/api/set-gear?ticker=${symbol}&gear=${encodeURIComponent(gear)}`, { method: 'POST' });
                const data = await res.json();
                if (data.error) {
                    this.gearMsg = '换档失败: ' + data.error;
                    return;
                }
                const t = this.tickers.find(t => t.symbol === symbol);
                if (t) t.hGear = gear;
                this.gearMsg = `✓ ${symbol} 已切到 ${gear}`;
            } catch (e) {
                this.gearMsg = '换档失败: ' + e;
            }
        },

        async applyGlobalSpreadFactor() {
            try {
                const res = await fetch(`/api/global-spread-factor?factor=${this.globalSpreadFactor}`, { method: 'POST' });
                const data = await res.json();
                if (data.factor !== undefined) this.globalSpreadFactor = data.factor;
                this.strategyMsg = `✓ 全局系数已生效（${data.updated_tickers || 0} 个ticker热更新，不重置状态机）`;
            } catch (e) {
                this.strategyMsg = '全局系数设置失败: ' + e;
            }
        },

        // 用 confirm_bars=3 家族的三个参考点线性插值估算"该阈值下大约多少信号/天"（仅供展示，非精确预测）
        estimateHRate(threshold) {
            const pts = this.hGearStats.confirm_bars_3;
            if (!pts || !pts.length) return null;
            const sorted = [...pts].sort((a, b) => a.spread_threshold - b.spread_threshold);
            if (threshold <= sorted[0].spread_threshold) return sorted[0].rate_per_day;
            const last = sorted[sorted.length - 1];
            if (threshold >= last.spread_threshold) return last.rate_per_day;
            for (let i = 0; i < sorted.length - 1; i++) {
                const a = sorted[i], b = sorted[i + 1];
                if (threshold >= a.spread_threshold && threshold <= b.spread_threshold) {
                    const frac = (threshold - a.spread_threshold) / (b.spread_threshold - a.spread_threshold);
                    return a.rate_per_day + frac * (b.rate_per_day - a.rate_per_day);
                }
            }
            return null;
        },

        // 生效阈值预览，公式与后端 resolve_h_spread_threshold 一致：max(0.15, 基准 × 全局系数)
        previewThreshold(baseThreshold) {
            return Math.max(0.15, baseThreshold * this.globalSpreadFactor);
        },

        // ==================== 消息处理 ====================
        handleMessage(data) {
            if (data.type === 'signal') {
                this.handleSignal(data);
            } else if (data.type === 'signal_event') {
                this.handleSignalEvent(data);
            } else if (data.type === 'update') {
                // 高频价格/图表刷新走分帧队列（见下方 _scheduleFlushUpdates），
                // 同一 ticker 在合批窗口内多次推送只保留最后一次，不重复渲染
                this._pendingUpdates.set(data.ticker, data.data);
                this._scheduleFlushUpdates();
            } else if (data.type === 'history') {
                this.handleHistory(data);
            } else if (data.type === 'position_alert') {
                this.handlePositionAlert(data);
            } else if (data.type === 'event_log') {
                this.handleEventLog(data);
            } else if (data.type === 'market_tide') {
                this.handleMarketTide(data);
            } else if (data.type === 'market_tide_cross') {
                this.handleMarketTideCross(data);
            } else if (data.type === 'news') {
                this.handleNews(data);
            }
        },

        // 盘中快讯滚动条。后端（news_feed_service）已经筛过，推过来的**全是最高档**，
        // 前端不再做二次判断 —— 判重要性的口径只能有一处，否则两边会各说各话。
        // spec: docs/superpowers/specs/2026-09-05-盘中快讯滚动条-design.md
        handleNews(data) {
            const items = Array.isArray(data.data) ? data.data : [];
            if (!items.length) return;

            // 首帧补发（浏览器刷新时）会把队列整个送来，里面多半是已经看过的。
            // 按时间戳去重，避免刷一次页面就重新响一遍铃。
            const seen = new Set(this.newsItems.map(n => n.ts + n.en));
            const fresh = items.filter(n => !seen.has(n.ts + n.en));
            if (!fresh.length) return;

            this.newsItems.push(...fresh);
            // 后端只留 20 条，前端也得封顶：页面开一整天不封顶会越堆越长。
            if (this.newsItems.length > 20) {
                this.newsItems.splice(0, this.newsItems.length - 20);
            }
            this._pruneNews();
            this.newsIndex = this.newsItems.length - 1;   // 新消息立刻顶到最前
            this._startNewsRotation();

            if (this.soundEnabled) Utils.playAlert();
        },

        // 剔掉超过 NEWS_SHOW_MAX_AGE_MS 的条目。
        //
        // **后端那一半挡不住这里**：它只管新连接首帧的快照，而这个页面是挂着不刷新的
        // ——不在前端也剔一遍的话，上午那批会一直在横条上轮播，正是用户 2026-09-10
        // 报的「不要整天的都显示」。
        //
        // **时间戳解析不了的留下**，跟后端 _age_of() 同一个取舍：这一栏是盘中盯盘用的，
        // 漏掉一条重要消息比多挂一条旧的糟得多。
        _pruneNews() {
            const now = Date.now();
            this.newsItems = this.newsItems.filter(n => {
                const t = Date.parse(n.ts);
                return !Number.isFinite(t) || now - t <= NEWS_SHOW_MAX_AGE_MS;
            });
            if (this.newsIndex >= this.newsItems.length) {
                this.newsIndex = Math.max(this.newsItems.length - 1, 0);
            }
        },

        // 多条时轮播；只有一条就静止显示，不做无意义的切换。
        //
        // **每一拍先剔一遍旧的**：新消息稀疏的时候（盘中常常十几分钟没有一条），
        // 光在 handleNews 里剔等于永远不剔——那条 30 分钟前的消息会一直轮播下去，
        // 因为没有新消息来触发清理。
        _startNewsRotation() {
            clearInterval(this._newsTimer);
            if (this.newsItems.length <= 1) return;
            this._newsTimer = setInterval(() => {
                this._pruneNews();
                if (!this.newsItems.length) {
                    clearInterval(this._newsTimer);
                    return;
                }
                this.newsIndex = (this.newsIndex + 1) % this.newsItems.length;
            }, 8000);
        },

        currentNews() {
            return this.newsItems[this.newsIndex] || null;
        },

        // 模型挂掉时后端会退回英文原文并把 translated 置 false —— 那种情况显示原文，
        // 由 CSS 标灰区分，别让用户以为这就是翻译结果。
        newsText(item) {
            if (!item) return '';
            const body = item.translated && item.cn ? item.cn : item.en;
            return `${item.et}  ${body}`;
        },

        // UW 0DTE Market Tide 徽章更新（当前多空净额；纯展示，不参与策略计算）
        handleMarketTide(data) {
            const info = this.marketInfo.find(m => m.label === '0DTE Tide');
            if (!info) return;
            const { diff, direction, label, series, session } = data.data;
            // 后端缺 session 时按 live 兜底（旧缓存的前端/旧帧）
            info.session = session || 'live';
            const empty = info.session === 'empty';
            info.direction = empty ? 'flat' : (direction || 'flat');
            info.text = empty ? '--' : (label ?? '--');
            info.diff = empty ? null : (diff ?? null);
            if (Array.isArray(series)) info.series = series;
            // popover 正开着则实时刷新曲线
            if (this._tideChart && this._tidePopoverOpen) {
                ChartManager.setTideData(this._tideChart, info.series);
            }
        },

        // hover 展示 0DTE Market Tide 当日双线走势 popover
        showTidePopover() {
            const container = document.getElementById('tide-popover-chart');
            if (!container) return;
            this._tidePopoverOpen = true;
            const info = this.marketInfo.find(m => m.label === '0DTE Tide');
            // 容器此刻刚由 x-show 显示，宽度需在下一帧读取才准确
            requestAnimationFrame(() => {
                if (!this._tidePopoverOpen) return;
                if (!this._tideChart) {
                    this._tideChart = ChartManager.createTideChart(container);
                }
                this._tideChart.chart.applyOptions({ width: container.clientWidth || 320 });
                ChartManager.setTideData(this._tideChart, info?.series || []);
            });
        },

        hideTidePopover() {
            this._tidePopoverOpen = false;
        },

        // 0DTE Tide 徽章的数据来源。**必须按 label 找，不能按下标**：
        // marketInfo 只有 4 个元素（下标 0~3），此前模板与本文件读的都是
        // `marketInfo[4]` —— 恒为 undefined，徽章数值一直显示 `--`、配色也一直不生效。
        // 后端在推、handleMarketTide() 也确实按 label 找到对象写了值，只有显示这一环
        // 读错了位置。守卫见 tests/test_frontend_watcher_tide_badge.py。
        tideInfo() {
            return this.marketInfo.find(m => m.label === '0DTE Tide');
        },

        // 徽章场次注脚：盘中不标（省地方），非盘中必须标出来——否则昨收和实时值
        // 在徽章上完全一样，看的人分不出自己盯的是今天还是昨天
        tideSessionNote() {
            const s = this.tideInfo()?.session || 'live';
            return { closed: '今日收盘', prev: '昨收' }[s] || '';
        },

        // 陈值（昨收/无数据）置灰，和盘中实时值拉开视觉差
        tideStale() {
            const s = this.tideInfo()?.session || 'live';
            return s === 'prev' || s === 'empty';
        },

        // Market Tide 交叉提醒：声音 + 事件日志（复用现成两套机制）
        handleMarketTideCross(data) {
            const { direction, ts } = data.data;
            if (this.soundEnabled) Utils.playAlert();
            const isBull = direction === 'bullish';
            this._pushEventLog({
                category: 'market_tide',
                ticker: 'MARKET',
                message: isBull ? '📈 Market Tide 看涨交叉' : '📉 Market Tide 看跌交叉',
                details: null,
                timestamp: ts,
            });
        },

        _scheduleFlushUpdates() {
            if (this._flushingUpdates) return;
            this._flushingUpdates = true;
            requestAnimationFrame(() => this._flushUpdatesChunk());
        },

        _flushUpdatesChunk() {
            // 每帧最多处理 3 个 ticker 的图表更新，其余留到下一帧。
            // Lightweight Charts 每张图重绘 ~8ms（火焰图实测），6 张/帧≈48ms 长任务卡输入；
            // 降到 3 张/帧≈24ms，整分钟边界爆发摊到更多帧、每帧不再卡死点击。
            const CHUNK_SIZE = 3;
            let n = 0;
            for (const [ticker, state] of this._pendingUpdates) {
                this._pendingUpdates.delete(ticker);
                this.handleUpdate({ ticker, data: state });
                n++;
                if (n >= CHUNK_SIZE) break;
            }
            if (this._pendingUpdates.size > 0) {
                requestAnimationFrame(() => this._flushUpdatesChunk());
            } else {
                this._flushingUpdates = false;
            }
        },

        handleSignal(data) {
            // 前端 alertSwitch 守卫（后端已过滤，此处二次防御）
            if (this.alertSwitches.v7 === false) return;

            const { ticker, data: signalData } = data;
            const t = this.tickers.find(t => t.symbol === ticker);
            if (!t) return;

            Object.assign(t, {
                strategy: signalData,
                signals: signalData.signals,
                risk: signalData.risk,
                atrQuality: signalData.atr_quality,
                timeFactor: signalData.time_factor
            });

            const { golden_cross, death_cross, bull_pattern, bear_pattern } = signalData.signals;
            const anySignal = golden_cross || death_cross || bull_pattern || bear_pattern;
            console.log(`[Signal] ${ticker} F=${signalData.fast?.toFixed(2)} S=${signalData.slow?.toFixed(2)} | GC=${golden_cross} DC=${death_cross} BP=${bull_pattern} BAP=${bear_pattern} | FIRE=${anySignal}`);
            if (anySignal) {
                // 统一前缀格式徽章；新事件覆盖旧的（不叠加）
                const v7Label = golden_cross ? 'V7 金叉'
                              : death_cross  ? 'V7 死叉'
                              : bull_pattern ? 'V7 看涨'
                              :                'V7 看跌';
                const v7Dir = (golden_cross || bull_pattern) ? 'bull' : 'bear';
                const badge = { label: v7Label, direction: v7Dir };
                // 清理旧的清除定时器
                if (t.signalClearTimer) {
                    clearTimeout(t.signalClearTimer);
                }

                const signalId = Date.now() + '-' + Math.random();
                t.activeSignalId = signalId;

                t.activeSignalBadge = badge;
                t.activeSignalDirection = v7Dir;
                t.signalViewed = false;
                // 1 分钟后自动清除：用 ID 比较防止误清新事件
                t.signalClearTimer = setTimeout(() => {
                    if (t.activeSignalId === signalId) {
                        t.activeSignalBadge = null;
                        t.activeSignalDirection = null;
                        t.signalViewed = true;
                    }
                    t.signalClearTimer = null;
                }, 60000);
                Utils.sendNotification(ticker, signalData);
                if (this.soundEnabled) Utils.playAlert();
                // 更新未读计数；若抽屉已开则直接刷新
                if (this.signalDrawerOpen) {
                    this.loadSignalRecords();
                } else {
                    this.signalUnreadCount++;
                }
            }
        },

        handleSignalEvent(data) {
            const { ticker, data: ev } = data;
            const stratKey = (ev.strategy || '').toLowerCase();
            console.log(`[Diagnostic] Received SignalEvent strategy=${ev.strategy} type=${ev.signal_type} for ${ticker}. stratKey=${stratKey}, switchValue=${this.alertSwitches[stratKey]}, alertSwitches=${JSON.stringify(this.alertSwitches)}`);

            // 前端 alertSwitch 守卫（后端已过滤，此处二次防御）
            if (this.alertSwitches[stratKey] === false) return;

            const t = this.tickers.find(t => t.symbol === ticker);
            if (!t) return;
            t.lastSignalEvent = ev;  // 保留供详情弹窗使用

            // B 缩量整理：持续状态 badge（⚡ 整理），不进入场徽章、不闪、不响、不通知
            if (ev.signal_type === 'contraction') {
                t.contracting = true;
                return;
            }
            if (ev.signal_type === 'breakout') {
                t.contracting = false;
            }

            // ── 统一前缀文字徽章 ─────────────────────────────────────────
            // C 策略只暴露方向（不暴露具体触发指标）；其他策略用 signal_type 映射
            const LABEL_MAP = {
                cross_confirm:   'A 信号确认',
                pullback_bounce: 'A 回踩反弹',
                breakout:        'B 突破',
                pullback:        'B 回踩',
                exhaustion:      'B 衰竭',
            };
            let badgeLabel;
            if (ev.strategy === 'C') {
                badgeLabel = ev.direction === 'bull' ? 'C 买入' : 'C 卖出';
            } else {
                badgeLabel = LABEL_MAP[ev.signal_type] || `${ev.strategy} ${ev.label || ev.signal_type}`;
            }

            // 清理旧的清除定时器
            if (t.signalClearTimer) {
                clearTimeout(t.signalClearTimer);
            }

            const signalId = Date.now() + '-' + Math.random();
            t.activeSignalId = signalId;

            // 徽章 + 边框闪烁（所有策略入场信号都触发，1 分钟自动清除）
            const badge = { label: badgeLabel, direction: ev.direction };
            t.activeSignalBadge = badge;
            t.activeSignalDirection = ev.direction;
            t.signalViewed = false;
            t.signalClearTimer = setTimeout(() => {
                if (t.activeSignalId === signalId) {
                    t.activeSignalBadge = null;
                    t.activeSignalDirection = null;
                    t.signalViewed = true;
                }
                t.signalClearTimer = null;
            }, 60000);

            // 2026-07-13 用户拍板：所有入场信号一律响铃+通知，不再按 confidence 过滤
            // （原规则见 CLAUDE.md「声音触发规则」，此处是明确的用户反转决定）
            Utils.sendNotification(ticker, {
                signals: {
                    golden_cross: ev.direction === 'bull',
                    death_cross:  ev.direction === 'bear',
                    bull_pattern: false,
                    bear_pattern: false,
                }
            });
            if (this.soundEnabled) Utils.playAlert();
            if (!this.signalDrawerOpen) this.signalUnreadCount++;
            // 注：入场信号不再推送到 EVENT LOG（EVENT LOG 留给止盈止损 + 系统事件）
        },

        handleUpdate(data) {
            const { ticker, data: state } = data;
            // 更新市场状态条
            const marketItem = this.marketInfo.find(m => m.label === ticker);
            if (marketItem && state.current_bar) {
                marketItem.value = state.current_bar.close.toFixed(2);
                // 这里暂时用固定值或之前的逻辑，如果有 change 字段可以用
                // 如果没有 change，可以考虑根据 prev close 计算，但目前先简单显示价格
            }

            const t = this.tickers.find(t => t.symbol === ticker);
            if (!t) return;

            if (state.current_bar) {
                t.price = state.current_bar.close;
                t.hasData = true;
                
                // 实时更新图表
                const chartObj = this.charts[ticker];
                if (chartObj) {
                    // 卡片迷你图已改为 1m K线（loadChartData 取 '1m'），实时更新必须同用 1m current_bar，
                    // 否则 5m bar 落在 5 分钟网格上、叠加在 1m 历史右侧，导致成交量右半缺失。
                    // 注：后端 live 帧只带 current_bar（无 indicators），蜡烛/MA 只需 current_bar 即可更新，
                    // 不能用 state.indicators 作为渲染门槛（否则 K线永远不跳）。
                    const r = ChartManager.updateChartData(chartObj, state.current_bar, state.indicators);
                    // 后端实时帧跳过了整根分钟（那分钟无 tick）。真 bar 后端有，
                    // 走 /api/history 取回来，绝不在前端伪造平线。
                    if (r && r.gapDetected) this.refillChartGap(ticker);
                    // 强制维持右侧偏移空间，实现”固定在右侧且新线往左移动”的效果
                    if (chartObj.chart) chartObj.chart.timeScale().scrollToPosition(20, false);
                }

                // 如果详情窗打开，也更新详情图表
                if (this.detailTicker && this.detailTicker.symbol === ticker) {
                    const combined = this.detailCharts[`${ticker}-combined`];
                    if (combined) {
                        ChartManager.updateChartData(combined, state.current_bar, state.indicators);
                    }
                }
            }
            if (state.strategy) t.strategy = state.strategy;
            if (state.signals) t.signals = state.signals;
            if (state.risk) t.risk = state.risk;
            // 2026-09-01：`market_regime` 随震荡闸 + 方向闸一并从后端删除，
            // 趋势徽章与多空比同时撤掉。
            if (state.trend_score) t.trendScore = state.trend_score;
            if (state.atr_quality !== undefined) t.atrQuality = state.atr_quality;
            if (state.time_factor !== undefined) t.timeFactor = state.time_factor;
            if (state.h_gear !== undefined) t.hGear = state.h_gear;
            if (state.h_spread_threshold !== undefined) t.hSpreadThreshold = state.h_spread_threshold;
        },

        handleHistory(data) {
            const { ticker, data: historyData } = data;
            if (!historyData || !historyData.bars) return;
            this.chartData[ticker] = historyData.bars;
            this.refreshChart(ticker);
        },

        // ==================== Watchlist 逻辑 ====================
        async loadWatchlist() {
            try {
                const data = await ApiService.loadWatchlist();
                if (data.tickers) {
                    this.tickers = data.tickers.map(s => this.createTickerState(s));
                    this._applySort();
                    // 不再一次性给全部 ticker 建图（那是卡顿根因：35 个 canvas 常驻）。
                    // 卡片元素通过 x-init="registerCard(...)" 注册到 IntersectionObserver，
                    // 只有滚进视口的才 initChart，滚出视口销毁。
                }
            } catch (e) {
                console.error('加载 Watchlist 失败', e);
            }
        },

        async addTicker() {
            // 后端要把这只票的 K 线补完才返回（单只票通常几秒），期间置忙：一是给个
            // 在忙的迹象，二是挡住连点 —— 每点一次都会真起一轮预热。
            if (this.addingTicker) return;
            const symbol = this.newTicker.toUpperCase().trim();
            if (!symbol || this.tickers.some(t => t.symbol === symbol)) return;

            this.addingTicker = true;
            this.addTickerError = '';
            try {
                const data = await ApiService.addTicker(symbol);
                if (data.success) {
                    this.tickers.push(this.createTickerState(symbol));
                    this._applySort();
                    this.newTicker = '';
                    // 新卡片的迷你图由 IntersectionObserver 在其滚进视口时按需建立
                    this.wsManager.subscribe(symbol);
                } else {
                    // 后端 400/500 的返回体是 {error: "..."}（K 线补不齐 / 代码不合法 /
                    // 写名单失败）。以前这条路只有 console.error，界面毫无反应，用户看到
                    // 的就是"点了没用"——那句具体原因必须摆到界面上。
                    this._showAddTickerError(data.error || `${symbol} 添加失败`);
                }
            } catch (e) {
                console.error('添加 Ticker 失败', e);
                this._showAddTickerError(`${symbol} 添加失败：${e.message || '网络异常'}`);
            } finally {
                this.addingTicker = false;
            }
        },

        _showAddTickerError(msg) {
            this.addTickerError = msg;
            clearTimeout(this._addTickerErrorTimer);
            this._addTickerErrorTimer = setTimeout(() => { this.addTickerError = ''; }, 6000);
        },

        removeTicker(symbol) {
            this.confirmDeleteTicker = symbol;
        },

        async executeRemoveTicker() {
            const symbol = this.confirmDeleteTicker;
            if (!symbol) return;
            try {
                const data = await ApiService.removeTicker(symbol);
                if (data.success) {
                    this.tickers = this.tickers.filter(t => t.symbol !== symbol);
                    this._applySort();
                    this.destroyChart(symbol);
                    this.wsManager.unsubscribe(symbol);
                }
            } catch (e) {
                console.error('移除 Ticker 失败', e);
            } finally {
                this.confirmDeleteTicker = null;
            }
        },

        searchTicker() {
            const symbol = this.searchQuery.toUpperCase().trim();
            if (!symbol) return;

            const t = this.tickers.find(t => t.symbol === symbol);
            if (!t) {
                console.warn(`未找到 Ticker: ${symbol}`);
                return;
            }

            // 滚动到该卡片
            const el = document.getElementById(`card-${symbol}`);
            if (el) {
                el.scrollIntoView({ behavior: 'smooth', block: 'center' });
            }

            // 闪烁效果
            t.searchHighlighted = true;
            setTimeout(() => {
                t.searchHighlighted = false;
            }, 5000);

            this.searchQuery = '';
        },

        createTickerState(symbol) {
            return {
                symbol,
                price: null,
                priceChange: 0,
                strategy: { fast: 0, slow: 0 },
                signals: {},
                risk: {},
                trendScore: null,
                atrQuality: 0,
                timeFactor: 0,
                hGear: null,               // H 策略当前档位（TODO P2.5）
                hSpreadThreshold: null,    // H 策略当前生效阈值（gear 基准 × 全局系数）
                hasData: false,
                signalViewed: false,
                activeSignalDirection: null,   // 驱动卡片边框闪烁
                activeSignalBadge: null,       // 卡片头部信号徽章，唯一，1 分钟后自动清除
                searchHighlighted: false,
                contracting: false,            // 策略B：缩量整理状态
                lastSignalEvent: null,         // 保留，供详情弹窗等使用
            };
        },

        // ==================== 图表逻辑 ====================
        initChart(symbol) {
            // 已建则跳过（虚拟化下同一卡片可能被多次触发 enter）
            if (this.charts[symbol]) return;
            const klineContainer = document.getElementById(`chart-kline-${symbol}`);
            if (!klineContainer) return;

            const chart = LightweightCharts.createChart(klineContainer, ChartManager.createBaseOptions(klineContainer.clientHeight));
            // 迷你图砍掉成交量面板（withVolume:false）：K线+MA9+MA21 三条 series，降渲染成本
            const klineSeries = ChartManager.addKlineSeries(chart, { withVolume: false });

            this.charts[symbol] = { chart, ...klineSeries };
            ChartManager.setMaVisible(this.charts[symbol], this.showMiniMA);

            this.loadChartData(symbol);

            // 响应式（ResizeObserver 存到 chartObj 上，销毁时 disconnect，避免虚拟化反复建销时泄漏）
            const ro = new ResizeObserver(() => {
                chart.applyOptions({ width: klineContainer.clientWidth, height: klineContainer.clientHeight });
            });
            ro.observe(klineContainer);
            this.charts[symbol]._resizeObserver = ro;
        },

        // 实时帧跳过整根分钟时的补齐：重取 /api/history 拿后端的**真 bar**。
        // 等价于把用户手动刷新自动化 —— 前端不伪造任何一根（旧的 flat 平线做法已废弃，
        // 见 charts.js barGapDetected 注释）。
        //
        // 必须节流：缺口可能连着几分钟反复触发，而 /api/history 一次 300 根。
        // 同一 symbol 至少间隔 _GAP_REFILL_THROTTLE_MS 才允许再取一次。
        refillChartGap(symbol) {
            const now = Date.now();
            this._gapRefillAt = this._gapRefillAt || {};
            if (now - (this._gapRefillAt[symbol] || 0) < this._GAP_REFILL_THROTTLE_MS) return;
            this._gapRefillAt[symbol] = now;
            this.loadChartData(symbol);
        },

        async loadChartData(symbol) {
            try {
                // 迷你图 240px 高，1500 根 1m 既看不出来也纯浪费抓取/解析/渲染成本；
                // 300 根（约5小时）够用且保留手动拖拽回看的空间，详情弹窗大图仍走满量 1500
                const data = await ApiService.fetchHistory(symbol, '1m', 300);
                if (!data.bars) return;

                const chartObj = this.charts[symbol];
                if (chartObj) {
                    ChartManager.setChartData(chartObj, data.bars);
                    const t = this.tickers.find(t => t.symbol === symbol);
                    if (t) t.hasData = true;
                }
            } catch (e) {
                console.error('加载图表数据失败', symbol, e);
            }
        },

        refreshChart(symbol) {
            const bars = this.chartData[symbol];
            if (bars && this.charts[symbol]) {
                ChartManager.setChartData(this.charts[symbol], bars);
            }
        },

        // 只销毁迷你图（虚拟化：卡片滚出视口时调用；不碰详情弹窗大图）
        destroyMiniChart(symbol) {
            const obj = this.charts[symbol];
            if (!obj) return;
            obj._resizeObserver?.disconnect();
            obj.chart?.remove?.();
            delete this.charts[symbol];
        },

        destroyChart(symbol) {
            this.destroyMiniChart(symbol);
            // 清理详情
            Object.keys(this.detailCharts).forEach(key => {
                if (key.startsWith(symbol + '-')) {
                    this.detailCharts[key]?.chart?.remove();
                    delete this.detailCharts[key];
                }
            });
        },

        // ==================== 视口虚拟化（IntersectionObserver）====================
        // 卡片元素 x-init 时调用，把自己注册进共享的 observer；
        // rootMargin 提前 300px 建图避免滚动时空白闪一下。
        registerCard(el, symbol) {
            if (!el) return;
            el.dataset.chartSymbol = symbol;
            if (!this._chartObserver) {
                this._chartObserver = new IntersectionObserver((entries) => {
                    for (const entry of entries) {
                        const sym = entry.target.dataset.chartSymbol;
                        if (!sym) continue;
                        if (entry.isIntersecting) {
                            // 进入视口：取消待销毁计时，按需建图
                            if (this._chartDestroyTimers[sym]) {
                                clearTimeout(this._chartDestroyTimers[sym]);
                                delete this._chartDestroyTimers[sym];
                            }
                            if (!this.charts[sym]) this.initChart(sym);
                        } else if (this.charts[sym] && !this._chartDestroyTimers[sym]) {
                            // 离开视口：延迟 2s 销毁，快速滚动来回时不反复建销
                            this._chartDestroyTimers[sym] = setTimeout(() => {
                                delete this._chartDestroyTimers[sym];
                                // 详情弹窗正开着这只票时不销毁（大图与迷你图互不干扰，但保守起见）
                                if (this.detailTicker?.symbol === sym) return;
                                this.destroyMiniChart(sym);
                            }, 2000);
                        }
                    }
                }, { rootMargin: '300px 0px' });
            }
            this._chartObserver.observe(el);
        },

        // ==================== 详情弹窗 ====================
        showDetail(ticker, event) {
            if (event && (event.target?.closest?.('.btn-remove') || event.target?.closest?.('button'))) {
                return;
            }
            ticker.signalViewed = true;
            ticker.activeSignalDirection = null;
            if (ticker.signalClearTimer) {
                clearTimeout(ticker.signalClearTimer);
                ticker.signalClearTimer = null;
            }
            this.detailTicker = ticker;
            setTimeout(() => {
                this.initDetailCharts(ticker);
                this.fetchDetailData(ticker.symbol);
            }, 300);
        },

        hideDetail() {
            this.detailTicker = null;
        },

        initDetailCharts(ticker) {
            const symbol = ticker.symbol;
            const containerKline = document.getElementById(`detail-kline-${symbol}`);
            const containerMacd = document.getElementById(`detail-macd-${symbol}`);
            if (!containerKline || !containerMacd) return;

            // 清理
            this.destroyDetailCharts(symbol);

            const options = ChartManager.createBaseOptions(150, false);
            const klineHeight = containerKline.clientHeight || 600;
            const macdHeight = containerMacd.clientHeight || 300;
            const klineChart = LightweightCharts.createChart(containerKline, { ...options, height: klineHeight });
            const macdChart = LightweightCharts.createChart(containerMacd, { ...options, height: macdHeight });

            const klineSeries = ChartManager.addKlineSeries(klineChart);
            klineSeries.maType = 'ema';  // 大图 K 线用 EMA9/21（对齐 H 策略实际算的均线，非小卡片的 SMA）
            const macdSeries = ChartManager.addMacdSeries(macdChart);

            ChartManager.syncCharts(klineChart, macdChart);

            this.detailCharts[`${symbol}-kline`] = { chart: klineChart, ...klineSeries };
            this.detailCharts[`${symbol}-macd`] = { chart: macdChart, ...macdSeries };
            // 合并后的 series 引用必须稳定复用（不能每次调用现建字面量对象），
            // 否则 EMA9/21 的增量状态（_emaState）和 volMa 的滑动窗口（_bars）每次都会被扔掉重置
            this.detailCharts[`${symbol}-combined`] = {
                kline: klineSeries.kline,
                ma9: klineSeries.ma9,
                ma21: klineSeries.ma21,
                volume: klineSeries.volume,
                volMa: klineSeries.volMa,
                maType: klineSeries.maType,
                macdHist: macdSeries.hist,
                macdLine: macdSeries.line,
                signalLine: macdSeries.signal,
            };

            const ro = new ResizeObserver(entries => {
                for (const entry of entries) {
                    const { width, height } = entry.contentRect;
                    if (width > 0 && height > 0) {
                        if (entry.target === containerKline) {
                            klineChart.applyOptions({ width, height });
                            klineChart.timeScale().fitContent();
                        } else if (entry.target === containerMacd) {
                            macdChart.applyOptions({ width, height });
                            macdChart.timeScale().fitContent();
                        }
                    }
                }
            });
            ro.observe(containerKline);
            ro.observe(containerMacd);
        },

        destroyDetailCharts(symbol) {
            ['kline', 'macd'].forEach(type => {
                const key = `${symbol}-${type}`;
                if (this.detailCharts[key]) {
                    this.detailCharts[key].chart.remove();
                    delete this.detailCharts[key];
                }
            });
            delete this.detailCharts[`${symbol}-combined`];
        },

        async fetchDetailData(symbol) {
            try {
                const data = await ApiService.fetchHistory(symbol, '1m');
                if (!data.bars) return;

                const combined = this.detailCharts[`${symbol}-combined`];
                if (combined) {
                    ChartManager.setChartData(combined, data.bars);
                }
            } catch (e) {
                console.error('加载详情数据失败', symbol, e);
            }
        },

        // ==================== 辅助方法 ====================
        hasActiveSignal(ticker) {
            const s = ticker.signals || {};
            return s.golden_cross || s.death_cross || s.bull_pattern || s.bear_pattern;
        },

        hasRiskState(ticker) {
            const r = ticker.risk || {};
            return r.in_opening || r.extreme_volatility || r.in_saturation;
        },

        _startSortTimer() {
            const now = new Date();
            const msToNextMinute = (60 - now.getSeconds()) * 1000 - now.getMilliseconds();
            setTimeout(() => {
                this.sortTick++;
                setInterval(() => { this.sortTick++; }, 60000);
            }, msToNextMinute);
        },

        _applySort() {
            const PINNED = ['SPY', 'QQQ'];
            const pinned = PINNED
                .map(s => this.tickers.find(t => t.symbol === s))
                .filter(Boolean);

            const trendRank = (t) => {
                const ts = t.trendScore;
                if (!ts) return 0;
                const quality = ts.data_quality_ok !== undefined ? ts.data_quality_ok : ts.tradable;
                if (quality === false) return -1;  // 行情质量/流动性不足，排到最后
                return Math.abs(ts.trend_display ?? 0);
            };

            const rest = this.tickers
                .filter(t => !PINNED.includes(t.symbol) && t.symbol !== '^VIX')
                .sort((a, b) => trendRank(b) - trendRank(a));

            this.sortedTickersList = [...pinned, ...rest];
        },

        // ==================== 信号记录抽屉 ====================
        async openSignalDrawer() {
            this.signalDrawerOpen = true;
            this.signalUnreadCount = 0;
            await this.loadSignalRecords();
        },

        closeSignalDrawer() {
            this.signalDrawerOpen = false;
        },

        async loadSignalRecords() {
            try {
                // 抽屉只保留最近 50 条：盯盘看最新信号足够，200 行 DOM 白白拖慢开/关抽屉
                const res = await fetch('/api/signals?limit=50');
                this.signalRecords = await res.json();
            } catch (e) {
                console.error('加载信号记录失败', e);
            }
        },

        async clearSignalRecords() {
            await fetch('/api/signals', { method: 'DELETE' });
            this.signalRecords = [];
        },

        async deleteSignalRecord(id) {
            await fetch(`/api/signals/${id}`, { method: 'DELETE' });
            this.signalRecords = this.signalRecords.filter(r => r.id !== id);
        },

        sigSource(rec) {
            // 优先用数据库里的 source 字段，fallback 从 signal_type 前缀推断
            if (rec && rec.source) return rec.source;
            const type = typeof rec === 'string' ? rec : rec?.signal_type;
            if (!type) return 'V7';
            if (type.startsWith('A_')) return 'A';
            if (type.startsWith('B_')) return 'B';
            if (type.startsWith('C_')) return 'C';
            return 'V7';
        },

        sigTypeLabel(type) {
            return {
                golden_cross:    '▲ 金叉',
                death_cross:     '▼ 死叉',
                bull_pattern:    '◆ 看涨',
                bear_pattern:    '◆ 看跌',
                cross_confirm:   '✓ 信号确认',
                pullback_bounce: '↩ 回踩反弹',
                contraction:     '⚡ 蓄力整理',
                breakout:        '🚀 放量突破',
                pullback:        '↩ 趋势回踩',
                exhaustion:      '⚠ 动量衰竭',
                price_breakout:  '▲ 金叉突破',
                price_breakdown: '▼ 死叉破位',
                close_breakout:  '▲ 宽松金叉',
                close_breakdown: '▼ 宽松死叉',
                gap_expansion:   '▲ gap扩大',
                gap_compression: '▼ gap压缩',
                ema_entry_bull:  '▲ EMA金叉入场',
                ema_entry_bear:  '▼ EMA死叉入场',
                ema_exit_long:   '↩ EMA出场(多)',
                ema_exit_short:  '↩ EMA出场(空)',
                ema_cross:       '⚡ EMA穿越',
                macd_cross:      '⚡ MACD穿越',
                rsi_cross:       '⚡ RSI穿越',
                ema_vol_bull:    '▲ H双闸金叉',
                ema_vol_bear:    '▼ H双闸死叉',
                // 即时档（instant_0.15，现役三只在用）：交叉当根发 bull/bear（无声），
                // 拉开到阈值再发 bull2/bear2（响铃）。老档位下这四个的语义是
                // 「确认 / 二次确认」，文案按现役档位写。
                h_spread_bull:   '▲ H金叉',
                h_spread_bear:   '▼ H死叉',
                h_spread_bull2:  '▲▲ H金叉已拉开',
                h_spread_bear2:  '▼▼ H死叉已拉开',
            }[type] || type;
        },

        regimeLabelClass(label) {
            if (!label) return 'regime-flat';
            if (label.startsWith('bull')) return 'regime-bull';
            if (label.startsWith('bear')) return 'regime-bear';
            return 'regime-flat';
        },

        formatSignalTime(isoStr) {
            if (!isoStr) return '--';
            try {
                return new Date(isoStr).toLocaleTimeString('en-US', {
                    timeZone: 'America/New_York',
                    hour12: false,
                    hour: '2-digit',
                    minute: '2-digit',
                    second: '2-digit',
                }) + ' ET';
            } catch { return isoStr.slice(11, 19); }
        },

        // ==================== 事件日志抽屉 ====================
        handlePositionAlert(data) {
            const { ticker, data: a } = data;
            if (this.soundEnabled) {
                Utils.playAlert();
            }
            // 同步推送到事件日志
            this._pushEventLog({
                category: a.category,
                ticker,
                message: a.message,
                timestamp: a.timestamp,
            });
            // 弹窗展示
            const alertObj = { ticker, ...a };
            if (this.positionAlertModal) {
                this.positionAlertQueue.push(alertObj);
            } else {
                this.positionAlert = alertObj;
                this.positionAlertModal = true;
            }
        },

        closePositionAlert() {
            this.positionAlertModal = false;
            this.positionAlert = null;
            // 若队列有待处理，延迟 200ms 后显示下一条
            if (this.positionAlertQueue.length > 0) {
                setTimeout(() => {
                    this.positionAlert = this.positionAlertQueue.shift();
                    this.positionAlertModal = true;
                }, 200);
            }
        },

        alertTypeName(type) {
            return {
                stop_atr:          'ATR 硬止损',
                stop_regime_drop:  'Regime 衰减',
                stop_regime_zero:  'Regime 穿零轴',
                stop_time:         '持仓超时',
                tp_score_pullback: '评分峰值回撤',
                tp_regime_extreme: 'Regime 极值',
            }[type] || type;
        },

        handleEventLog(data) {
            const { data: ev } = data;
            this._pushEventLog({
                category: ev.category,
                ticker: ev.ticker,
                message: ev.message,
                details: ev.details || null,
                timestamp: ev.timestamp,
            });
        },

        _pushEventLog(entry) {
            this.eventLogs.unshift({ id: Date.now() + Math.random(), ...entry });
            if (this.eventLogs.length > 300) this.eventLogs.pop();
            if (!this.eventLogOpen) this.eventUnreadCount++;
        },

        openEventLog() {
            this.eventLogOpen = true;
            this.eventUnreadCount = 0;
        },

        closeEventLog() {
            this.eventLogOpen = false;
        },

        clearEventLog() {
            this.eventLogs = [];
        },

        formatEventTime(isoStr) {
            if (!isoStr) return '--';
            try {
                return new Date(isoStr).toLocaleTimeString('en-US', {
                    timeZone: 'America/New_York',
                    hour12: false,
                    hour: '2-digit',
                    minute: '2-digit',
                    second: '2-digit',
                }) + ' ET';
            } catch { return isoStr.slice(11, 19); }
        },

        eventCategoryLabel(cat) {
            return {
                monitor:     'MON',
                stop_loss:   'STOP',
                take_profit: 'T/P',
                market_tide: 'TIDE',
            }[cat] || cat.toUpperCase();
        },
    }));
});
