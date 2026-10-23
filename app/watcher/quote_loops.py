"""四路实时行情 quote loop（Yahoo / Massive / UW / Mock）—— 2026-07-24 从 watcher_manager 拆出。

QuoteLoopsMixin 供 WatcherManager 继承：只含数据源接入循环；共享的
_ingest_tick / _fill_gaps / _watchers / _running 状态仍在 WatcherManager 本体。
（拆分原因：watcher_manager.py 超过 CLAUDE.md 单文件 800 行红线）
"""
import asyncio
from datetime import datetime
import os
from typing import Optional

from app.config import UW_API_KEY, env
from app.core import market_gate
from app.core.dailylogger import get_logger

logger = get_logger("tt.watcher")

# 盯盘池增删后多久对账一次订阅集合
_UW_POOL_RECONCILE_S = 10.0

# 非盘中每隔多久重新看一眼要不要开工。与 market_gate 的时钟缓存同量级即可，
# 早几十秒晚几十秒都无所谓——真正的开盘时刻由盘前 5 分钟的预热窗口兜着。
_CLOSED_RECHECK_S = 30.0

# 盘中检查「窗口是否已关」的间隔。收盘后必须主动把 WS 拆掉：Yahoo 盘后照样推
# 报价，只在外层循环挂闸的话这些 tick 会被继续吃进来，闸等于没装。
_GATE_RECHECK_S = 30.0


def _uw_data_source() -> str:
    """当前行情数据源。独立成函数是为了让测试能 monkeypatch 掉它——
    `env()` 读的是进程环境与 .env 文件，测试里不该去改那两样。"""
    return env("DATA_SOURCE", "yahoo").lower()


import yfinance as yf  # noqa: F401,E402 — Yahoo 代理路由唯一入口，删掉会静默破坏 Yahoo 实时源
from yfinance.live import AsyncWebSocket
from yfinance.config import YfConfig

# yfinance AsyncWebSocket.listen() 内部重连有 bug：断线后调用 self._connect()，
# 但 _connect() 只在 self._ws is None 时才真正重连，断线后 self._ws 不会被置 None，
# 导致重连是空操作，随即在同一条死连接上再次报错，陷入 3 秒一次的死循环且用户毫无感知。
# 默认 hide_exceptions=True 会把异常吞掉，不往外抛，导致下面 _yf_quote_loop 里
# 已写好的“关闭旧连接 + fill_gap 补数据 + 用全新 AsyncWebSocket 重连”逻辑永远不会触发。
# 关掉吞异常，让错误抛出来，走我们自己的重连分支。
YfConfig.debug.hide_exceptions = False


def _mock_extract_ticks(data) -> list[tuple[str, float, int, datetime]]:
    """回放服务器帧 → [(ticker, price, tick_vol, virtual_time)]。

    脏帧/缺字段一律跳过并返回空列表：一个坏帧不得让整条 WS 循环崩掉；
    缺 price 时更不能补 0；缺虚拟时间也不能回落墙钟，否则 60 倍回放会把约
    60 根历史分钟线压进同一根 BarBuilder 分钟线。
    """
    import json as _json
    try:
        msg = _json.loads(data)
    except Exception:  # noqa: BLE001
        return []
    if not isinstance(msg, dict):
        return []
    sym = msg.get("sym")
    price = msg.get("price")
    timestamp = msg.get("timestamp")
    if not sym or price is None or not timestamp:
        return []
    try:
        price_value = float(price)
        if price_value <= 0:
            return []
        virtual_time = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
        if virtual_time.tzinfo is None:
            return []
        return [(
            str(sym).upper(), price_value, int(msg.get("volume") or 0), virtual_time,
        )]
    except (TypeError, ValueError):
        return []


