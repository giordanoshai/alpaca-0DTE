/* ----------------------------------------------------------------------------
 * 外壳单例：App 实例与两个 DOM 锚点
 *
 * 单独成文件、且**谁也不 import**，是为了让依赖图无环：runtime / sfx / app-* 全都
 * 要读这三个值，若把它们留在 runtime.js 里，sfx.js 读 app 就会和 runtime 互相 import。
 *
 * 导出的是 ES module 的**活绑定**（live binding，别的文件 import 到的是引用不是拷贝）：
 * bindShell() 赋值之后，所有 import 方读到的就是新值。拆分前这三个是同一文件里的
 * 模块级 let，语义完全一致 —— 所以下游调用处一个字都没改。
 * --------------------------------------------------------------------------*/

export let app = null;
export let tplEl = null;
export let stageEl = null;

export function bindShell(instance, tpl, stage) {
  app = instance;
  tplEl = tpl;
  stageEl = stage;
}
