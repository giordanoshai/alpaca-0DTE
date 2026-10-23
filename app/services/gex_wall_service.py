# -*- coding: utf-8 -*-
"""每标的距墙（GEX call_wall / put_wall / gamma_flip）轮询服务 —— 纯展示，不接任何交易判定。

数据源：UW REST `/api/stock/{ticker}/gex-levels`（无批量端点，N 只 = N 个请求）。

**为什么只推结构位绝对价、不推百分比**：这些位都是慢变量（一轮更新一次足够），
而现价每 tick 变。后端算好再推会让它滞后整整一个轮询周期；推绝对价、由前端用已有的
实时价（`feed_snapshot.py` 下发的 `price` 字段）每 tick 重算，数才是活的。两种做法请求成本相同。

**±GEX（`net_gex`）是例外，由后端算好推一个字符串**：它跟现价无关，是独立的一份数据
（`spot-exposures` 的 `gamma_per_one_percent_move_dir` 真值），不存在「后端算会滞后」
的问题，所以上面那条理由对它不适用。2026-08-17～08-21 曾让前端拿现价跟 `gamma_flip`
比大小推这个符号，2026-08-25 实测查明那是**口径错配**（flip 自带 `source="vol"`，
当时却拿它去对 oi 口径的净 Gamma），且推算法在 −GEX 一侧只有 53.5% 准 ≈ 抛硬币 ——
现已改为直读真值。它走**独立的小轮次**，见 `_refresh_net_gex`。

**解耦约束（勿破坏）**：
  1. 本模块对 `app.watcher` 零 import —— 标的列表由调用方注入 `tickers_provider`。
  2. 不进任何交易判定（validator / watchdog / order_gate / strategies 都不得引用本模块）。
     规则 7b 距墙闸门的教训：进了闸门却从未真正拦过单，最后留下一地死代码。
  3. 失败不外溢 —— 异常在本模块内消化，最坏情况是前端不显示墙位行。
  4. **线程不外溢** —— 阻塞的 HTTP 调用走本模块**专属**线程池，不碰 `asyncio.to_thread`
     的默认池（见 `_executor()` 的说明）。
"""
from __future__ import annotations

import asyncio
import functools
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from app.config import UW_API_KEY
from app.core import market_gate
from app.core.dailylogger import get_logger

logger = get_logger("tt.gex_wall")

# 模块级副本，便于测试 monkeypatch。**禁止 os.getenv 直读** ——
# config.py 统一定义 UW_API_KEY。
_UW_KEY = UW_API_KEY

# 轮询节拍。原定 10s（ADV 接口无请求限制），2026-08-15 首次实盘轮询后放宽到 30s，
# 2026-08-17 当天先改 15s、复盘中转站阵发性抽风的真实日志后又改回 30s——
# 抽风时是同一轮几乎全灭（25+/33 同时超时），不是「差一点点没赶上」，轮询更勤
# 只是更快把同一次抽风又打一遍，换不来更新鲜的数据，不如把节拍还给中转站喘气。
# 注意实际节拍是「上一轮跑完 + 再等 30s」，不是严格 30s 一轮。
_WALL_POLL_INTERVAL_S = 30.0
# 非盘中的重看间隔：闸关着时不必每 30s 就问一次
_CLOSED_RECHECK_S = 60.0
# 整轮硬超时：未完成的标的按失败处理（保留旧值 + stale），绝不让一轮拖进下一轮。
# 必须 < _WALL_POLL_INTERVAL_S，否则请求会在中转站上叠罗汉（有用例钉死）。
# 2026-08-17 用户拍板从 15s 降到 10s——轮询间隔当时同步降到 15s 才需要这一步，
# 后来间隔改回 30s，但 10s 这个值本身用户是单独确认过的偏好，不随间隔改动跟着弹回。
_WALL_ROUND_TIMEOUT_S = 10.0
# 传给 gex_levels 的缓存上限，必须小于轮询间隔，否则每轮都命中上一轮的缓存、墙位永不更新。
# 2026-08-17 随轮询间隔 30s→15s 等比例下调到 12s；间隔改回 30s 后 12s 仍然 < 30s、
# 不违反约束，维持不变（没有理由跟着间隔弹回 25s，缓存更短只会让墙位更新更及时）。
_GEX_MAX_AGE_S = 12.0
# ±GEX 小轮次的整轮超时与缓存上限。整轮上限跟距墙那一轮同款（10s）；
# 缓存上限必须 < 实际间隔（30s × _NET_GEX_EVERY_N_ROUNDS = 60s），否则每次都命中
# 上一轮的缓存、符号永不更新 —— 与 _GEX_MAX_AGE_S 同一个道理。
_NET_GEX_ROUND_TIMEOUT_S = 10.0
_NET_GEX_MAX_AGE_S = 50.0
# 单请求超时。3.0s 是 UwClient 的默认值，2026-08-15 实测扛不住 SOCKS 代理下的
# 中转站抖动（19/33 只读超时）；复用 Session 之后单只中位降到 0.14s，5s 当时是很宽的余量。
# 2026-08-17 实测推翻了「5s 够用」——同样 33 并发 / 16 线程条件下跑满 10 分钟：
# 走代理仍有约 9.3%（52/561）稳定超时率（且每隔几轮出现一次 60%~76% 的明显掉底）；
# 换成直连对比测出成功请求的延迟分布 p50=2844ms、p90=4516ms、p99=8344ms、
# 最大 11844ms——贴着 5s 超时线的请求占相当比例，5s 太紧。8.0s 覆盖了直连测出的
# p99（代理链路只会更快，留有更多余量）。2026-08-17 整轮超时随后又从 15s 降到 10s，
# 余量收窄到 2s——中转站阵发性抽风时（同轮 25+/33 只同时超时的实况已实测坐实），
# 33 只票 / 16 并发的第二批任务基本等不到自己单独超时就被整轮上限切掉，
# 会静默变成 stale 而不是各自打一条「拉取失败」日志，属于预期行为不是回归。
_HTTP_TIMEOUT_S = 8.0