class QuoteLoopsMixin:
    """数据源实时接入循环：Yahoo / Massive / UW / Mock 四路，由 WatcherManager.start() 按 DATA_SOURCE 选路。"""

    #: 上一拍看到的行情窗口状态；None = 本进程还没看过（启动第一拍）。
    _window_was_open: Optional[bool] = None

    async def _await_market_window(self, source_name: str) -> bool:
        """行情窗口闸门 +「关→开」跳变时补缺口。返回 True 表示现在可以开工。

        非盘中整个行情循环挂起（服务 2026-09-02 起 24 小时常驻，见
        `app/core/market_gate.py`），期间一条 tick 都不进来。窗口重新开门时缓存里
        最后一根 bar 还停在上一次收盘，中间那一段（跨夜、周末、节假日）**谁都没补**
        —— `_fill_gaps` 原先只挂在 WebSocket 报错那条分支上，而窗口切换是正常流程、
        不报错。2026-09-03 线上实测：SPY 缓存 09-02 19:59 → 09-03 09:25 空了 806
        分钟，今天盘前一根没有，于是开盘头几十分钟的 EMA/MACD/ATR/效率比全算在
        239 根昨天盘后的零成交量 bar 上，而入场判据看的就是多周期均线同向。

        只在「关→开」那一次跳变补：进程启动时窗口本来就开着不算跳变（启动预热
        自己会补），否则盘中每一拍都补一次会把行情源打爆。
        """
        open_now = market_gate.data_window_open()
        was_open = self._window_was_open
        self._window_was_open = open_now
        if not open_now:
            await asyncio.sleep(_CLOSED_RECHECK_S)
            return False
        if was_open is False:
            await self._fill_gaps(source_name, reason="行情窗口开门")
        return True

    async def _run_json_ws_loop(self, name, build_uri, on_connect, extract_ticks,
                                use_proxy: bool = True) -> None:
        """Massive/UW 共用的 websockets 主循环：连接→订阅→recv→逐 tick 入库，含断线补齐重连。

        build_uri() -> str 生成连接 URI；on_connect(ws, tickers) 完成鉴权/订阅；
        extract_ticks(data) -> Iterable[(ticker, price, tick_vol[, timestamp])]
        做源特定解析；只有加速历史回放需要显式时间戳。
        """
        import websockets
        while self._running:
            if not await self._await_market_window(name):
                continue

            all_tickers = [t for t in self._watchers.keys() if not t.startswith("^")]
            if not all_tickers:
                await asyncio.sleep(5)
                continue

            try:
                uri = build_uri()
                # 本地 mock 服务器（127.0.0.1）走代理必然连不上，故留一个关闭开关
                proxy = self._get_proxy_url() if use_proxy else None
                if proxy:
                    logger.info(f"[{name}] Connecting via proxy {proxy}")

                async with websockets.connect(uri, proxy=proxy) as ws:
                    await on_connect(ws, all_tickers)
                    while self._running and market_gate.data_window_open():
                        # 限时 recv：否则收盘后没有新消息时会一直阻塞在这里，
                        # 上面那个窗口判断永远轮不到执行，连接挂一整夜。
                        try:
                            data = await asyncio.wait_for(ws.recv(),
                                                          timeout=_GATE_RECHECK_S)
                        except asyncio.TimeoutError:
                            continue
                        for tick in extract_ticks(data):
                            ticker, price, tick_vol, *timestamp = tick
                            await self._ingest_tick(
                                ticker, price, tick_vol,
                                timestamp=timestamp[0] if timestamp else None,
                            )
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[{name}] WS error: {e}")
                await self._fill_gaps(name)
                await asyncio.sleep(3)

    async def _yf_quote_loop(self) -> None:
        """Yahoo Finance WebSocket 实时流 — 覆盖所有 ticker"""
        _vol_tracker: dict[str, dict] = {}

        while self._running:
            if not await self._await_market_window("Yahoo"):
                continue

            all_tickers = [t for t in self._watchers.keys()]
            if not all_tickers:
                await asyncio.sleep(5)
                continue

            ws = AsyncWebSocket(verbose=False)

            async def on_quote(message: dict):
                ticker = message.get("id")
                price  = message.get("price")
                if not ticker or price is None:
                    return
                watcher = self._watchers.get(ticker)
                if not watcher:
                    return

                # 增量 volume：AsyncWebSocket 用 preserving_proto_field_name=True 解码，
                # 字段名是 snake_case 的 day_volume，不是 dayVolume（旧写法恒为 0）。
                day_vol    = int(message.get("day_volume") or 0)
                quote_time = str(message.get("time", ""))
                if ticker not in _vol_tracker:
                    # 该 ticker 自进程/协程重启后的第一条报价：只建立基线，不计入本笔量。
                    # 否则会把当日累计总量当成这一笔的成交量，在每次重连/热重载后
                    # 对所有 ticker 同时炸出一根几十上百倍的假成交量尖峰。
                    tick_vol = 0
                else:
                    last = _vol_tracker[ticker]
                    if last.get("time") == quote_time:
                        # 同一个 quote_time 内的重复推送（无新成交），不重复计数
                        tick_vol = 0
                    else:
                        # 新的 quote：增量 = 本次日累计量 - 上次日累计量
                        tick_vol = max(0, day_vol - last.get("vol", 0))
                _vol_tracker[ticker] = {"vol": day_vol, "time": quote_time}

                await self._ingest_tick(ticker, float(price), tick_vol)

            try:
                await ws.subscribe(all_tickers)
                # listen() 会一直阻塞到断线为止，没法在里面插窗口判断。收盘后
                # Yahoo 照样推盘后报价，不主动拆连接的话这些 tick 会继续被
                # on_quote 吃进来 —— 闸就等于没装。故起成任务，收盘时取消它。
                listen_task = asyncio.create_task(ws.listen(on_quote))
                try:
                    while self._running and market_gate.data_window_open():
                        done, _ = await asyncio.wait(
                            {listen_task}, timeout=_GATE_RECHECK_S)
                        if listen_task in done:
                            break  # listen 自己结束了（断线）
                finally:
                    if not listen_task.done():
                        listen_task.cancel()
                        await asyncio.gather(listen_task, return_exceptions=True)
                await ws.close()

                # listen 是断线退出的话，必须把异常放出来交给下面既有的
                # except Exception 处理（记日志 → _fill_gaps 补缺口 → 退避 3 秒）。
                # 吞掉的话外层会立刻重连，变成紧密重连循环。
                if listen_task.done() and not listen_task.cancelled():
                    listen_exc = listen_task.exception()
                    if listen_exc is not None:
                        raise listen_exc
            except asyncio.CancelledError:
                await ws.close()
                break
            except Exception as e:
                logger.error(f"Yahoo WS error: {e}")
                try:
                    await ws.close()
                except Exception:
                    pass
                await self._fill_gaps("WS")
                await asyncio.sleep(3)

    def _get_proxy_url(self) -> Optional[str]:
        """Massive/UW WebSocket 用的 HTTP 代理地址；没配代理时返回 None（直连）。

        取 `config.HTTP_PROXY`（`.env` 优先）而不是读 `os.environ`：Alpaca 的
        `apply_proxy()` 会把全进程 HTTP_PROXY 覆盖成 `socks5h://…`，旧写法拿到它后
        还要再套一层前缀，拼出连不上的 `http://socks5h://host:port`（2026-09-01 修）。
        """
        from app.config import HTTP_PROXY, normalize_http_proxy
        return normalize_http_proxy(HTTP_PROXY) or None

    async def _massive_ws_quote_loop(self) -> None:
        """Massive WebSocket 实时流"""
        import json

        api_key = os.getenv("MASSIVE_API_KEY")
        if not api_key:
            logger.warning("[Massive] 缺少 MASSIVE_API_KEY")
            return

        async def on_connect(ws, tickers):
            await ws.send(json.dumps({"action": "auth", "params": api_key}))
            logger.info(f"[Massive] Auth: {await ws.recv()}")
            params = ",".join([f"T.{t}" for t in tickers])
            await ws.send(json.dumps({"action": "subscribe", "params": params}))
            logger.info(f"[Massive] Sub: {await ws.recv()}")

        def extract_ticks(data):
            for message in json.loads(data):
                if message.get("ev") == "T":
                    yield (message.get("sym"),
                           float(message.get("p")),
                           int(message.get("s", message.get("v", 0))))

        await self._run_json_ws_loop(
            "Massive", lambda: "wss://socket.massive.com/stocks", on_connect, extract_ticks)

    async def _mock_ws_quote_loop(self) -> None:
        """本地回放服务器实时流（DATA_SOURCE=mock）—— 盘外跑全链路测试用。

        协议与 Massive/UW 两路一致，故直接复用 _run_json_ws_loop。
        走 127.0.0.1，必须关掉代理（use_proxy=False）。
        """
        import json

        uri = os.getenv("MOCK_STREAM_URL", "ws://127.0.0.1:9000/sim/stream")

        async def on_connect(ws, tickers):
            await ws.send(json.dumps({"action": "subscribe", "symbols": tickers}))
            logger.info(f"[Mock] 已订阅 {len(tickers)} 只标的：{uri}")

        await self._run_json_ws_loop(
            "Mock", lambda: uri, on_connect, _mock_extract_ticks, use_proxy=False)

    async def _uw_price_channel(self, ticker: str) -> None:
        """订阅单个标的的 `price:{ticker}` 频道，把累计成交量差分成单笔量后入库。

        payload 实测形状（2026-08-05 盘中）：`{"close":"773.85","time":1785941525000,"vol":12720002}`
        —— 约每秒一条；`close` 是**字符串**价格；`time` 是 epoch **毫秒**（本路不用，
        watcher 走自己的时钟）；**payload 里没有 ticker**，只能靠频道名带（这里由闭包给）。
        """
        from app.integrations.uw_ws import subscribe

        last_vol: int | None = None
        async for payload in subscribe(f"price:{ticker}"):
            try:
                price = float(payload["close"])
                vol = int(payload["vol"])
            except (KeyError, TypeError, ValueError):
                logger.warning(f"[UW] price:{ticker} 报文字段异常，跳过: {payload!r}")
                continue
            if price <= 0:
                continue

            # `vol` 是**当日累计成交量**（2026-08-05 实测 SPY/AAPL 各 30+ 条全程单调不减，
            # 逐条增量中位数仅 1.6k~2.1k），必须差分才是单笔量。旧实现直接把它当单笔量
            # 传给 _ingest_tick，会把上千万股灌进量能计算——而且连得上、跑得动，只是数全错。
            if last_vol is None:
                tick_vol = 0            # 首条没有增量基准，只记价不记量
            elif vol >= last_vol:
                tick_vol = vol - last_vol
            else:
                tick_vol = vol          # 累计量回退 = 跨交易日重置
            last_vol = vol

            await self._ingest_tick(ticker, price, tick_vol)

    async def _uw_ws_quote_loop(self) -> None:
        """Unusual Whales 中转站实时正股行情（channel = `price:{ticker}`）。

        走 `app/integrations/uw_ws.py` 的 `UwWsPool` 路由到 `ticker` 组：该组按用途
        单独占一条连接，有自己独立的 `MAX_CHANNELS` 条频道预算，`market_tide` 走
        `global` 组，不再与本路挤占同一份预算；ticket 申请、代理、退避重连、断线
        重订阅也都在那一层做好了。

        ⚠️ **容量约束**：`price` 频道是「一个标的一条」，本路能盯多少只取决于 UW 连接池
        `ticker` 组的剩余容量（`uw_ws.remaining_capacity()`）——按当前硬顶换算约 **72 只**
        （8 条连接上限，扣掉 flood / global 各 1 条，余 6 条 × 12）。盯盘池若超出，
        **直接拒绝启动**并打 ERROR——绝不静默只订前 N 只，那样看着在跑、实际大部分标的
        没有行情，比不跑更危险。

        容量数字**不写死在本模块**：向连接池查询，池子是唯一权威。以后硬顶或分组怎么变
        都不用回来改这里。
        """
        from app.integrations.uw_ws import GROUP_TICKER, remaining_capacity

        if not UW_API_KEY:
            logger.error("[UW] 缺少 UW_API_KEY，UW 实时行情不可用")
            return

        tasks: dict[str, asyncio.Task] = {}
        warned_over_budget = False

        try:
            while self._running:
                desired = {t for t in self._watchers.keys() if not t.startswith("^")}
                budget = remaining_capacity(GROUP_TICKER)

                if len(desired) > budget:
                    if not warned_over_budget:
                        logger.error(
                            f"[UW] 盯盘池 {len(desired)} 只，超出 UW 连接池 ticker 组"
                            f"当前可用容量 {budget} 只。UW 的 price 频道是一标的一条，"
                            f"覆盖不了这个池子，本路拒绝启动。请改用 "
                            f"DATA_SOURCE=les/yahoo，或把盯盘池缩到 {budget} 只以内。"
                        )
                        warned_over_budget = True
                    for tk in list(tasks):
                        tasks.pop(tk).cancel()
                    await asyncio.sleep(_UW_POOL_RECONCILE_S)
                    continue

                warned_over_budget = False

                # 池子随 add_ticker/remove_ticker 变动，这里定期对账增删订阅任务；
                # 取消任务会关掉 async generator，触发 uw_ws 侧的 leave 把预算还回去。
                for tk in desired - tasks.keys():
                    logger.info(f"[UW] 订阅 price:{tk}")
                    tasks[tk] = asyncio.create_task(self._uw_price_channel(tk))
                for tk in tasks.keys() - desired:
                    logger.info(f"[UW] 退订 price:{tk}")
                    tasks.pop(tk).cancel()
                for tk, task in list(tasks.items()):
                    if task.done():
                        del tasks[tk]

                await asyncio.sleep(_UW_POOL_RECONCILE_S)
        finally:
            for task in tasks.values():
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks.values(), return_exceptions=True)
