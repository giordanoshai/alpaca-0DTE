"""Unusual Whales 外部 API 客户端。

UW 是外部股票数据 API：必须走 .env 代理（CLAUDE.md 硬性要求，HTTP_PROXY）。
**2026-08-17 曾短暂改直连又改回**：两次各跑满 10 分钟、同参数（33 并发标的、
16 线程、5s 单请求超时）的真实对照——成功率几乎打平（代理 90.7% vs 直连
90.3%，差距在噪声内，说明超时根子在中转站本身扛不住并发、换传输层治不好），
但**直连成功请求延迟明显更高**（中位数 2844ms、p90 4516ms，贴近 5s 超时线；
代理侧单轮 33 并发多在 5~8s 内跑完，明显更快）。代理留的余量更大，改回代理。
所有失败（无 key / 超时 / 非 200 / 坏 JSON / 空数据）统一抛 UwUnavailable，
调用侧（datasource 组装层）捕获后回退 ATM 近似 —— UW 不得成为下单链路单点。
回放标定脚本用 raw() 直调历史端点（带 date 参数）。
"""
from __future__ import annotations

import time
from decimal import Decimal, InvalidOperation
import requests

from app.config import HTTP_PROXY, SOCKS5_PROXY, UW_API_KEY, UW_ENDPOINT


