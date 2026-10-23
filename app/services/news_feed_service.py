# -*- coding: utf-8 -*-
"""盘中快讯滚动条：轮询 UW Tradex → 翻译分档 → 只推最高档给盯盘页。

spec: `docs/superpowers/specs/2026-09-05-盘中快讯滚动条-design.md`

**纯展示，不参与任何策略计算**，与 market_tide 徽章同级。

为什么是这套口径（2026-09-05 实测，别凭感觉改回去）：

- **只认 `sources=Tradex`**：UW 盘中 83% 的条目来自这个源，剩下的是公关通稿
  （"某公司美容仪器全球第一"）。它是唯一有效的筛选维度。
- **绝不用 `is_major`**：全天 315~760 条、盘中每 1.5~3 分钟一条，44% 的条目都带这个
  标记。加上它只省 18% 的量，却会漏掉"英国央行首席经济学家说要把基准利率加到 4%"
  这种——那条实测就是 `is_major=False`。
- **绝不用 `sentiment`**：抽 1000 条全是 `neutral`，字段是死的。
- **绝不用 `tickers`**：仅 30% 填充，且把 New **York** 的 YORK、北美 NA 当成股票代码。
- **窗口含盘前**：9/3 那天唯一有价值的消息（Waller 倾向 9 月不加息）在 **08:33**。
  只开盘中等于把它排除掉，功能就白做了。
"""
from __future__ import annotations

import asyncio
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time as dtime, timedelta, timezone

from app.core.dailylogger import get_logger
from app.integrations.llm import LlmClient, LlmUnavailable, parse_json_array

logger = get_logger("tt.news")

_ET = timezone(timedelta(hours=-4))

# 窗口两端都含。16:05 跟 market_gate 的收盘缓冲对齐；08:00 是本功能特有的
# ——盘前那两个半小时正是 Waller 讲话落地的时段。
NEWS_WINDOW_START = dtime(8, 0)
NEWS_WINDOW_END = dtime(16, 5)

QUEUE_MAX = 20          # 横条只看当下，留 20 条足够回头扫一眼
BATCH_MAX = 8           # 一批 8 条实测 65 秒；再多会被 max_tokens 截断
SEEN_MAX = 500          # 去重集合上限，够覆盖大半天的量
POLL_INTERVAL_S = 60.0
FETCH_LIMIT = 100       # UW 的 limit 硬上限就是 100（填 200 直接 422）

# 超过这个岁数的条目**不送模型**（但仍登记为已见）。
# 2026-09-05 真实端到端踩到：UW 一次返回最近 100 条，冷启动时全都"没见过"，
# 被切成 13 批送模型、一批 30~65 秒 —— 首轮光翻译就要跑好几分钟，而轮询间隔只有
# 60 秒。且那 100 条大多是几小时前的旧闻，滚动条要的是当下，翻它们既没意义又烧 token。
# 15 分钟的余量足够覆盖短暂断线后的补齐（Tradex 盘中约每 2~3 分钟一条）。
NEWS_MAX_AGE_S = 15 * 60

# 翻好的条目在滚动条上留多久。用户 2026-09-10：「那个新闻啊，你不要整天的都显示，
# 我觉得没必要啊，只要显示前 30 分钟的就行。」
#
# **跟上面那个 15 分钟管的是两件事，别混**：那个管「这条还值不值得花 token 去翻」，
# 这个管「翻好的还该在屏幕上留多久」。**这个必须比那个大**——反过来的话会出现刚翻
# 完就已经不该显示了，白烧一轮 token（`test_展示窗口比送模型那道龄限宽` 锁着）。
#
# **前端 static/watcher/app.js 的 NEWS_SHOW_MAX_AGE_MS 必须跟这个相等**：后端只管
# 新连接那一帧的快照，页面开着不刷新时，剔除旧条目是前端自己的事。少改一处的症状
# 是「刷新之后干净了，可挂着不动的那个页面还在轮播上午的消息」。
NEWS_SHOW_MAX_AGE_S = 30 * 60

# 一轮最多送几批。极端情况（长时间断线后恢复）也不能一口气塞几十批进去；
# 超出的按时间取最新的那些，旧的丢掉——同样是"只看当下"。
MAX_BATCHES_PER_ROUND = 3

# 每条输出按 250 token 算：这个模型出正文前有一段看不见的思考，
# 8 条给 1500 实测只写到第 5 条就被截断了。
_TOKENS_PER_ITEM = 250

_EXECUTOR: ThreadPoolExecutor | None = None

# 模型挂掉时的兜底词表。**必须按词匹配**：`rate` 藏在 Accelerated / Corporate 里，
# 子串匹配会把 FDA 批药当成利率消息。
_FALLBACK_WORDS = (
    "fed", "fomc", "rate", "rates", "inflation", "cpi", "ppi", "tariff", "tariffs",
    "payroll", "payrolls", "jobless", "unemployment", "treasury", "yields",
    "powell", "warsh", "waller", "hike", "hikes", "recession",
    "nvda", "qqq", "tsla", "nvidia", "tesla",
)
_FALLBACK_RE = re.compile(r"\b(" + "|".join(_FALLBACK_WORDS) + r")\b", re.I)

