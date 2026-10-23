/* =============================================================================
 * 0DTE 交易终端 — 前端外壳（入口）
 *
 * 2026-09-02：原本 3385 行的单文件按职责拆进 static/js/，本文件只剩组装。
 * 拆分只搬家不改逻辑，唯一的代码改动是 setFocus() 里那处跨模块赋值改走
 * runtime.requestScrollToSym()（import 进来的活绑定不可赋值）。
 *
 * 依赖单向、不成环：
 *   app.js（组装 App 类）
 *     └─ app-core / app-trade / app-sync / app-settings / render
 *          └─ runtime（渲染调度 · WS 接缝 · 命令式叠层 · 挂载）
 *               └─ shell（app / tplEl / stageEl 三个单例）
 *          └─ template · format · self-check · gex-alert · hotkeys · sfx（叶子）
 *
 * 行情、持仓、订单和风控状态均从服务端 WS 接入；交易动作走 HTTP POST。
 * ===========================================================================*/

import { AppCore } from './js/app-core.js';
import { withTrade } from './js/app-trade.js';
import { withSync } from './js/app-sync.js';
import { withSettings } from './js/app-settings.js';
import { withRender } from './js/render.js';
import { boot } from './js/runtime.js';

// 四层 mixin 叠在基类上。四组方法名互不重复，叠加顺序不影响行为。
class App extends withRender(withSettings(withSync(withTrade(AppCore)))) {}

// type="module" 本身就是 defer，跑到这里 DOM 已解析完；readyState 判断保留，
// 是为了将来万一被人改回普通 <script> 也不会挂。
if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', () => boot(App));
else boot(App);
