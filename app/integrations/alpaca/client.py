"""Alpaca 底层连接/鉴权 —— 唯一的 key 读取 + 惰性 SDK 客户端工厂（进程内共享）。

data.py（账户/时钟/分钟线/期权链）与 trading.py（下单/撤单）共享同一 TradingClient，
不再各自 new。key 从 .env 读（ALAPCA_AKEY / ALPACA_SKEY —— 注意 .env 里 ALPACA 拼作 ALAPCA）。

代理：CLAUDE.md 硬要求股票数据 API 走代理，且实测本机直连 Alpaca 报 WinError 10055、
走代理才通（2026-07-20 盘中）。alpaca-py 0.43 的 RESTClient 无 proxy 参数，内部用
requests —— 故在构造 SDK 客户端前把 .env 的 HTTP_PROXY 写进进程环境变量，由 requests
自动接管。
"""
from __future__ import annotations

import os
import pathlib
import re
from urllib.parse import urlsplit, urlunsplit

_BASE = pathlib.Path(__file__).resolve().parent.parent.parent.parent  # 仓库根（app/integrations/alpaca/client.py 上溯 4 层）

# .env 键名（新 → 旧回退）。2026-07-20 键名从 ACCESS_KEY/ACCESS_SECERT 改为下列名称。
_KEY_NAMES = ("ALAPCA_AKEY", "ALPACA_AKEY", "ACCESS_KEY")
_SEC_NAMES = ("ALPACA_SKEY", "ACCESS_SECERT")


