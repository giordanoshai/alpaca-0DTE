/* ----------------------------------------------------------------------------
 * 设置页与全局开关：页面切换、盈亏显隐、提示音、GEX 破墙提醒、热键设置、模板配置
 * --------------------------------------------------------------------------*/

import { searchCtl } from './runtime.js';
import { GEX_ALERT_STORAGE_KEY, gexBreachStep, gexBreachToast } from './gex-alert.js';
import { HOTKEY_DEFAULTS, HOTKEY_STORAGE_KEY, normalizeHotkeyLetter } from './hotkeys.js';
import { playGexBreachSfx } from './sfx.js';

export const withSettings = (Base) => class extends Base {
  // Alt+P：按一下切换显示/隐藏（持仓盈亏 + 今日盈亏一起）。
  toggleHide() { this.setState((s) => ({ pnlShown: !s.pnlShown })); }
  reveal() { this.toggleHide(); }   // 「显示盈亏」按钮沿用
  gotoSettings() {
    this.setState({ page: 'settings' });
    this.loadTemplateValues();
  }
  gotoTrade() { this.setState({ page: 'trade' }); }
  openSearch(seed) { if (searchCtl) searchCtl.open(seed); }   // ① 命令式搜索 overlay
  closeSearch() { if (searchCtl) searchCtl.close(); }
  toggleSound() { this.setState((s) => ({ sound: !s.sound })); }

  toggleGexAlert() {
    const next = !this.state.gexAlert;
    // 持久化到 localStorage（与快捷键同法）：盘中嫌吵关掉之后，刷新不该又弹回来。
    try { localStorage.setItem(GEX_ALERT_STORAGE_KEY, next ? '1' : '0'); } catch (e) {}
    this.setState({ gexAlert: next });
  }

  checkGexBreach() {
    // 每帧对全池跑一遍：33 只 × 两道墙的纯算术，可以忽略不计。
    // 只读不写 state —— 提醒不参与任何下单判定，也不排渲染。
    if (!this.state.gexAlert) return;
    const walls = this.state.walls || {};
    const now = Date.now();
    for (const t of this.TICKERS) {
      const w = walls[t.sym];
      const px = t.price == null ? null : Number(t.price);
      const r = gexBreachStep(this._gexBreach[t.sym], {
        px,
        callWall: w ? w.call_wall : null,
        putWall: w ? w.put_wall : null,
        // 这一轮没拉到墙位（显示的是上一轮的旧值）时一律不判 —— 拿旧墙位报警就是假警。
        stale: !!(w && w.stale),
      }, now);
      this._gexBreach[t.sym] = r.next;
      for (const kind of r.breaches) {
        const wall = kind === 'call' ? r.next.callWall : r.next.putWall;
        this.toast(gexBreachToast(t.sym, kind, wall), 'warn', 6000);
        playGexBreachSfx();
      }
    }
  }
  hotkeyErrors(draft) {
    const values = Object.values(draft || {});
    const duplicate = values.find((key, index) => key && values.indexOf(key) !== index);
    if (values.some((key) => !/^[A-Z]$/.test(key || ''))) return '每项快捷键都必须是一个英文字母';
    if (values.includes('P')) return '字母 P 已固定用于显示 / 隐藏盈亏';
    if (duplicate) return `字母 ${duplicate} 已被重复使用`;
    return '';
  }
  setHotkeyDraft(action, raw, input) {
    if (!Object.prototype.hasOwnProperty.call(HOTKEY_DEFAULTS, action)) return;
    const key = normalizeHotkeyLetter(raw);
    if (input) input.value = key;
    this.state.hotkeyDraft = { ...this.state.hotkeyDraft, [action]: key };
    this.refreshHotkeySettingsUI();
  }
  refreshHotkeySettingsUI() {
    const error = this.hotkeyErrors(this.state.hotkeyDraft);
    const hint = document.getElementById('hotkey-save-hint');
    const button = document.getElementById('btn-save-hotkeys');
    if (hint) {
      hint.textContent = error || '保存后立即生效，并保存在当前浏览器';
      hint.style.color = error ? '#d08a82' : '#5f9e83';
    }
    if (button) {
      button.disabled = !!error;
      button.style.opacity = error ? '.45' : '1';
      button.style.cursor = error ? 'not-allowed' : 'pointer';
    }
  }
  saveHotkeys() {
    const error = this.hotkeyErrors(this.state.hotkeyDraft);
    if (error) { this.toast('快捷键无法保存 · ' + error, 'block'); return; }
    const hotkeys = { ...this.state.hotkeyDraft };
    try {
      localStorage.setItem(HOTKEY_STORAGE_KEY, JSON.stringify(hotkeys));
    } catch (e) {
      this.toast('快捷键仅本次生效 · 浏览器存储不可用', 'warn');
    }
    this.setState({ hotkeys });
    this.toast('快捷键已保存并生效', 'ok');
  }
  resetHotkeys() {
    const hotkeyDraft = { ...HOTKEY_DEFAULTS };
    this.setState({ hotkeyDraft }, { soft: true, regions: ['settings'] });
  }
  setDraft(k, v) { this.setState((s) => ({ draft: { ...s.draft, [k]: v } })); }

  changedKeys() { const d = this.state.draft, v = this.state.svalues; return Object.keys(d).filter((k) => String(d[k]) !== String(v[k])); }

  // 输入过程中的保存按钮/提示实时刷新（命令式改样式，不重建 DOM——重建会打断中文输入法）
  refreshSaveUI() {
    const btn = document.getElementById('btn-save-settings');
    if (!btn) return;
    const changed = this.changedKeys().length;
    const hasName = !!this.state.configName.trim();
    const hasReason = !!this.state.reason.trim();
    const canSave = hasName && hasReason && changed > 0;
    btn.style.background = canSave ? '#6366f1' : '#1c2740';
    btn.style.borderColor = canSave ? '#6366f1' : '#22314a';
    btn.style.color = canSave ? '#fff' : '#4d5c73';
    btn.style.cursor = canSave ? 'pointer' : 'not-allowed';
    const hint = document.getElementById('save-hint');
    if (hint) {
      hint.textContent = changed === 0 ? '无改动' : (!hasName ? '需填写配置名称' : (!hasReason ? '需填写修改理由' : changed + ' 项改动 · 仅保存'));
      hint.style.color = (changed > 0 && (!hasName || !hasReason)) ? '#d08a82' : '#5a6a82';
    }
  }

  // 保存只生成一个带完整快照的配置版本，不自动改变当前生效参数。
  // 真正启用必须在“配置变更历史”中明确点击切换。
  async save() {
    const ck = this.changedKeys();
    if (!this.state.configName.trim() || !this.state.reason.trim() || !ck.length || this._saving) return;
    // draft 键 → 后端模板字段（含单位换算：% → 小数、分钟 → 秒）。tp 是展示项无映射。
    const MAP = {
      blackoutMin: ['opening_blackout_min', (v) => String(v)],
      cutoffMins: ['entry_cutoff_mins', (v) => String(v)],
      minDelta: ['min_delta', (v) => String(v)],
      maxSpreadPct: ['max_spread_pct', (v) => String(Number(v) / 100)],
      maxTrades: ['max_trades_per_day', (v) => String(Math.round(Number(v)))],
      dayLoss: ['max_daily_loss', (v) => String(v)],
      weekLoss: ['max_weekly_loss', (v) => String(v)],
      cooldownMin: ['cooldown_seconds', (v) => String(Math.round(Number(v) * 60))],
      dayTarget: ['daily_profit_target', (v) => String(v)],
      stopPct: ['stop_pct', (v) => String(Number(v) / 100)],
      dollarLine: ['max_loss_per_trade', (v) => String(v)],
      maxHold: ['max_hold_mins', (v) => String(v)],
      orderTimeout: ['order_timeout_s', (v) => String(Math.max(0, Math.round(Number(v))))],
    };
    const changes = {};
    for (const k of ck) { if (MAP[k]) changes[MAP[k][0]] = MAP[k][1](this.state.draft[k]); }
    if (!Object.keys(changes).length) { this.toast('止盈规则为展示项，无可写入的参数', 'warn'); return; }
    this._saving = true;
    let resp;
    try {
      const r = await fetch('/api/templates/params', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: this.state.configName.trim(),
          changes,
          reason: this.state.reason.trim(),
        }),
      });
      resp = await r.json();
      if (!r.ok) throw new Error(resp.detail || ('HTTP ' + r.status));
    } catch (e) { this._saving = false; this.toast('保存失败 · ' + (e.message || '请求异常'), 'block'); return; }
    this._saving = false;
    if (!resp || resp.status !== 'saved') {
      this.toast('保存失败 · ' + ((resp && resp.detail) || '后端拒绝'), 'block');
      return;
    }
    this.setState({ configName: '', reason: '' });
    this.toast('配置已保存 · 可在下方加载', 'ok');
    this.loadTemplateValues();
  }

  async loadConfig(templateId) {
    if (!templateId || this._loadingConfig) return;
    if (this.state.marketLocked) {
      this.toast('美股盘中不可加载配置模板', 'warn');
      return;
    }
    this._loadingConfig = templateId;
    try {
      const response = await fetch('/api/templates/' + templateId + '/load', {
        method: 'POST',
      });
      const result = await response.json().catch(() => ({}));
      if (!response.ok || !result || result.status !== 'active') {
        throw new Error(result.detail || ('HTTP ' + response.status));
      }
      this.toast('配置已加载并生效', 'ok');
      await this.loadTemplateValues();
    } catch (e) {
      this.toast('配置加载失败 · ' + (e.message || '请求异常'), 'block');
    } finally {
      this._loadingConfig = null;
    }
  }

  async deleteConfig(templateId, name) {
    if (!templateId || this._deletingConfig) return;
    if (!window.confirm(`确认删除配置“${name}”？删除后无法恢复。`)) return;
    this._deletingConfig = templateId;
    try {
      const response = await fetch('/api/templates/' + templateId, { method: 'DELETE' });
      const result = await response.json().catch(() => ({}));
      if (!response.ok || !result.deleted) {
        throw new Error(result.detail || ('HTTP ' + response.status));
      }
      this.toast('配置已删除 · ' + name, 'ok');
      await this.loadTemplateValues();
    } catch (e) {
      this.toast('配置删除失败 · ' + (e.message || '请求异常'), 'block');
    } finally {
      this._deletingConfig = null;
    }
  }
};
