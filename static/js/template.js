/* ----------------------------------------------------------------------------
 * 绑定运行时：直接解释设计稿的 sc-if / sc-for / {{path}} / on* 事件
 *
 * 设计稿模板原样进 index.html，不做编译；这里把模板节点 + 作用域对象构建成真实
 * DOM。isInteractiveFormControl 也放这儿 —— 它服务的是「重建时别换掉用户正在操作
 * 的控件」，跟构建是同一件事的两端。
 * --------------------------------------------------------------------------*/

// 点路径求值：'order.callBg' / 'k.0' / 字面量 true|false，针对 scope（含原型链继承外层作用域）
function resolvePath(path, scope) {
  path = path.trim();
  if (path === 'true') return true;
  if (path === 'false') return false;
  let v = scope;
  for (const p of path.split('.')) {
    if (v == null) return undefined;
    v = v[p];
  }
  return v;
}

// 字符串插值：把 "...{{ a.b }}...{{ c }}..." 里的每个 {{}} 替换为求值结果
function interpolate(str, scope) {
  return str.replace(/\{\{([^}]+)\}\}/g, (_, e) => {
    const v = resolvePath(e, scope);
    return v == null ? '' : String(v);
  });
}

const stripBraces = (s) => s.replace(/^\s*\{\{\s*|\s*\}\}\s*$/g, '');

function mapEvent(name) {
  name = name.toLowerCase();
  if (name === 'onclick') return 'click';
  if (name === 'onwheel') return 'wheel';
  if (name === 'onchange') return 'change';   // 输入框：失焦/回车提交，避免逐字重建打断输入
  if (name === 'oninput') return 'input';
  return name.slice(2);
}

// 实时软刷新不能替换用户正在操作的表单控件。尤其是原生 select：
// 下拉面板打开后若节点被 replaceChildren 替换，浏览器会立刻关闭下拉面板。
function isInteractiveFormControl(node) {
  const tag = (node && node.tagName) || '';
  return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || Boolean(node && node.isContentEditable);
}

// 从模板节点 + 作用域构建真实 DOM 节点数组
function build(tpl, scope) {
  // 文本节点：插值
  if (tpl.nodeType === Node.TEXT_NODE) {
    return [document.createTextNode(interpolate(tpl.textContent, scope))];
  }
  if (tpl.nodeType !== Node.ELEMENT_NODE) return [];

  const tag = tpl.tagName.toLowerCase();

  // 结构指令：sc-if
  if (tag === 'sc-if') {
    const cond = resolvePath(stripBraces(tpl.getAttribute('value') || ''), scope);
    return cond ? buildChildren(tpl, scope) : [];
  }

  // 结构指令：sc-for
  if (tag === 'sc-for') {
    const arr = resolvePath(stripBraces(tpl.getAttribute('list') || ''), scope) || [];
    const as = tpl.getAttribute('as') || 'it';
    const out = [];
    for (const item of arr) {
      const child = Object.create(scope); // 继承外层作用域，叠加循环变量
      child[as] = item;
      out.push(...buildChildren(tpl, child));
    }
    return out;
  }

  // 普通元素
  const el = document.createElement(tag);
  let deferredSelectValue = null;
  for (const attr of tpl.attributes) {
    const name = attr.name;
    if (name.startsWith('hint-placeholder')) continue;          // 设计工具占位提示，忽略
    const lower = name.toLowerCase();

    if (lower.startsWith('on')) {                                // 事件绑定
      const fn = resolvePath(stripBraces(attr.value), scope);
      if (typeof fn === 'function') {
        const ev = mapEvent(lower);
        el.addEventListener(ev, fn, ev === 'wheel' ? { passive: false } : false);
      }
      continue;
    }
    if (lower === 'disabled') {                                  // 布尔属性 → property
      el.disabled = !!resolvePath(stripBraces(attr.value), scope);
      continue;
    }
    if (lower === 'value') {                                     // 表单值 → property
      const value = interpolate(attr.value, scope);
      if (tag === 'select') deferredSelectValue = value;          // options 建完后再选中
      else el.value = value;
      continue;
    }
    if (lower === 'disabled') {                                  // disabled="false" 不能写成布尔属性
      const value = interpolate(attr.value, scope).trim().toLowerCase();
      el.disabled = value === 'true' || value === 'disabled' || value === '1';
      continue;
    }
    el.setAttribute(name, interpolate(attr.value, scope));       // 其余：插值后写属性
  }
  for (const child of tpl.childNodes) {
    for (const n of build(child, scope)) el.appendChild(n);
  }
  if (deferredSelectValue !== null) el.value = deferredSelectValue;
  return [el];
}

function buildChildren(node, scope) {
  const out = [];
  for (const c of node.childNodes) out.push(...build(c, scope));
  return out;
}

export {
  resolvePath,
  interpolate,
  stripBraces,
  mapEvent,
  isInteractiveFormControl,
  build,
  buildChildren,
};