def _read_env() -> dict[str, str]:
    env = {}
    env_file = _BASE / ".env"
    if not env_file.exists():
        return env
    for line in env_file.read_text(encoding="utf-8").splitlines():
        # 键名允许含数字（`[A-Za-z_]+` 读不出 SOCKS5_PROXY，2026-09-01 修）。
        m = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$", line)
        if m:
            env[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    return env


def _first(env: dict[str, str], names: tuple[str, ...]) -> str | None:
    for n in names:
        if env.get(n):
            return env[n]
    return None


def read_keys() -> tuple[str, str]:
    env = _read_env()
    key = _first(env, _KEY_NAMES)
    sec = _first(env, _SEC_NAMES)
    if not key or not sec:
        raise RuntimeError(f"缺少 Alpaca key（.env 需 {_KEY_NAMES[0]} / {_SEC_NAMES[0]}）")
    return key, sec


def normalize_proxy_url(raw: str | None) -> str | None:
    """把 Alpaca 代理规范化为 ``socks5h://host:port``。

    ``socks5h`` 的 ``h`` 表示域名交给代理端解析，避免本机 DNS 成为旁路。无 scheme 的
    ``host:port`` 与 ``socks5://`` 都升级为 ``socks5h://``；显式 HTTP 代理拒绝使用。
    """
    value = (raw or "").strip()
    if not value:
        return None
    if "://" not in value:
        value = f"socks5h://{value}"
    parts = urlsplit(value)
    if parts.scheme.lower() == "socks5":
        parts = parts._replace(scheme="socks5h")
    if parts.scheme.lower() != "socks5h":
        raise ValueError("Alpaca 代理必须使用 SOCKS5（socks5h://host:port）")
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError("Alpaca SOCKS5 代理端口无效") from exc
    if not parts.hostname or port is None or parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ValueError("Alpaca SOCKS5 代理地址必须是 socks5h://host:port")
    return urlunsplit(("socks5h", parts.netloc, "", "", ""))


def alpaca_proxy_url() -> str:
    """返回 Alpaca 专用 SOCKS5h URL；默认固定为本机 127.0.0.1:1080。

    新配置 ``ALPACA_PROXY_URL`` 优先。为兼容现有 `.env`，缺少新键时读取旧
    ``HTTP_PROXY``；旧键即便带 ``http://`` 也仅借用其 host:port，并强制改走 SOCKS5h。
    """
    file_env = _read_env()
    explicit = os.environ.get("ALPACA_PROXY_URL") or file_env.get("ALPACA_PROXY_URL")
    if explicit:
        proxy = normalize_proxy_url(explicit)
    else:
        legacy = (file_env.get("HTTP_PROXY") or "").strip()
        if legacy.startswith(("http://", "https://")):
            parsed = urlsplit(legacy)
            legacy = parsed.netloc
        proxy = normalize_proxy_url(legacy or "127.0.0.1:1080")
    if not proxy:  # 默认值保证不会发生；保留防御性 fail-closed。
        raise RuntimeError("缺少 ALPACA_PROXY_URL，禁止 Alpaca 自动直连")
    return proxy


def apply_proxy() -> str:
    """显式覆盖 Alpaca SDK 使用的代理环境变量，返回生效的 SOCKS5h URL。

    同时设置 NO_PROXY 环境变量，把本机地址（127.0.0.1、localhost）加进不走代理的名单，
    确保本地 mock 服务器可以绕过代理直连，而真实 Alpaca 流量仍然走代理。
    """
    proxy = alpaca_proxy_url()
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        os.environ[var] = proxy
    # 合并而非覆盖已有 NO_PROXY，让本机 mock 始终直连。
    for var in ("NO_PROXY", "no_proxy"):
        existing = [item.strip() for item in os.environ.get(var, "").split(",") if item.strip()]
        merged = list(dict.fromkeys([*existing, "127.0.0.1", "localhost"]))
        os.environ[var] = ",".join(merged)
    return proxy


def rest_timeout(kind: str = "trading") -> tuple[float, float]:
    """Alpaca REST 的 ``(连接超时, 读超时)``，单位秒。按用途分两档。

    alpaca-py 的 RESTClient 调 ``self._session.request(method, url, **opts)`` 时
    **opts 里没有 timeout**（``alpaca/common/rest.py``），requests 收到
    ``timeout=None`` 就是无限期等。请求又必须走 SOCKS5 代理，连接被挂住
    （不断开也不回数据）时只能干等对端主动掐断 —— 2026-08-25 那次等了 8 分 48 秒。

    后果不是"这一发请求慢"，而是 watchdog 整个停摆：它的主循环是
    ``await asyncio.to_thread(_watchdog_tick, ...)``，串行一个线程，
    卡住期间止损、超时平仓、触墙检查、成交回填全停。
    历史日志里同形态的静默共 13 次（最长 55 分钟）。

    **两档不能合并**，历史日志说得很清楚：

    - ``trading``（下单/撤单/查单/查持仓）—— 实测 `submit_ms` 最大 0.68s、
      `account_ms` 最大 1.29s，从来没有慢调用。watchdog 走的就是这一档，
      它必须收紧：这一发卡住等于止损、超时平仓、触墙检查全停。
    - ``data``（期权链、到期日、批量报价）—— **单发请求真的会跑到 16 秒**：
      2026-08-21 META 一发 `chain_ms=16233`（`chain_calls=1`）、
      2026-08-24 AMD 一发 `expiry_ms=16217`。这一档在**开仓选约**路径上，
      按 15s 砍会直接让人开不了仓，所以放宽到 45s —— 但仍然有限，
      不能再出现"永远等下去"。

    两档都可用环境变量覆盖（见下面的键名）。
    """
    env = _read_env()

    def _val(name: str, default: float) -> float:
        raw = (os.environ.get(name) or env.get(name) or "").strip()
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return default
        return value if value > 0 else default

    connect_s = _val("ALPACA_REST_CONNECT_TIMEOUT_S", 5.0)
    if kind == "data":
        return (connect_s, _val("ALPACA_REST_DATA_READ_TIMEOUT_S", 45.0))
    return (connect_s, _val("ALPACA_REST_READ_TIMEOUT_S", 15.0))


def install_rest_timeout(sdk_client, kind: str = "trading") -> bool:
    """给 SDK 客户端内部的 requests.Session 注入**默认**超时；已装过则不重复包。

    只填 ``setdefault``：调用方自己显式传了 timeout 就按它的来。
    返回是否本次真的装上（便于测试与诊断）。
    """
    session = getattr(sdk_client, "_session", None)
    if session is None or getattr(session, "_tt_timeout_installed", False):
        return False

    original_request = session.request
    default_timeout = rest_timeout(kind)

    def _request_with_timeout(*args, **kwargs):
        # 用 *args 透传：`Session.request` 的签名是 (method, url, params, data, ...)，
        # 写死 (method, url, **kwargs) 会让任何按位置传第三个参数的调用方炸掉。
        kwargs.setdefault("timeout", default_timeout)
        return original_request(*args, **kwargs)

    session.request = _request_with_timeout
    session._tt_timeout_installed = True
    return True


def endpoint_override() -> str | None:
    """本地 mock 服务器地址（.env 的 ALPACA_END_POINT）。未配置 → None → 走真实 Alpaca。

    SDK 的 url_override 要的是**根地址**（它自己拼 /v2、/v1beta1），
    但 trading_test 的 README 教的是写到 /v2，故此处兼容地剥掉尾部的 /v2 与斜杠。
    """
    raw = (_read_env().get("ALPACA_END_POINT") or "").strip()
    if not raw:
        return None
    raw = raw.rstrip("/")
    if raw.endswith("/v2"):
        raw = raw[: -len("/v2")]
    return raw or None


def trading_stream_endpoint_override() -> str | None:
    """Derive the mock TradingStream URL from ``ALPACA_END_POINT``."""
    endpoint = endpoint_override()
    if not endpoint:
        return None
    parts = urlsplit(endpoint)
    scheme = "wss" if parts.scheme == "https" else "ws"
    path = f"{parts.path.rstrip('/')}/stream"
    return urlunsplit((scheme, parts.netloc, path, "", ""))


def option_stream_endpoint_override(feed: str = "indicative") -> str | None:
    """Derive the mock option quote stream URL from ``ALPACA_END_POINT``."""
    endpoint = endpoint_override()
    if not endpoint:
        return None
    parts = urlsplit(endpoint)
    scheme = "wss" if parts.scheme == "https" else "ws"
    path = f"{parts.path.rstrip('/')}/v1beta1/{feed.strip() or 'indicative'}"
    return urlunsplit((scheme, parts.netloc, path, "", ""))


def is_loopback_url(url: str | None) -> bool:
    if not url:
        return False
    return (urlsplit(url).hostname or "").lower() in {"127.0.0.1", "localhost", "::1"}


class AlpacaClients:
    """惰性构造并缓存 TradingClient / StockHistoricalDataClient / OptionHistoricalDataClient。"""

    def __init__(self) -> None:
        self._key = self._sec = None
        self._trading = self._stock = self._option = None

    def _keys(self):
        if self._key is None:
            apply_proxy()  # 必须在构造任何 SDK 客户端前生效
            self._key, self._sec = read_keys()
        return self._key, self._sec

    def trading(self):
        if self._trading is None:
            from alpaca.trading.client import TradingClient
            # 只读一次 .env 并复用：endpoint_override() 内部会解析 .env 文件，
            # 这里既要传给 url_override，又要用来判断 mock/paper，避免重复解析。
            endpoint = endpoint_override()
            self._trading = TradingClient(*self._keys(), paper=True,
                                          url_override=endpoint)
            install_rest_timeout(self._trading)
            # 向 core 层事件流注册当前运行 mode（mock/paper）。
            # 方向是 integrations → core，符合单向依赖；core 不得反向
            # import integrations，故只能由这里主动注册。将来把
            # paper=True 参数化为可配置的 live 时，改这一处即可，不会漏。
            from app.core.event_stream import set_mode
            set_mode("mock" if endpoint else "paper")
        return self._trading

    def stock(self):
        if self._stock is None:
            from alpaca.data.historical import StockHistoricalDataClient
            self._stock = StockHistoricalDataClient(*self._keys(),
                                                    url_override=endpoint_override())
            install_rest_timeout(self._stock, kind="data")
        return self._stock

    def option(self):
        if self._option is None:
            from alpaca.data.historical.option import OptionHistoricalDataClient
            self._option = OptionHistoricalDataClient(*self._keys(),
                                                      url_override=endpoint_override())
            install_rest_timeout(self._option, kind="data")
        return self._option


CLIENTS = AlpacaClients()