# 并发上限。也是专属线程池的大小 —— 两者必须一致，否则要么排队、要么白开线程。
# 同时也是 UwClient 连接池的大小：pool_maxsize 小于并发数会让多出来的连接建了又扔。
_MAX_CONCURRENT_WORKERS = 16

# ── 没有 GEX 的标的 ────────────────────────────────────────────────────────
#
# 不是每只票都有 GEX：这个端点要求标的**有期权链且 UW 有覆盖**。
#
# 1) 靠符号就能判定的：`^` 开头是指数代码（`^VIX` 来自 config.py 的
#    MARKET_STATUS_TICKERS 与 pool_screener 的 CONTEXT_TICKERS，是故意放进池子的
#    大盘 context，不可交易也没有期权链）。这类不必先打一次请求再挨拒。
_SKIP_PREFIXES = ("^",)
#
# 2) 靠符号判不出、只能问了才知道的：源站按 ticker 拒绝时记下来，此后不再问。
#    **只认 422/404** —— 5xx/429/超时是中转站或网络的问题，是整站且临时的，
#    照那个拉黑等于抖动一次就把 33 只票全废掉且永不恢复（2026-08-15 21:00 实测
#    中转站整站 530 Argo Tunnel 断，若按失败拉黑，恢复后前端将永远空白）。
#    401/403 同理属于 key 的问题，也不是「这只票没有 GEX」。
_PERMANENT_REJECT_CODES = frozenset({404, 422})
_UNSUPPORTED: set[str] = set()

# 模块级快照。调用方（trading_ws._capture）只读，不得原地修改
# —— 与 MARKET_FEED.snapshot() 同约定。
_WALLS: dict[str, dict] = {}
_GENERATION = 0
# 轮次序号，每跑完一轮 +1。只用于决定本轮要不要跑 ±GEX 小轮次
# （见 _should_pull_net_gex）——**不能拿 _GENERATION 代替**，那个只在内容
# 真变了才涨，墙位连续几轮不动就再也轮不到 ±GEX。
_ROUND_SEQ = 0

_EXECUTOR: ThreadPoolExecutor | None = None