class UwUnavailable(Exception):
    """UW 不可用/数据不可信 —— 调用侧回退 ATM 近似。

    `status_code` 是 HTTP 状态码，**只有源站真的回了响应时才有值**；
    超时、连接失败、坏 JSON 一律是 None。

    调用侧靠它区分两类完全不同的失败（2026-08-15 距墙事故的教训）：
      · 422/404 —— 源站按 ticker 拒绝（`^VIX` 没有期权链），永久，重试无意义
      · 5xx / 429 / None —— 中转站或网络的问题，临时，下一轮必须照常重试
    混为一谈会各坏一头：要么给死票每 10s 刷一次屏，要么中转站抖一下就把整池永久废掉。
    """

    def __init__(self, message: str = "", *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def proxy_url(raw: str | None) -> str | None:
    value = (raw or "").strip()
    if not value:
        return None
    if "://" not in value:
        # 无协议头一律提升为 SOCKS5h（将域名推至远程服务器断开本机 11001 DNS 解析痛点）
        return f"socks5h://{value}"
    if value.startswith("socks5://"):
        return value.replace("socks5://", "socks5h://", 1)
    if value == "http://127.0.0.1:1080":
        return "socks5h://127.0.0.1:1080"
    return value


class UwClient:
    def __init__(self, base_url: str | None = None, api_key: str | None = None,
                 proxy: str | None = None, cache_ttl: float = 60.0, http_timeout: float = 3.0,
                 pool_size: int = 16):
        self._base = (base_url if base_url is not None else UW_ENDPOINT).rstrip("/")
        self._key = api_key if api_key is not None else UW_API_KEY
        raw_proxy = proxy if proxy is not None else (SOCKS5_PROXY or HTTP_PROXY)
        self._proxy = proxy_url(raw_proxy)
        self._ttl = cache_ttl
        self._timeout = http_timeout
        self._pool_size = pool_size
        self._cache: dict = {}
        self._session: requests.Session | None = None

    def _get_session(self) -> requests.Session:
        """懒建、**整个 client 复用一条**的 Session。

        **为什么必须复用**（2026-08-15 实测，33 只票打中转站）：
        模块级 `requests.get` 每次都新建连接，单只中位 **0.74s**；复用之后除首个
        请求外单只中位 **0.14s** —— 差的 0.6s 全是 SOCKS5 + TLS 握手。距墙服务
        33 只票、每轮全部重新握手、单只预算只有 3s，当天第一轮 0/33 成功
        （17 只读超时 + 16 只整轮超时）就是这么来的。

        线程安全：urllib3 的连接池本身是线程安全的，requests 的 cookiejar 内部
        带锁，而我们**从不改 session 级的 headers/params**（每次请求单独传）。
        因此多线程并发 `.get()` 是安全的 —— 距墙服务正是 16 线程共用一个 client。
        `pool_maxsize` 必须 ≥ 并发数，否则多出来的连接会被建了又扔，等于没复用。
        """
        if self._session is None:
            session = requests.Session()
            adapter = requests.adapters.HTTPAdapter(
                pool_connections=self._pool_size, pool_maxsize=self._pool_size
            )
            session.mount("https://", adapter)
            session.mount("http://", adapter)
            self._session = session
        return self._session

    def _get_json(self, path: str, params: dict | None = None) -> dict:
        url = self._base + path
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._key}",
            "UW-CLIENT-API-ID": "100001",
            # UW 前置 Cloudflare 会按 UA 签名拦掉纯底层程序启动请求（error 1010）
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) tt-trading/1.0",
        }
        proxies = {"http": self._proxy, "https": self._proxy} if self._proxy else None
        response = self._get_session().get(
            url, params=params, headers=headers, proxies=proxies, timeout=self._timeout)
        # 不用 raise_for_status()：它抛的 HTTPError 把状态码埋在 e.response 里，
        # 外层统一 `except Exception` 一包就丢了。调用侧要靠状态码区分
        # 「这只票不行」和「中转站挂了」，所以这里直接带着码抛。
        if response.status_code >= 400:
            raise UwUnavailable(
                f"HTTP {response.status_code}: {response.text[:200]}",
                status_code=response.status_code,
            )
        return response.json()

    def raw(self, path_qs: str) -> dict:
        """带鉴权+代理的裸 GET（离线回放脚本用）。失败抛 UwUnavailable。"""
        if not self._key:
            raise UwUnavailable("缺少 UW_API_KEY")
        try:
            return self._get_json(path_qs)
        except UwUnavailable:
            raise                     # 已带 status_code，再包一层就把它丢了
        except Exception as e:  # noqa: BLE001
            raise UwUnavailable(str(e)) from e

    def get_cached_gex(self, ticker: str, max_age_s: float = 300.0) -> dict | None:
        """非阻塞地读取 GEX 缓存，过期或未命中返回 None。

        时基必须与 gex_levels 写缓存一致用 time.monotonic()——曾误用 time.time() 读，
        两时钟相差 Unix 纪元量级(~17 亿秒)，恒判过期返 None，致前端距墙 wall 永远读不到。
        """
        hit = self._cache.get(("gex", ticker.upper()))
        if hit and (time.monotonic() - hit[0]) < max_age_s:
            return hit[1]
        return None

    def gex_levels(self, ticker: str, *, max_age_s: float = 300.0) -> dict:
        """当日 GEX 结构位 {call_wall/gamma_flip/gamma_magnet/put_wall}: Decimal|None。

        默认 300s 缓存（结构位盘中变化慢）。`max_age_s` 供高频调用方缩短——
        `gex_wall_service` 是 10s 一轮，吃 300s 缓存会让它 30 轮里 29 轮拿到旧值。
        失败抛 UwUnavailable（调用侧跳过该标的，保留上一轮的值）。
        """
        if not self._key:
            raise UwUnavailable("缺少 UW_API_KEY")
        key = ("gex", ticker.upper())
        hit = self._cache.get(key)
        if hit and (time.monotonic() - hit[0]) < max_age_s:
            return hit[1]
        try:
            payload = self._get_json(f"/api/stock/{ticker.upper()}/gex-levels")
        except UwUnavailable:
            raise                     # 已带 status_code，再包一层就把它丢了
        except Exception as e:  # noqa: BLE001
            raise UwUnavailable(str(e)) from e
        data = payload.get("data") or {}
        out: dict = {}
        for k in ("call_wall", "gamma_flip", "gamma_magnet", "put_wall"):
            v = data.get(k)
            try:
                out[k] = Decimal(str(v)) if v not in (None, "") else None
            except (InvalidOperation, ValueError):
                out[k] = None
        self._cache[key] = (time.monotonic(), out)
        return out

    def spot_gamma_dir(self, ticker: str, *, max_age_s: float = 300.0) -> str | None:
        """当前净 Gamma 的正负号:「pos」/「neg」/ None(取不到)。

        **只读 `gamma_per_one_percent_move_dir`(成交主动方向口径)**,不碰同一行里的
        `_oi` 与 `_vol` —— 三者会给出互相矛盾的答案(2026-08-25 实测 6 只票,4 只的
        oi 与 vol 符号相反)。盘中判断必须用 dir:客户主动买入 = 做市商被动卖出
        = 做市商负 Gamma。`_oi` 是全市场日频汇总模型的口径,**日内基本不翻号**
        (6 只票里 5 只整天符号一动不动),拿它做盘中判断等于把「市场状态」退化成
        一张固定的选股名单。完整实测表见 `tests/test_frontend_no_derived_net_gex.py`。

        端点返回的是**当天到此刻的整条序列**(约 550 行 / 316KB);`limit` 参数实测
        无效、裁不掉,所以默认缓存 300s,由调用方按需缩短。

        取不到符号(空序列 / 缺字段 / 解析失败 / 恰好为 0)返回 None 而**不抛** ——
        那是「这只票暂时没有 ±GEX 可显示」,抛出去会让调用方把整只票判成失败、
        连墙位一起标 stale。只有网络层失败才抛 UwUnavailable。
        """
        if not self._key:
            raise UwUnavailable("缺少 UW_API_KEY")
        key = ("spot_dir", ticker.upper())
        hit = self._cache.get(key)
        if hit and (time.monotonic() - hit[0]) < max_age_s:
            return hit[1]
        try:
            payload = self._get_json(f"/api/stock/{ticker.upper()}/spot-exposures")
        except UwUnavailable:
            raise                     # 已带 status_code，再包一层就把它丢了
        except Exception as e:  # noqa: BLE001
            raise UwUnavailable(str(e)) from e
        rows = payload.get("data") or []
        out: str | None = None
        if isinstance(rows, list) and rows:
            # 按 time 挑最新:服务端返回顺序不保证,直接取 data[-1] 可能拿到旧行,
            # 而 dir 口径日内是会翻号的(SPY 2026-08-24 翻 15 次),取错就是相反的符号。
            latest = max(rows, key=lambda r: str(r.get("time") or ""))
            raw = latest.get("gamma_per_one_percent_move_dir")
            try:
                v = Decimal(str(raw)) if raw not in (None, "") else None
            except (InvalidOperation, ValueError):
                v = None
            if v is not None and v != 0:
                out = "pos" if v > 0 else "neg"
        self._cache[key] = (time.monotonic(), out)
        return out

    def atm_chains(self, ticker: str, expiry: str) -> list[dict]:
        """ATM 合约原始行（备用报价佐证；主路径不依赖）。失败抛 UwUnavailable。"""
        if not self._key:
            raise UwUnavailable("缺少 UW_API_KEY")
        try:
            payload = self._get_json(f"/api/stock/{ticker.upper()}/atm-chains?expirations[]={expiry}")
        except UwUnavailable:
            raise                     # 已带 status_code，再包一层就把它丢了
        except Exception as e:  # noqa: BLE001
            raise UwUnavailable(str(e)) from e
        return list(payload.get("data", []) or [])

    def market_tide(self, date_str: str) -> list[dict] | None:
        """当日 market-tide 全市场资金潮流水。不落盘缓存（开盘每日实时获取）。失败抛 UwUnavailable。"""
        if not self._key:
            raise UwUnavailable("缺少 UW_API_KEY")
        try:
            payload = self._get_json("/api/market/market-tide", params={"date": date_str, "interval_5m": False})
        except UwUnavailable:
            raise                     # 已带 status_code，再包一层就把它丢了
        except Exception as e:  # noqa: BLE001
            raise UwUnavailable(str(e)) from e
        data = payload.get("data")
        return data if data else None

    def zero_dte_tide(
        self,
        date_str: str,
        tide_type: str = "all",
        moneyness: str = "all",
    ) -> list[dict] | None:
        """当日 0DTE 专属资金潮流水 (net-flow/expiry)。不落盘缓存。失败抛 UwUnavailable。

        调用 UW REST `/api/net-flow/expiry?date={date_str}&expiration=zero_dte&tide_type={tide_type}&moneyness={moneyness}`。
        **不带方括号是有意的**：官方文档写作 `expiration[]=`，2026-08-20 实测两种写法
        返回逐位相同，而不传该参数时数值完全不同 —— 说明过滤真的生效。别为了"对齐文档"
        改成 `expiration[]`，requests 会把它编码成 `expiration%5B%5D`，反而没验过。
        返回包含 timestamp(UTC 带 Z), net_call_premium, net_put_premium, net_volume,
        underlying_price 的分钟级流水；整日 390 根，覆盖 09:30–15:59 ET。
        """
        if not self._key:
            raise UwUnavailable("缺少 UW_API_KEY")
        try:
            params = {
                "date": date_str,
                "expiration": "zero_dte",
                "tide_type": tide_type,
                "moneyness": moneyness,
            }
            payload = self._get_json("/api/net-flow/expiry", params=params)
        except UwUnavailable:
            raise                     # 已带 status_code，再包一层就把它丢了
        except Exception as e:  # noqa: BLE001
            raise UwUnavailable(str(e)) from e

        data = payload.get("data")
        if not data:
            return None
        # Expiry Tide Response 结构: {"data": [{"tide_type": "all", "moneyness": "all", "data": [points...]}]}
        points = data
        if isinstance(data, list) and len(data) > 0 and isinstance(data[0], dict) and "data" in data[0]:
            points = data[0].get("data") or []
        if not isinstance(points, list):
            return None
        # 过滤掉未来时间槽位未填充的空点 (net_call_premium 为 None)
        valid_points = [
            p for p in points
            if isinstance(p, dict) and p.get("net_call_premium") is not None and p.get("net_put_premium") is not None
        ]
        return valid_points if valid_points else None

    def news_headlines(self, source: str = "Tradex", limit: int = 100) -> list[dict]:
        """快讯标题流（盘中滚动条用）。失败抛 UwUnavailable。

        调用 `/api/news/headlines?limit={limit}&sources={source}`，返回按时间倒序的
        `[{headline, created_at, source, is_major, sentiment, tickers, tags}, ...]`。

        **参数名只有这几个是真生效的**（2026-09-05 逐个实测）：`sources`（复数）、
        `ticker`（单数）、`limit`、`page`、`search_term`、`major_only`。写成
        `source` / `tickers` / `symbols` / `sentiment` 会被 UW **静默忽略**——不报错、
        直接返回全量，所以参数名写错的唯一症状是"筛选莫名其妙不起作用"。
        `sources` 也**只能填一个源**：逗号分隔只认第一个，重复传只认最后一个。
        日期类参数（`date` / `start_date` / `end_date`）同样被忽略，回溯只能靠 `page`。

        返回体里三个字段不可用，别去消费：`sentiment` 抽 1000 条全是 `neutral`；
        `tags` 恒为空数组；`tickers` 仅 30% 填充且把 New **York** 当成代码。
        """
        if not self._key:
            raise UwUnavailable("缺少 UW_API_KEY")
        try:
            payload = self._get_json("/api/news/headlines", params={
                "limit": min(int(limit), 100),   # 填 200 会被打回 422
                "sources": source,
            })
        except UwUnavailable:
            raise
        except Exception as e:  # noqa: BLE001
            raise UwUnavailable(str(e)) from e

        data = payload.get("data")
        return [x for x in data if isinstance(x, dict)] if isinstance(data, list) else []