_PROMPT_HEAD = """你是 0DTE 日内期权交易员的新闻助手，交易标的：NVDA、QQQ、TSLA。

逐条处理下面的英文快讯，只输出 JSON 数组，不要任何解释文字。每条给出：
- i: 序号（与输入一致）
- cn: 中文标题，一句话，不超过 30 字
- lv: 重要性。2=可能立刻影响美股大盘或上述三只票（美联储、利率、通胀、就业数据、
  关税、这三只票自身的重大消息）；1=值得知道但不影响当下交易（海外央行、其他大公司、
  地缘政治）；0=噪音（公司公关、海外小国、体育娱乐、加密货币技术分析）
- dir: 对美股方向，bull / bear / neutral

英文快讯：
"""


def _executor() -> ThreadPoolExecutor:
    """本模块**专属**线程池（懒建单例）。

    **为什么不能用 `asyncio.to_thread`**：默认 executor 与 `app/routers/orders.py`
    的「点击→提交」共用同一个池。本模块一次模型调用实测 **15~65 秒**，挂在那个池上
    等于长时间占着下单要用的槽 —— 下单延迟本来就在查（点击→提交实测 max 14.7s）。
    同理由见 `gex_wall_service._executor()`。

    ⚠️ 专池不受 `asyncio.run()` 的 `shutdown_default_executor()` 管辖，
    循环退出时要自己 shutdown。
    """
    global _EXECUTOR
    if _EXECUTOR is None:
        _EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="newsfeed")
    return _EXECUTOR


def in_news_window(now_et: datetime) -> bool:
    """现在该不该拉快讯：工作日 08:00~16:05 ET。

    不接 `market_gate.data_window_open()`：它从 09:25 才开门，而本功能的全部价值
    恰恰在盘前那段。节假日会白拉几轮，但每分钟一次请求对 UW 每日 20 万次额度
    可以忽略，换来的是实现简单且不依赖券商时钟。
    """
    if now_et.weekday() >= 5:
        return False
    return NEWS_WINDOW_START <= now_et.timetz().replace(tzinfo=None) <= NEWS_WINDOW_END