def _executor() -> ThreadPoolExecutor:
    """本模块**专属**的线程池（懒建单例）。

    **为什么不用 `asyncio.to_thread`**：它跑在事件循环的默认 executor 上，
    而那个池（本机 `cpu_count=24` → 28 槽）同时挂着 `app/routers/orders.py:101` 的
    **点击→提交**、`market_tide_service` 那个 `http_timeout=30.0` 的调用，以及
    market_feed / trading_state / option_candidates / order_sync。

    距墙每 10 秒要占最多 16 槽、每槽最长约 3s（`UwClient` 默认 http_timeout）。
    拿纯展示的墙位去挤下单的线程，方向完全错了 —— 下单延迟本来就在查
    （点击→提交实测 max 14.7s，券商侧 55-500ms 无辜）。开专池后共享池一根线程都不碰。

    ⚠️ 专池不受 `asyncio.run()` 的 `shutdown_default_executor()` 管辖，
    所以 `gex_wall_refresh_loop` 退出时要自己 shutdown。
    """
    global _EXECUTOR
    if _EXECUTOR is None:
        _EXECUTOR = ThreadPoolExecutor(
            max_workers=_MAX_CONCURRENT_WORKERS, thread_name_prefix="gexwall"
        )
    return _EXECUTOR


def snapshot() -> dict[str, dict]:
    """{ticker: {"call_wall": str|None, "put_wall": str|None, "gamma_flip": str|None, "ts": float, "stale": bool}}

    纯内存、非阻塞。trading_ws 的 _capture() 每 0.5s 调它，
    **绝不能在这里做 IO 或加锁等待**。
    """
    return _WALLS


def generation() -> int:
    """轮询轮次计数，每轮更新后 +1。trading_ws 拿它当变更 token，
    比深比较整个 dict 便宜。"""
    return _GENERATION


def _to_payload(levels: dict) -> dict | None:
    """`UwClient.gex_levels()` 的返回 → 前端要的三个结构位（Decimal 转 str）。

    2026-08-17 加 gamma_flip：接口本来就带这个字段，UwClient 也早在拉，只是之前没往下传。
    **它现在只是一条普通参考线**——曾拿它跟现价比大小推 ±GEX，2026-08-25 实测查明那是
    口径错配（详见文件头），±GEX 已改为直读 `spot-exposures` 的 dir 口径真值。

    **本函数只管墙位与 flip，不含 `net_gex`**：那个走独立的小轮次（`_refresh_net_gex`）、
    每 2 轮才拉一次，由 `merge_round` 合并进来。分开的原因见 `_refresh_net_gex` 的说明。

    三者都缺时返回 None：那等于没数据，不该占一个快照键位，否则前端会渲染出空行。
    """
    call = levels.get("call_wall")
    put = levels.get("put_wall")
    flip = levels.get("gamma_flip")
    if call is None and put is None and flip is None:
        return None
    return {
        "call_wall": None if call is None else str(call),
        "put_wall": None if put is None else str(put),
        "gamma_flip": None if flip is None else str(flip),
    }


# ±GEX 的轮次间隔：距墙 30s 一轮，这里每 2 轮 = 60s 拉一次。
# ±GEX 是慢变量（SPY 2026-08-24 全天翻 15 次，约 26 分钟一次），60s 足够；
# 而它单票 316KB（`limit` 参数实测无效、裁不掉），没必要跟墙位同频。
_NET_GEX_EVERY_N_ROUNDS = 2


def _should_pull_net_gex(round_seq: int) -> bool:
    """本轮要不要跑 ±GEX 小轮次。`round_seq` 从 0 起、每轮 +1。"""
    return round_seq % _NET_GEX_EVERY_N_ROUNDS == 0


def _worker_count(n: int) -> int:
    """并发度随池子大小自适应。

    下限 4：小池子也别串行，代理空载单程 1.6s，串行很快就把 10s 窗口吃完。
    上限 16：中转站是和隔壁回填共用的，不能无限开；也是专属线程池的大小。
    """
    return min(_MAX_CONCURRENT_WORKERS, max(4, n))


