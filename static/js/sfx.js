/* ----------------------------------------------------------------------------
 * 提示音
 *
 * 读 shell 里的 app 只为拿设置页「提示音」总开关（app.state.sound）——
 * 这是本文件唯一的外部依赖，也是它不能并进 runtime.js 的原因（会成环）。
 * --------------------------------------------------------------------------*/

import { app } from './shell.js';

// 提示音：风险/自动撤共用 stop_lose；手动撤单确认用 cancel。缺文件时静默。
// 由 state.sound 门控；首次用户交互后浏览器才允许播放（自动播放策略），故惰性初始化。
const SFX_FILES = {
  block: 'block.mp3',
  fill: 'submit.mp3',
  risk: 'stop_lose.mp3',
  cancel: 'cancel.mp3',
  uiScroll: 'ui_scoll.mp3',
  gexBreach: 'wall.mp3',
};
const SFX = { block: null, fill: null, risk: null, cancel: null, uiScroll: null, gexBreach: null };
function initSfx() {
  Object.entries(SFX_FILES).forEach(([kind, filename]) => {
    try { const a = new Audio('/static/sounds/' + filename); a.preload = 'auto'; SFX[kind] = a; } catch (e) {}
  });
}
function playSfx(kind) {
  if (!app || !app.state || app.state.sound === false) return;
  const base = SFX[kind];
  if (!base) return;
  try {
    const a = base.cloneNode(true);
    a.volume = kind === 'uiScroll' ? 0.32 : 0.5;
    const p = a.play();
    if (p && p.catch) p.catch(() => {});
  } catch (e) {}
}
// 破墙提示音的**全局**冷却（不是每票）。五只票同时破墙要弹五条横幅，
// 但只能响一声 —— 五声叠在一起是噪音，反而听不出发生了什么。
const GEX_SFX_COOLDOWN_MS = 2000;
let lastGexSfxAt = 0;
function playGexBreachSfx() {
  const now = performance.now();
  if (now - lastGexSfxAt < GEX_SFX_COOLDOWN_MS) return;
  lastGexSfxAt = now;
  playSfx('gexBreach');   // playSfx 自带设置页「提示音」总开关门控
}

let lastUiScrollSfxAt = 0;
function playUiScroll() {
  const now = performance.now();
  if (now - lastUiScrollSfxAt < 40) return;
  lastUiScrollSfxAt = now;
  playSfx('uiScroll');
}

export { initSfx, playSfx, playGexBreachSfx, playUiScroll };