def _age_of(ts: object, now_et: datetime) -> float:
    """这个时间戳距今多少秒。

    **解析不了、或者压根没有，一律返回 0**（当成「刚刚发生」）。这一栏是盘中盯盘
    用的，漏掉一条重要消息的代价，比多挂一条旧的大得多。

    **两个调用方共用这一份实现**（「这条要不要送模型」和「这条还该不该显示」）——
    各写一份的话，哪天有人把 `Z` 后缀那道处理改了一处，症状是两边对同一条消息给出
    不同的年龄，而日志上完全正常。
    """
    try:
        created = datetime.fromisoformat(str(ts or "").replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    return (now_et - created).total_seconds()


class NewsFeedService:
    def __init__(self, uw=None, llm=None):
        self._uw = uw
        self._llm = llm if llm is not None else LlmClient()
        self._seen: set[str] = set()
        self._seen_order: list[str] = []
        self._queue: list[dict] = []

    # ── 依赖懒建：测试注入假对象，生产走全局 UW 客户端 ──
    def _uw_client(self):
        if self._uw is None:
            from app.services.market_tide_service import _uw_client as shared
            self._uw = shared()
        return self._uw

    def recent(self) -> list[dict]:
        """队列里现在有什么，旧→新。**这是内部视图**，去重和裁剪都以它为准。"""
        return list(self._queue)

    def visible(self, now_et: datetime) -> list[dict]:
        """此刻**该显示**的那些（最近 `NEWS_SHOW_MAX_AGE_S`），旧→新。

        跟 `recent()` 分开是有意的：合成一个的话，队列裁剪那几条用例就得跟着时钟
        走，而它们测的根本不是时间。**而且这里绝不能就地裁队列**——队列还担着去重
        的职责，裁掉之后同一条旧闻下一轮会被当成新的重新翻一遍。

        `now_et` 由调用方给，这一层不读时钟（跟 `poll_once` 同一条）。
        """
        return [item for item in self._queue
                if _age_of(item.get("ts"), now_et) <= NEWS_SHOW_MAX_AGE_S]

    def _remember(self, key: str) -> None:
        self._seen.add(key)
        self._seen_order.append(key)
        while len(self._seen_order) > SEEN_MAX:
            self._seen.discard(self._seen_order.pop(0))

    async def poll_once(self, now_et: datetime) -> list[dict]:
        """拉一轮，返回本轮新增的最高档条目（已进队列）。"""
        if not in_news_window(now_et):
            return []

        try:
            rows = await self._run(
                lambda: self._uw_client().news_headlines(
                    source="Tradex", limit=FETCH_LIMIT))
        except Exception as e:  # noqa: BLE001 —— 拉取失败只是这一轮没有，下轮再来
            logger.warning(f"[News] UW 拉取失败（跳过本轮）: {e}")
            return []

        fresh = [r for r in (rows or []) if self._key(r) not in self._seen]
        if not fresh:
            return []
        # 无论后面翻不翻、翻成不成，这批都算见过了 —— 否则模型每挂一轮，同一批就被
        # 重新翻译一次，15 秒的调用会滚雪球；超龄的旧闻也会每轮重新判一次龄。
        for r in fresh:
            self._remember(self._key(r))

        # 只翻最近的：冷启动那 100 条里绝大多数是几小时前的旧闻（详见 NEWS_MAX_AGE_S）
        recent = [r for r in fresh if self._age_s(r, now_et) <= NEWS_MAX_AGE_S]
        if not recent:
            return []
        recent.sort(key=lambda r: r.get("created_at") or "")
        cap = MAX_BATCHES_PER_ROUND * BATCH_MAX
        if len(recent) > cap:
            # 排序是旧→新，取尾部＝留下最新的那些
            logger.info(f"[News] 本轮 {len(recent)} 条超出单轮上限，只处理最新 {cap} 条")
            recent = recent[-cap:]

        picked: list[dict] = []
        for i in range(0, len(recent), BATCH_MAX):
            picked += await self._classify(recent[i:i + BATCH_MAX])

        for item in picked:
            self._queue.append(item)
        while len(self._queue) > QUEUE_MAX:
            self._queue.pop(0)
        return picked

    @staticmethod
    def _key(row: dict) -> str:
        return (row.get("headline") or "").strip()

    @staticmethod
    def _age_s(row: dict, now_et: datetime) -> float:
        """条目有多老（秒）。时间戳缺失或解析不了时返回 0 —— 宁可多翻一条，不漏真消息。"""
        return _age_of(row.get("created_at"), now_et)

    async def _run(self, fn):
        return await asyncio.get_running_loop().run_in_executor(_executor(), fn)

    async def _classify(self, batch: list[dict]) -> list[dict]:
        """一批送模型翻译分档；模型不可用则退回关键词兜底。"""
        titles = [self._key(r) for r in batch]
        prompt = _PROMPT_HEAD + "\n".join(f"{i}. {t}" for i, t in enumerate(titles))
        try:
            raw = await self._run(
                lambda: self._llm.complete(
                    prompt, max_tokens=_TOKENS_PER_ITEM * len(batch)))
        except LlmUnavailable as e:
            logger.warning(f"[News] 模型不可用，退回关键词兜底: {e}")
            return [self._fallback_item(r) for r in batch if self._is_macro(self._key(r))]
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[News] 分档异常，退回关键词兜底: {e}")
            return [self._fallback_item(r) for r in batch if self._is_macro(self._key(r))]

        out: list[dict] = []
        for obj in parse_json_array(raw):
            try:
                idx = int(obj.get("i"))
                lv = int(obj.get("lv"))
            except (TypeError, ValueError):
                continue
            if lv != 2 or not (0 <= idx < len(batch)):
                continue
            row = batch[idx]
            out.append(self._item(row, cn=str(obj.get("cn") or "").strip(),
                                  lv=2, direction=str(obj.get("dir") or "neutral"),
                                  translated=True))
        return out

    @staticmethod
    def _is_macro(headline: str) -> bool:
        return bool(_FALLBACK_RE.search(headline or ""))

    def _fallback_item(self, row: dict) -> dict:
        return self._item(row, cn="", lv=2, direction="neutral", translated=False)

    @staticmethod
    def _item(row: dict, *, cn: str, lv: int, direction: str, translated: bool) -> dict:
        ts = row.get("created_at") or ""
        et_label = ""
        try:
            et_label = datetime.fromisoformat(
                ts.replace("Z", "+00:00")).astimezone(_ET).strftime("%H:%M")
        except ValueError:
            pass
        if direction not in ("bull", "bear", "neutral"):
            direction = "neutral"
        return {
            "ts": ts, "et": et_label,
            "cn": cn, "en": row.get("headline") or "",
            "lv": lv, "dir": direction, "translated": translated,
        }


GLOBAL_NEWS_FEED = NewsFeedService()


async def news_feed_refresh_loop(manager) -> None:
    """后台协程：每 60 秒拉一轮，有新的最高档消息就广播。

    照 `market_tide_refresh_loop` 的形状，由 `app/main.py` 的 lifespan 起停。
    """
    logger.info("[News] 快讯滚动服务启动（窗口 08:00-16:05 ET，只推最高档）")
    try:
        while True:
            try:
                fresh = await GLOBAL_NEWS_FEED.poll_once(datetime.now(_ET))
                if fresh:
                    await manager.broadcast({"type": "news", "data": fresh})
                    for item in fresh:
                        logger.info(f"[News] {item['et']} {item['dir']} "
                                    f"{item['cn'] or item['en'][:60]}")
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 —— 单轮异常绝不能停掉整个循环
                logger.warning(f"[News] 本轮异常（继续下一轮）: {e}")
            await asyncio.sleep(POLL_INTERVAL_S)
    finally:
        global _EXECUTOR
        if _EXECUTOR is not None:
            _EXECUTOR.shutdown(wait=False)
            _EXECUTOR = None