def merge_round(previous: dict, results: dict, now: float,
                net_gex: dict | None = None) -> dict:
    """把一轮拉取结果并进上一轮快照。

    `results` 的键就是**本轮请求的标的集**，值为 `_to_payload()` 的结果或 None（失败/超时）。
    因此从盯盘池移除的标的会自动从快照里消失，不需要额外的清理逻辑。

    失败的标的保留上一轮的值并标 `stale=True`（前端灰显）；`ts` 停在上次成功那一刻，
    这样前端能看出数据有多旧。从未成功过的标的直接不出现在快照里。

    `net_gex`：本轮 ±GEX 小轮次拉到的 `{ticker: "pos"/"neg"/None}`。传 None 表示
    本轮没跑那个轮次（它每 2 轮才跑一次，见 `_should_pull_net_gex`），所有票沿用
    上一轮的符号 —— 不能因为本轮没拉就把它抹成空。

    **「不在 dict 里」与「在 dict 里但值是 None」是两回事**：前者是这只票本轮拉失败，
    沿用旧值；后者是拉到了、这只票就是没有 ±GEX，要写 None 覆盖旧值 ——
    否则一只票会永远挂着几小时前的符号。
    """
    merged: dict[str, dict] = {}
    for ticker, payload in results.items():
        if payload is not None:
            if net_gex is not None and ticker in net_gex:
                gex = net_gex[ticker]
            else:
                gex = (previous.get(ticker) or {}).get("net_gex")
            merged[ticker] = {**payload, "net_gex": gex, "ts": now, "stale": False}
        elif ticker in previous:
            # 整只票 stale：墙位与 ±GEX 是同一份快照的两半，不该一半新一半旧，
            # 所以这里连 net_gex 一起留在旧值上，哪怕本轮 ±GEX 其实拉到了。
            merged[ticker] = {**previous[ticker], "stale": True}
        # else: 从未成功过 —— 宁可不渲染，也不显示空墙位
    return merged


def _content_key(walls: dict) -> tuple:
    """比对用的内容指纹：**只看墙位与 stale，不看 ts**。

    `ts` 每轮都是 `time.time()`、必然在动，让它参与比对等于永远判定为「变了」——
    那样 generation 每轮都涨，trading_ws 就会把整个 walls dict 推给每个客户端，
    每 10 秒一次、通宵不停，即使一个数都没动。
    """
    return tuple(sorted(
        (ticker, w.get("call_wall"), w.get("put_wall"), w.get("gamma_flip"),
         w.get("net_gex"), w.get("stale"))
        for ticker, w in walls.items()
    ))


def is_supported(ticker: str) -> bool:
    """这只票值不值得再问一次 UW。见上面 `_SKIP_PREFIXES` / `_UNSUPPORTED` 的说明。"""
    t = ticker.upper()
    return not t.startswith(_SKIP_PREFIXES) and t not in _UNSUPPORTED


def _mark_unsupported(ticker: str, code: int) -> None:
    """源站按 ticker 拒绝 → 记下来，此后整轮跳过。只写一次日志。"""
    t = ticker.upper()
    if t not in _UNSUPPORTED:
        _UNSUPPORTED.add(t)
        logger.warning(
            f"[GexWall] {ticker} 没有 GEX（HTTP {code}），后续轮次不再请求"
        )


async def _fetch_one(client, ticker: str) -> tuple[dict | None, str | None]:
    """拉一只票。任何异常都吞成 (None, 错误摘要) —— 调用方据此保留旧值并标 stale。

    2026-08-17 以前这里逐票各打一条 WARNING；中转站阵发性抽风时一轮能同时炸
    25+/33 只（[[uw-relay-latency-ack-watchdog]] 已实测坐实的现象），日志瞬间
    刷屏几十行还都是同一个原因。改成把错误摘要带回去，由 refresh_once 按轮
    归并成一条汇总日志，不在这里打印。
    """
    loop = asyncio.get_running_loop()
    try:
        # run_in_executor 不收 kwargs，故用 partial 把 max_age_s 绑上。
        levels = await loop.run_in_executor(
            _executor(),
            functools.partial(client.gex_levels, ticker, max_age_s=_GEX_MAX_AGE_S),
        )
    except Exception as e:  # noqa: BLE001 —— 单只失败不得拖垮整轮
        code = getattr(e, "status_code", None)
        if code in _PERMANENT_REJECT_CODES:
            _mark_unsupported(ticker, code)
            return None, None
        return None, str(e)
    return _to_payload(levels), None


async def _fetch_net_gex_one(client, ticker: str) -> tuple[str, str | None] | None:
    """拉一只票的 ±GEX 符号。失败返回 None —— 调用方据此**不把它放进 dict**，
    于是 merge_round 会让这只票继续用上一轮的符号（而不是被抹成空）。

    注意 `spot_gamma_dir` 自己已经区分了两种情况：网络失败抛异常（走这里的
    except，返回 None = 沿用旧值），拿到了但没有符号则正常返回 None
    （走下面的 return，进 dict 且值为 None = 覆盖旧值）。
    """
    loop = asyncio.get_running_loop()
    try:
        sign = await loop.run_in_executor(
            _executor(),
            functools.partial(client.spot_gamma_dir, ticker, max_age_s=_NET_GEX_MAX_AGE_S),
        )
    except Exception:  # noqa: BLE001 —— 单只失败不得拖垮整轮
        return None
    return ticker, sign


