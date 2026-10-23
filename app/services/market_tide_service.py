# -*- coding: utf-8 -*-
"""
market_tide_service.py
=======================
UW 0DTE Market Tide 展示徽章（纯展示，不接入任何策略/信号引擎）。

数据源：UW REST `/api/net-flow/expiry?expiration[]=zero_dte` 周期轮询（30s 一轮）。
专门过滤当天到期（0DTE）期权的主动买卖净权利金流向（ask_side - bid_side）。

⚠️ 核心数据特性与口径事实（2026-08-20 逐条真拉接口验过，不是看文档推的）：
  - **REST** `/api/net-flow/expiry`：每分钟一根，字段是当日开盘至今的 **0DTE 累计净值**；
    实测整日 390 根，覆盖 09:30–15:59 ET。
  - 响应是**两层嵌套**：`data` 是「分组」列表、组内的 `data` 才是点列表
    （`UwClient.zero_dte_tide` 负责解包）。分组维度是 `tide_type × moneyness`，
    **与 expiration 无关** —— 各传单值时恒为 1 组，所以取 `data[0]` 是安全的；
    哪天要同时问多个 tide_type，这个假设立刻失效。
  - `timestamp` 是 **UTC 带 `Z`**（旧的 market-tide 接口给的是 ET 带 `-04:00`）。
    两种都带时区，`fromisoformat` 解析后转 epoch 都正确，别"顺手"改成手动补时区。
  - **`date` 参数真的生效**（实测 08-18 / 08-19 各得各自当天数据，顶层 `date` 回显正确）。
    盘前启动回填依赖这条 —— 一旦 UW 哪天改成忽略 `date`，`seed_previous_session`
    会把"今天"当成"昨天"回填，正是它 docstring 里记的那个 2026-08-14 事故。
  - 请求参数**不带方括号**（`expiration=zero_dte`）与官方文档的 `expiration[]=zero_dte`
    实测结果逐位相同；且不传该参数时数值完全不同（08-19 收盘 +23.2M vs 0DTE 的 −18.7M），
    证明过滤确实生效、不是被静默忽略。
  - 主动买为正、主动卖为负，累计线全天可涨可跌。
  - 本模块展示口径：
      diff = 累计 0DTE net_call_premium - 累计 0DTE net_put_premium
    即「0DTE 净看涨减净看跌」：call 为正=主动买 0DTE call（看涨），put 为正=主动买 0DTE put
    （看跌），两者相减就是日内 0DTE 市场情绪净值。
  - ⚠️ **量级比原全市场口径小一个数量级**：0DTE 的 diff 日间摆幅实测 **±20M 上下**
    （08-12～08-19 六个交易日：21.4 / 24.6 / 28.7 / 22.6 / 15.4 / 22.1 百万美元），
    而原 `/api/market/market-tide` 是 ±100M～750M。**别把这个小数值当成口径错误**
    —— 这是切 0DTE 的必然结果。凡是照旧量级标定的阈值都得重标：`cross_hysteresis_usd`
    已于 2026-08-20 由 5M 重标为 1.0M，见 `detect_cross`。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time as _dtime, timedelta

from app.config import MARKET_TIDE_CONFIG
from app.core import market_gate
from app.core.bar_builder import NTPTimeSync

logger = logging.getLogger("tt.market_tide")

_RTH_START = _dtime(9, 30)
_RTH_END = _dtime(16, 0)

# 轮询节拍。REST 0DTE market-tide 每分钟才出一根；
# 30s 保证新根出现后最多 30s 内看到，请求量 2 次/分钟。
_TIDE_POLL_INTERVAL_S = 30.0

# 播种/轮询这一次 REST 调用给足超时：中转站计算全网 0DTE 净流需 15~20s，超时提升至 30s 防误杀
_SEED_HTTP_TIMEOUT_S = 30.0
# 播种的三种结果。**故意不用 bool**：`if await load_tide_day(...)` 这种写法下
# "error"/"empty" 都是 truthy，会把失败当成功——逼调用方显式判等。
_SEED_OK = "ok"          # 拿到数据，已写入累加器
_SEED_EMPTY = "empty"    # 请求成功但当日无数据 ⇒ 那天休市，可以往前找
_SEED_ERROR = "error"    # 网络/中转站异常 ⇒ **那天有没有数据根本没问过**，只能重试
# 网络异常时同一天最多问几次、每次之间等多久。中转站实测「第一次超时、4 秒后成功」，
# 短退避即可；上限存在是为了中转站长时间挂掉时不死磕一天，否则整个回填功能失效。
_SEED_ERROR_ATTEMPTS = 3
_SEED_ERROR_BACKOFF_S = 2.0


def _safe_float(v, default: float = 0.0) -> float:
    """安全转换为 float，遇到 None、空串或非数字时返回 default。"""
    if v is None:
        return default
    try:
        return float(v)
    except (ValueError, TypeError):
        return default


def _format_tide_label(diff: float, direction: str) -> str:
    if direction == "flat":
        return "持平 $0.0M"
    millions = abs(diff) / 1_000_000
    sign = "+" if diff > 0 else "-"
    word = "看多" if direction == "bullish" else "看空"
    return f"{word} {sign}${millions:.1f}M"


def tide_view(cum_call: float, cum_put: float) -> dict:
    """由**累计**的 call/put 净权利金算出徽章展示字段。"""
    diff = cum_call - cum_put
    if diff > 0:
        direction = "bullish"
    elif diff < 0:
        direction = "bearish"
    else:
        direction = "flat"
    return {"diff": diff, "direction": direction, "label": _format_tide_label(diff, direction)}


def compute_tide_current(ticks: list[dict]) -> dict:
    """【REST 口径】取最新一根**累计值**，算 diff = net_call - net_put。

    REST `/api/net-flow/expiry` 返回的是开盘至今的当日 0DTE 累计值（按分钟聚合）。
    故最后一根的 net_call_premium 就是当前累计看多，不需要累加。

    返回三态：
      - 有数据：{"diff": float, "direction": "bullish"|"bearish"|"flat", "label": str}
      - 空数据：{"diff": None, "direction": "flat", "label": "--"}
    """
    valid = [
        t for t in (ticks or [])
        if t.get("net_call_premium") is not None and t.get("net_put_premium") is not None
    ]
    if not valid:
        return {"diff": None, "direction": "flat", "label": "--"}
    last = valid[-1]
    return tide_view(_safe_float(last["net_call_premium"]), _safe_float(last["net_put_premium"]))


def build_tide_series(ticks: list[dict]) -> list[dict]:
    """把 REST 流水压成前端画图用的 series 数组 [{t: epoch_s, call, put}, ...]。"""
    series = []
    for t in (ticks or []):
        if t.get("net_call_premium") is None or t.get("net_put_premium") is None:
            continue
        ts_str = t.get("timestamp")
        if not ts_str:
            continue
        try:
            dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        series.append({
            "t": int(dt.timestamp()),
            "call": _safe_float(t["net_call_premium"]),
            "put": _safe_float(t["net_put_premium"]),
        })
    return series


def _minute_epoch(ts_raw) -> int | None:
    """把报文 timestamp 归一成所在分钟的 epoch 秒；解析不了返回 None。"""
    try:
        ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return int(ts.timestamp()) // 60 * 60


def detect_cross(diff: float, state: dict, hysteresis: float) -> dict | None:
    """带滞回带的 0 轴穿越检测。state={'zone': ...} 原地更新，跨周期持有。

    - diff >= +hysteresis → 进 above 区；上一 zone 是 below → bullish 交叉
    - diff <= -hysteresis → 进 below 区；上一 zone 是 above → bearish 交叉
    - 死区（|diff| < hysteresis）→ 不改 zone、不报
    - 同方向不重复；从 neutral 首次越界只建 zone 不报

    喂进来的 diff 必须是**累计**口径。

    `cross_hysteresis_usd` **2026-08-20 已由 5M 重标为 1.0M**，别按老印象改回去：
    5M 是照被取代的全市场 market-tide 口径定的，0DTE 的日间摆幅只有 ±20M 上下
    （见模块 docstring），5M 死区占掉两三成摆幅。六个交易日真实序列扫描的报警次数：
    5M→共 4 次（08-14、08-19 整天不报）、2M→9 次、**1.0M→15 次**、0.75M→21 次、
    0.5M→29 次。选 1.0M 是因为它约 2.5 次/天、每天都有信号且无空白日，最接近旧口径
    的密度。**换数据源就要重扫这张表** —— 阈值绑的是量级，不是绑的"经验值"。
    """
    zone = state.get("zone", "neutral")
    if diff >= hysteresis:
        new_zone = "above"
    elif diff <= -hysteresis:
        new_zone = "below"
    else:
        return None  # 死区
    if new_zone == zone:
        return None  # 同向不重复
    state["zone"] = new_zone
    if zone == "neutral":
        return None  # 首次建立初始 zone，不报
    direction = "bullish" if new_zone == "above" else "bearish"
    return {"direction": direction, "diff": diff}


# ══════════════════════════════════════════════════════════════════════════════
# REST 当日累计值状态持有者
# ══════════════════════════════════════════════════════════════════════════════


class TideRestState:
    """REST 轮询拿到的当日累计值。

    与被它取代的 `TideAccumulator` 的根本区别：REST 返回的**就是**当日累计
    （`/api/net-flow/expiry` 每分钟一根，字段是开盘至今的 0DTE 累计），
    所以不需要 base/delta 双层拆分、不需要播种反解、不需要重放缓冲、不需要 cutoff 对齐。
    每轮直接用最新一次 REST 结果整体替换。

    2026-08-15 从 WS 迁回 REST 时引入。WS 时代那套累加语义的两个已知故障
    （断线期间增量永久丢失、播种失败污染 base 导致方向整个是前一天的）随之从根上消失。

    属性契约（`tide_snapshot()` 只用这 5 个，改动会波及前端）：
      cum_call / cum_put / seeded / series / session_date
    """

    def __init__(self) -> None:
        self.cum_call = 0.0
        self.cum_put = 0.0
        # 语义：最近一次 REST 拉取成功过（原 TideAccumulator 里是"播种完成"）。
        # 对前端而言含义不变：有没有可信的累计值。
        self.seeded = False
        self.series: list[dict] = []
        # 当前累计值属于哪个交易日（美东 "YYYY-MM-DD"）。空串表示还没有任何数据。
        # 消费方靠它区分「盘中实时值」与「上一交易日终值」——见 session_label()。
        self.session_date = ""

    def reset(self, session_date: str = "") -> None:
        """开盘清零（UW 的累计值本身也是每日开盘重置）。

        清零时机是**开盘那一刻**而非自然日跨日：跨日清零会让 00:00–09:30 ET
        （＝北京 12:00–21:30，正是盘前盯盘时段）的徽章显示成 0/NEUTRAL，
        而那和「盘中真的多空平衡」完全无法区分。推迟到 09:30 才清，
        盘前就能一直显示上一交易日终值。
        """
        self.__init__()
        self.session_date = session_date

    def update(self, ticks: list[dict]) -> None:
        """用一次 REST 返回的当日累计流水整体替换状态。

        `ticks` 为空时**不动任何数**：当日尚无数据（开盘前）或这次没问到，
        都不该把已有的值清零成 0/NEUTRAL。
        """
        valid = [
            t for t in (ticks or [])
            if t.get("net_call_premium") is not None and t.get("net_put_premium") is not None
        ]
        if not valid:
            return
        last = valid[-1]
        self.cum_call = _safe_float(last["net_call_premium"])
        self.cum_put = _safe_float(last["net_put_premium"])
        self.series = build_tide_series(valid)
        self.seeded = True


_UW_CLIENT = None


def _uw_client():
    """常驻单例 UwClient（懒建）。

    **不要改回每次新建**：2026-08-15 起 `UwClient` 内部持有一条常驻
    `requests.Session`（连接池）且没有 close()，每 30s 新建一个就是每 30s
    漏一个连接池 —— 本机有过 WinError 10055 本地端口耗尽的前科。
    复用之后连接也复用了，顺带省掉每次约 0.6s 的 SOCKS5 + TLS 握手。
    """
    global _UW_CLIENT
    if _UW_CLIENT is None:
        from app.integrations.uw import UwClient
        _UW_CLIENT = UwClient(http_timeout=_SEED_HTTP_TIMEOUT_S)
    return _UW_CLIENT


async def load_tide_day(state: TideRestState, date_str: str) -> str:
    """拉某一交易日的 0DTE (net-flow/expiry) REST 当日累计值并写入 state。

    返回 `_SEED_OK` / `_SEED_EMPTY` / `_SEED_ERROR` —— **不是布尔**，
    调用方必须显式判等（见常量处的说明）。

    ⚠️ 三态是 2026-08-14 一个真实 bug 的修复，**不要图省事改成 bool**：
    `_SEED_EMPTY`（那天休市，可以往前找）与 `_SEED_ERROR`（那天有没有数据根本没问到，
    只能同日重试）混为一谈，会让一次网络抖动被当成整个交易日的缺席——
    详见 `seed_previous_session` 的 docstring。

    当日轮询与启动回填**共用**本函数。
    """
    def _fetch():
        return _uw_client().zero_dte_tide(date_str)

    try:
        ticks = await asyncio.to_thread(_fetch)
    except Exception as e:  # noqa: BLE001 —— 拉取失败只降级，保持现值不清零
        logger.warning(f"[0DTE MarketTide] REST 拉取失败（保持现值，稍后重试）: {e}")
        return _SEED_ERROR
    if not ticks:
        return _SEED_EMPTY

    state.update(ticks)
    return _SEED_OK


async def seed_previous_session(state: TideRestState, now_et: datetime,
                                max_lookback: int = 5) -> bool:
    """启动时不在盘中的话，用最近一个有数据的交易日给累加器播种。

    累加器是内存态，进程一重启就没了上一场的终值 —— 而盘前重启恰恰是日常操作，
    不回填的话「保留上一交易日终值」在重启后就退化成空徽章。

    不需要交易日历：周末直接跳过，节假日 REST 返回空（`_SEED_EMPTY`），顺势再往前
    找一天。`max_lookback` 计的是**实际发起请求的交易日**数，周末跳过不消耗预算
    —— 否则「周一早上 + 周五休市」会在还没试几天时就用完。

    **网络异常绝不能当成「那天没数据」**（2026-08-14 11:23 线上实测的 bug）：
    08-13 播种时中转站 SOCKS 超时，旧实现直接往前跳、回填了 08-12，徽章显示
    净额 -106,089,408（看空 $106M），而 08-13 真实收盘是 +314,246,584（看多 $314M）
    —— 量级和方向全是前一天的。这污染的是 base（基准）而非 delta，
    盘中 resync 也救不回来（它对齐的是**当日** REST）。故 `_SEED_ERROR` 时
    在同一天上退避重试，只有 `_SEED_EMPTY` 才往前找。
    """
    day = now_et.date()
    # 今日 09:30 之前，今天自己还没有数据，从昨天找起
    if now_et.time() < _RTH_START:
        day -= timedelta(days=1)

    attempts = 0
    scanned = 0
    while attempts < max_lookback and scanned < max_lookback + 10:  # 兜底防跑飞
        scanned += 1
        if day.weekday() >= 5:                      # 周六 / 周日
            day -= timedelta(days=1)
            continue

        # 重试同一天不消耗回看预算：预算计的是交易日，不是 HTTP 请求次数
        attempts += 1
        date_str = day.strftime("%Y-%m-%d")
        for i in range(_SEED_ERROR_ATTEMPTS):
            result = await load_tide_day(state, date_str)
            if result != _SEED_ERROR:
                break
            if i < _SEED_ERROR_ATTEMPTS - 1:
                logger.warning(
                    f"[MarketTide] {date_str} 播种遇网络异常，"
                    f"{_SEED_ERROR_BACKOFF_S}s 后重试同一天"
                    f"（{i + 2}/{_SEED_ERROR_ATTEMPTS}）——"
                    "不能当成这天没数据，否则会回填成前一个交易日"
                )
                await asyncio.sleep(_SEED_ERROR_BACKOFF_S)
        if result == _SEED_OK:
            state.session_date = date_str
            logger.info(
                f"[MarketTide] 已回填 {date_str} 终值："
                f"净额 {state.cum_call - state.cum_put:,.0f}"
            )
            return True
        day -= timedelta(days=1)

    logger.warning(f"[MarketTide] 回看 {attempts} 个交易日未找到可回填数据")
    return False


def session_label(acc: TideRestState | None, now_et: datetime) -> str:
    """按累计值的 session_date 与当前 ET 时间推导场次标签。

    返回 4 态（盯盘页徽章直接读这个字段）：
      - 'empty'  : 从未成功拉到过任何数据（启动初期 / 无 key / 连续报错）
      - 'live'   : 今日 09:30–16:00 RTH 期间的实时值
      - 'closed' : 今日已收盘后的终值（16:00 之后，或今日非 RTH 时段但拿到的是今日终值）
      - 'prev'   : 上一交易日的终值（盘前 00:00–09:30 显示昨收）
    """
    if acc is None or not getattr(acc, "session_date", ""):
        return "empty"
    today_str = now_et.strftime("%Y-%m-%d")
    is_today = acc.session_date == today_str
    if is_today:
        if _RTH_START <= now_et.time() <= _RTH_END:
            return "live"
        return "closed"
    return "prev"


def tide_snapshot(state: TideRestState | None, now_et: datetime, with_series: bool = True) -> dict:
    """构造一帧 market_tide 数据。连线首帧与循环广播共用它，口径只此一份。

    `session` 字段告诉前端这个累计值属于哪一场（见 `session_label`）。没有它的话，
    盘前握着的**上一交易日终值**在徽章上和盘中实时值长得一模一样，看的人分不出
    自己盯的是今天还是昨天。
    """
    if state is None:
        view = {"diff": None, "direction": "flat", "label": "--"}
        series: list[dict] = []
    else:
        view = tide_view(state.cum_call, state.cum_put)
        series = [dict(p) for p in state.series]
    data = {
        **view,
        "seeded": bool(state is not None and state.seeded),
        "session": session_label(state, now_et),
        "ts": now_et.isoformat(),
    }
    if with_series:
        data["series"] = series
    return data


async def broadcast_tide(state: TideRestState, manager, now_et: datetime,
                         cross_state: dict, with_series: bool) -> dict:
    """按当前累计值广播徽章，并在带滞回带穿越 0 轴时补一条交叉提醒。

    `with_series` 为 False 时不带 series——前端 `handleMarketTide()` 对缺失的
    series 保留上一次的值。
    """
    data = tide_snapshot(state, now_et, with_series=with_series)
    await manager.broadcast({"type": "market_tide", "data": data})

    # 交叉提醒只在**盘中实时值**上判定：陈值（昨收 / 今日终值）本身不再变化，它
    # 再"穿"一次 0 轴纯属场次切换的副作用，而这条提醒会直接响铃 + 写事件日志。
    if data["session"] == "live":
        hysteresis = MARKET_TIDE_CONFIG.get("cross_hysteresis_usd", 1_000_000)
        cross = detect_cross(data["diff"], cross_state, hysteresis)
        if cross:
            await manager.broadcast({
                "type": "market_tide_cross",
                "data": {**cross, "ts": now_et.isoformat()},
            })
    return data


async def market_tide_refresh_loop(manager) -> None:
    """REST 轮询 UW 0DTE net-flow/expiry 当日累计值，广播徽章 + 交叉提醒。

    纯展示，不接策略。函数签名与旧版一致，main.py 调用方式不变。

    2026-08-15 从 WS 迁回 REST：WS 实测天花板是 2.02 次断线/小时、存活中位 11 分钟，
    而 REST 的返回**就是**当日累计，不需要累加、播种反解与周期性对齐。
    """
    time_sync = NTPTimeSync()
    time_sync.sync()
    cross_state: dict = {"zone": "neutral"}
    state = TideRestState()
    global _ACTIVE_ACC
    _ACTIVE_ACC = state
    # 最近一次推给前端的场次标签。非 RTH 期间只在它变化时补一帧（收盘 live→closed、
    # 启动回填出昨收），而不是整夜每 30 秒推一个不变的值。
    last_pushed_session = ""

    # ── 启动回填 ──
    # 非盘中启动时先把上一场终值捞回来。不做的话，每天早上重启完徽章都是空的，
    # 「盘前显示昨收」就只在进程整夜不重启时才成立。盘中启动无需回填：下面的
    # 常规轮询会用今日 REST 累计值覆盖。
    try:
        if MARKET_TIDE_CONFIG.get("enabled", True):
            _now_et = time_sync.get_ntp_time()
            if not (_RTH_START <= _now_et.time() <= _RTH_END):
                if await seed_previous_session(state, _now_et):
                    # 立刻推一帧：盯盘页是被推送驱动的，不推的话已经开着的浏览器
                    # 要一直空到 09:30 才有数。
                    data = await broadcast_tide(state, manager, _now_et, cross_state,
                                                with_series=True)
                    last_pushed_session = data["session"]
    except Exception as e:  # noqa: BLE001 —— 回填失败只是没有昨收，不能拖垮轮询
        logger.warning(f"[MarketTide] 启动回填失败（不影响后续轮询）: {e}")

    while True:
        try:
            if not MARKET_TIDE_CONFIG.get("enabled", True):
                await asyncio.sleep(60)
                continue

            now_et = time_sync.get_ntp_time()

            # ── 非 RTH：不拉取、且**不清零** ──
            # 不清零的理由：清零推迟到开盘那一刻，盘前徽章才能显示上一交易日终值，
            # 而不是一个和「盘中真中性」分不清的 0/NEUTRAL。
            #
            # market_gate 那一条是**外加**的，不能拿它替换掉下面的 RTH 判断：
            # 闸门窗口是 09:25–16:05（比 RTH 宽 5 分钟），换过去会让开盘清零提前到
            # 09:25 发生，正好破坏上面那条「清零推迟到开盘那一刻」的用意。
            # 加在外面只多拿一样东西：节假日识别（纯时钟判不出来）。
            if (not market_gate.data_window_open(now_et)
                    or not (_RTH_START <= now_et.time() <= _RTH_END)):
                session = session_label(state, now_et)
                if session != last_pushed_session:
                    last_pushed_session = session
                    await broadcast_tide(state, manager, now_et, cross_state,
                                         with_series=True)
                await asyncio.sleep(_TIDE_POLL_INTERVAL_S)
                continue

            # ── 开盘清零：按交易日切换，不是自然日跨日 ──
            date_str = now_et.strftime("%Y-%m-%d")
            if date_str != state.session_date:
                state.reset(date_str)
                cross_state.clear()
                cross_state["zone"] = "neutral"

            result = await load_tide_day(state, date_str)
            if result == _SEED_OK:
                data = await broadcast_tide(state, manager, now_et, cross_state,
                                            with_series=True)
                last_pushed_session = data["session"]
            # _SEED_EMPTY（当日尚无数据）/ _SEED_ERROR（没问到）都保持现值不动，
            # 下一轮再试。绝不清零。

            await asyncio.sleep(_TIDE_POLL_INTERVAL_S)

        except asyncio.CancelledError:
            break
        except Exception as e:  # noqa: BLE001
            logger.error(f"[MarketTide] 轮询循环异常: {e}")
            await asyncio.sleep(5)


# ── 统一出口访问器（SOS 引擎等消费 bias 用）─────────────────────────────────────

_ACTIVE_ACC = None


def current_accumulator():
    """当前活跃的 TideRestState（由 market_tide_refresh_loop 创建并持有）。

    market_tide 数据统一出口约定：实时数据只由本模块的 REST 轮询获取、只在本模块维护；
    任何消费方（0DTE 徽章等）都只读本状态，不另发请求、不另做累加。未启动时返回 None。

    函数名保留 `current_accumulator` 是为了不动 `watcher_ws.py` 的调用点；
    2026-08-15 起它返回的已经不是累加器，而是 REST 快照持有者。
    """
    return _ACTIVE_ACC
