/* ----------------------------------------------------------------------------
 * 热键：默认值、中文标签、localStorage 存取
 *
 * 只负责「键位是什么」，不负责「按下去干什么」—— 后者在 app-core.js 的 onKey()。
 * --------------------------------------------------------------------------*/

const HOTKEY_STORAGE_KEY = 'tt-trading.hotkeys.v1';
const HOTKEY_DEFAULTS = Object.freeze({
  // 2026-09-01 重新拆成两个键：方向闸删除后系统不判方向，买 CALL 还是 PUT 由用户
  // 自己选，一个键给不出两个方向。`buy` 继续是 CALL（沿用 B，肌肉记忆不变），
  // PUT 新占 N。2026-08-21 到 2026-09-01 之间只有 `buy` 一个键。
  buy: 'B',
  buyPut: 'N',
  sell: 'S',
  third: 'T',
  half: 'H',
  close: 'F',
  add: 'A',
});
const HOTKEY_LABELS = Object.freeze({
  buy: '买入 CALL',
  buyPut: '买入 PUT',
  sell: '卖出指定数量',
  third: '卖出 1/3',
  half: '卖出半仓',
  close: '单仓全平',
  add: '加仓',
});

function normalizeHotkeyLetter(value) {
  const letters = String(value == null ? '' : value).toUpperCase().match(/[A-Z]/g);
  return letters && letters.length ? letters[letters.length - 1] : '';
}

function loadHotkeys() {
  try {
    const saved = JSON.parse(localStorage.getItem(HOTKEY_STORAGE_KEY) || '{}');
    // 两条方向相反的迁移，顺序不能反：
    //   更老的一版存的是 buy → 2026-08-08 拆成 buyCall/buyPut
    //   2026-08-21 又合并回 buy
    // 不迁移的话，用户自定义的键会被默认值悄悄顶掉：手指记的是旧键，按下去没反应还不报错。
    if (saved.buy && !saved.buyCall) {
      saved.buyCall = saved.buy;
    }
    if (saved.buyCall && !saved.buy) {
      saved.buy = saved.buyCall;
    }
    const next = {};
    const used = new Set(['P']);
    Object.keys(HOTKEY_DEFAULTS).forEach((action) => {
      const requested = normalizeHotkeyLetter(saved[action]) || HOTKEY_DEFAULTS[action];
      const candidates = [
        requested,
        HOTKEY_DEFAULTS[action],
        ...'ABCDEFGHIJKLMNOPQRSTUVWXYZ',
      ];
      const key = candidates.find((candidate) => candidate && !used.has(candidate));
      next[action] = key || HOTKEY_DEFAULTS[action];
      used.add(next[action]);
    });
    return next;
  } catch (e) {
    return { ...HOTKEY_DEFAULTS };
  }
}


export { HOTKEY_STORAGE_KEY, HOTKEY_DEFAULTS, HOTKEY_LABELS, normalizeHotkeyLetter, loadHotkeys };