async def _refresh_net_gex(tickers: list[str], client) -> dict[str, str | None]:
    """±GEX 小轮次：**排在距墙那一轮之后单独跑**，不跟它抢同一个窗口。

    为什么不并进 _fetch_one 里一票两请求：专属线程池只有
    `_MAX_CONCURRENT_WORKERS`(16) 条线程，每票要两条就只能同时跑 8 只票，
    33 只要 4 批 × 单请求 8s = 32s，远超 `_WALL_ROUND_TIMEOUT_S`(10s)，
    结果是每轮大批标 stale。分开跑则两轮各自 10s 上限、合计 ≤20s，
    仍小于 30s 的轮询间隔（何况节拍本来就是「上一轮跑完再等 30s」，不会叠罗汉）。

    与距墙那一轮同款的容错：整轮硬超时，未完成的直接丢（不进 dict = 沿用旧值）。
    """
    if not tickers:
        return {}
    sem = asyncio.Semaphore(_worker_count(len(tickers)))

    async def _guarded(t: str):
        async with sem:
            return await _fetch_net_gex_one(client, t)

    tasks = [asyncio.create_task(_guarded(t)) for t in tickers]
    done, pending = await asyncio.wait(tasks, timeout=_NET_GEX_ROUND_TIMEOUT_S)
    for task in pending:
        task.cancel()
    if pending:
        logger.warning(
            f"[GexWall] ±GEX 轮次超时 {_NET_GEX_ROUND_TIMEOUT_S}s，"
            f"{len(pending)}/{len(tickers)} 只未完成（保留上一轮的符号）"
        )
    out: dict[str, str | None] = {}
    for task in done:
        if task.cancelled():
            continue
        try:
            r = task.result()
        except Exception:  # noqa: BLE001 —— _fetch_net_gex_one 已兜底，再防一层
            continue
        if r is not None:
            out[r[0]] = r[1]
    return out


async def refresh_once(tickers_provider: Callable[[], list[str]], client) -> None:
    """跑一轮：拉取 → 合并 → 更新模块级快照与 generation。

    整轮设 `_WALL_ROUND_TIMEOUT_S` 硬超时。用 `asyncio.wait` 而不是
    `wait_for(gather(...))`：后者超时会连已完成的结果一起取消掉，
    一只挂住的票就能让整轮颗粒无收。
    """
    global _WALLS, _GENERATION, _ROUND_SEQ

    # 没配 key 时整轮跳过：否则每 10s 会为每只票各抛一次 UwUnavailable，
    # 32 只 × 6 轮/分钟纯属刷屏，而信息量只有"没配 key"这一条
    # —— 那条已经在 loop 启动时记过 ERROR 了。
    if not _UW_KEY:
        return

    # 先筛掉没有 GEX 的：既省一个并发槽，也省一条每轮都来的 WARNING。
    # 筛在 results 之前 —— results 的键就是快照的键集，被筛掉的票会自动从
    # 快照里消失（merge_round 的约定），前端也就不会为它渲染墙位行。
    tickers = [t for t in (tickers_provider() or []) if is_supported(t)]
    if not tickers:
        return

    sem = asyncio.Semaphore(_worker_count(len(tickers)))

    async def _guarded(t: str):
        async with sem:
            return await _fetch_one(client, t)

    tasks = {asyncio.create_task(_guarded(t)): t for t in tickers}
    done, pending = await asyncio.wait(
        tasks.keys(), timeout=_WALL_ROUND_TIMEOUT_S
    )
    for task in pending:
        task.cancel()
    if pending:
        logger.warning(
            f"[GexWall] 整轮超时 {_WALL_ROUND_TIMEOUT_S}s，"
            f"{len(pending)}/{len(tickers)} 只未完成（保留上一轮的值）"
        )

    results: dict[str, dict | None] = {}
    failed: list[str] = []
    sample_error: str | None = None
    for task, ticker in tasks.items():
        if task in done and not task.cancelled():
            try:
                payload, error = task.result()
            except Exception:  # noqa: BLE001 —— _fetch_one 已兜底，这里防御性再兜一层
                payload, error = None, "task 异常"
            results[ticker] = payload
            if error is not None:
                failed.append(ticker)
                sample_error = sample_error or error
        else:
            results[ticker] = None

    # 归并成一条汇总日志，不逐票各打一行——中转站阵发性抽风时一轮能同时炸
    # 25+/33 只，逐票打行是刷屏（2026-08-17 用户反馈）。只带一条错误样例
    # （抽风时几十只报的通常是同一个原因），要看每只的完整异常去 DEBUG 级找。
    if failed:
        extra = f"（示例: {sample_error}）" if sample_error else ""
        logger.warning(
            f"[GexWall] {len(failed)}/{len(tickers)} 只拉取失败（保留上一轮的值）: "
            f"{', '.join(failed)}{extra}"
        )

    # ±GEX 小轮次：排在距墙之后，两者不共用同一个 10s 窗口（原因见 _refresh_net_gex）。
    # 每 _NET_GEX_EVERY_N_ROUNDS 轮才跑一次；不跑的轮次传 None，让 merge_round
    # 沿用上一轮的符号。
    net_gex: dict[str, str | None] | None = None
    if _should_pull_net_gex(_ROUND_SEQ):
        net_gex = await _refresh_net_gex(tickers, client)
    _ROUND_SEQ += 1

    merged = merge_round(_WALLS, results, time.time(), net_gex)
    # 只有内容真的变了才动 generation —— 它是 trading_ws 的变更 token，
    # 无条件 +1 等于每轮强推一帧。`_WALLS` 照常换成新的（ts 更新），
    # 不涨 generation 时 hub 继续用它手上那份，两边内容一致。
    if _content_key(merged) != _content_key(_WALLS):
        _GENERATION += 1
    _WALLS = merged


