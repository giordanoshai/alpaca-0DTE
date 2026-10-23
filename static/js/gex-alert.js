/* ----------------------------------------------------------------------------
 * GEX 破墙提醒：判定逻辑（纯函数）
 *
 * 只出「要不要响 + 新状态 + 横幅文案」，不碰 DOM、不发请求、不进 validator。
 * 响不响由 app-settings.js 的 checkGexBreach() 接线，声音在 sfx.js。
 * --------------------------------------------------------------------------*/

// ── GEX 破墙提醒 ────────────────────────────────────────────────────────────
//
// 价格涨破 call wall / 跌破 put wall 时弹横幅 + 提示音。判据全部用**已有数据**算：
// 墙位来自 gex_wall_service（30s 一轮），现价来自 feed（每 tick），不新增任何
// UW 请求。**这是提醒不是闸门** —— 不拦单、不改单、不进 validator。
//
// 0.1% 是**迟滞带**：破要超过 +0.1%，复位要退回 −0.1%，中间那段保持上一次判定。
// 没有这条，价格贴着墙抖会每 tick 来回触发。
const GEX_BREACH_BUFFER = 0.001;
// 每票每道墙的提醒冷却。破墙后价格常在墙附近反复，5 分钟一次足够。
const GEX_BREACH_COOLDOWN_MS = 5 * 60 * 1000;
// 2026-09-01 **改成默认关**（用户决策）。
//
// 原因：这个提醒把「涨破 call wall」和「跌破 put wall」当成同一件事报，而这两道墙
// 在净 Gamma 为正和为负时根本不是一回事 —— 正 Gamma 时做市商反向对冲，墙是真阻力，
// 价格被钉在附近，破墙才算个事件；负 Gamma 时做市商顺势对冲、墙压根不挡，同样的
// 「破墙」只是波动被放大的副产物。不分正负一律报，等于在负 Gamma 那半边一直发假警报。
//
// 这一点跟上面那段【±GEX 标签当前不显示】里写的是同一件事的两面：净 Gamma 符号
// 这一环没接进来，破墙提醒就没法说清自己在提醒什么。**代码全部保留**，
// 设置页的开关随时能开回来；要真正修好得先把 dir 口径的净 Gamma 符号接进判据。
//
// 存储键从 v1 提到 v2：老浏览器里存着的 '1'（曾手动开过）必须一起作废，
// 否则「默认关」对已经开过的人不生效 —— 那正是要关掉的那些人。
const GEX_ALERT_STORAGE_KEY = 'tt-trading.gexAlert.v2';

function loadGexAlertEnabled() {
  // 默认**关**：只有显式存过 '1' 才开。读不到（无痕窗口 / 禁用存储）也当关 ——
  // 这条提醒在负 Gamma 那半边是假警报，宁可不响。
  try { return localStorage.getItem(GEX_ALERT_STORAGE_KEY) === '1'; } catch (e) { return false; }
}

// 【±GEX 标签当前不显示】2026-08-21 删除，2026-08-25 更正了删除的理由。
// 当初判定「拿现价跟 gamma_flip 比大小推正负号」这个判据本身是错的，依据是拉
// spot-exposures 对照后 10 只票 7 错 1 对。**那次是口径错配**：gex-levels 返回里
// 自带 source="vol"，而当时用来对照的是 oi 口径的净 Gamma —— 两个不同的东西互比。
// 同一天同一条线改按 dir 口径（客户主动买卖方向，盘中该用的口径）重算，「价在
// flip 上方 ⇔ 净 Gamma 为正」符合率 86.7%，拆开是「上方 90.8% vs 下方 46.5%」，
// 差 44pp —— 判据是成立的。
// 现在不恢复的理由换成了：「-GEX 时破墙会不会顺势走」这一环还没验（-GEX 实测放大
// 波动 1.90 倍，但有符号收益约等于零、不指方向），而标签一旦显示 -GEX 就等于对每次
// 破墙提醒说「这次可信，追」，与防追高目的冲突。
// 恢复时要直读 gamma_per_one_percent_move_dir 真值，别再拿价位推。
// 详见 tests/test_frontend_no_derived_net_gex.py 里的完整实测表。