async def gex_wall_refresh_loop(tickers_provider: Callable[[], list[str]]) -> None:
    """后台轮询主循环。由 main.py 的 lifespan 起 task、退出时 cancel。

    没有 on_update 回调 —— trading_ws 是 pull 模式，每 0.5s 自己来拉 snapshot()。

    逻辑都在 refresh_once 里，这里只管节拍与兜底：任何异常都不得让 task 静默死掉，
    否则墙位会永远停在最后一次成功的值上而没人发现。
    """
    from app.integrations.uw import UwClient

    if not _UW_KEY:
        # 记一次就够：refresh_once 会整轮跳过，不会每 10s 再刷一遍。
        logger.error("[GexWall] 缺少 UW_API_KEY，距墙功能不可用")

    # 单实例常驻 —— 复用它内部那条 Session（连接池），否则每轮 33 次
    # SOCKS5+TLS 握手，单只从 0.14s 涨到 0.74s。pool_size 必须 ≥ 并发数。
    client = UwClient(http_timeout=_HTTP_TIMEOUT_S, pool_size=_MAX_CONCURRENT_WORKERS)
    logger.info(
        f"[GexWall] 距墙轮询启动，间隔 {_WALL_POLL_INTERVAL_S}s、"
        f"整轮上限 {_WALL_ROUND_TIMEOUT_S}s、单请求 {_HTTP_TIMEOUT_S}s"
    )
    try:
        while True:
            # 非盘中不拉：墙位保持在收盘时的值（前端会标「休市」）。UW 这一路
            # 每轮 13 次请求，是非盘中最大的一笔无谓开销之一。
            if not market_gate.data_window_open():
                await asyncio.sleep(_CLOSED_RECHECK_S)
                continue
            try:
                await refresh_once(tickers_provider, client)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.error(f"[GexWall] 轮询异常（下轮继续）: {e}")
            await asyncio.sleep(_WALL_POLL_INTERVAL_S)
    finally:
        # 专池不归 asyncio.run() 的 shutdown_default_executor() 管，得自己收。
        # wait=False：在途的 HTTP 最多 3s 自己结束，不拖住关停；
        # cancel_futures=True：还没起跑的直接丢掉。
        global _EXECUTOR
        if _EXECUTOR is not None:
            _EXECUTOR.shutdown(wait=False, cancel_futures=True)
            _EXECUTOR = None