// 纯函数：上一次状态 + 这一刻的价与墙位 → 要不要响 + 新状态。
//
// 做成纯函数而不是写进方法里，是因为四种「绝不许响」的情形（数据 stale / 墙位
// 自己变了 / 冷却中 / 首次观测）盘中靠肉眼验不出来，必须能单测。
// 见 tests/test_frontend_gex_wall_alert.py。
const gexBreachStep = (prev, cur, nowMs) => {
  const p = prev || {};
  const next = {
    callWall: null, putWall: null, callSide: null, putSide: null,
    // null = 从没响过（不是 0）。用 0 当初值的话，页面刚打开的头 5 分钟
    // 会被自己的冷却判断吞掉第一次提醒。
    callTs: p.callTs == null ? null : p.callTs,
    putTs: p.putTs == null ? null : p.putTs,
  };
  const breaches = [];
  const px = Number(cur && cur.px);
  const priceOk = isFinite(px) && px > 0;

  const track = (kind, wallRaw, from, to) => {
    const wall = Number(wallRaw);
    // 数据不可信：基准留空（next 里该键保持 null），下一帧重新记。
    // 绝不在这里补报 —— stale 期间价格怎么走过去的没人知道，补出来就是假警。
    if (!priceOk || (cur && cur.stale) || !isFinite(wall) || wall <= 0) return;
    next[kind + 'Wall'] = wall;
    const prevWall = Number(p[kind + 'Wall']);
    const prevSide = p[kind + 'Side'] || null;
    // 首次拿到这只票，或墙位数字刚变：只记基准不响。
    // call_wall 从 605 掉到 598 而价格没动，价格「突然在墙上面」是数据更新不是突破。
    if (!prevSide || !isFinite(prevWall) || prevWall !== wall) {
      next[kind + 'Side'] = px >= wall ? 'above' : 'below';
      return;
    }
    const side = px > wall * (1 + GEX_BREACH_BUFFER) ? 'above'
      : px < wall * (1 - GEX_BREACH_BUFFER) ? 'below'
        : prevSide;
    next[kind + 'Side'] = side;
    const lastTs = p[kind + 'Ts'];
    if (prevSide === from && side === to
        && (lastTs == null || nowMs - lastTs >= GEX_BREACH_COOLDOWN_MS)) {
      breaches.push(kind);
      next[kind + 'Ts'] = nowMs;
    }
  };

  track('call', cur && cur.callWall, 'below', 'above');   // 涨破 call wall
  track('put', cur && cur.putWall, 'above', 'below');     // 跌破 put wall
  return { next, breaches };
};

// 横幅文案。只报「哪只票、向哪边破了哪道墙、墙位多少」。
// 原本还接一个 ±GEX 当「该信几分」的参考，2026-08-21 删 —— 那个标签本身是错的，
// 见上面【别再把 ±GEX 加回来】。
// 文案是**警示**不是播报：2026-08-25 实测（6 只票 × 10 个交易日）破墙后追进去
// 是零到略负期望（30 分钟：−GEX −0.02bp、+GEX −1.31bp，还没算期权点差）。
// **不按 ±GEX 分情况措辞** —— 两种环境的跟进差别只有 0.4~2.3pp，落在「一辈子
// 验不出来」的区间，分开说等于解读噪音。详见 docs/reports/2026-08-25-GEX破墙策略验证.md。
const gexBreachToast = (sym, kind, wall) => {
  const action = kind === 'call' ? '涨破 call wall' : '跌破 put wall';
  return 'GEX 突破 · ' + sym + ' ' + action + ' ' + Number(wall).toFixed(1)
    + '\n追进 30 分钟平均负期望，别追';
};


export { GEX_ALERT_STORAGE_KEY, loadGexAlertEnabled, gexBreachStep, gexBreachToast };
