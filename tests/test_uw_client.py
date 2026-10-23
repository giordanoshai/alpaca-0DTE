"""UW 客户端：解析 / 缓存 / 失败降级 / 代理规范化。全部离线（mock _get_json）。"""
from decimal import Decimal

import pytest

from app.integrations.uw import UwClient, UwUnavailable, proxy_url


def test_proxy_url_adds_scheme():
    assert proxy_url("127.0.0.1:1080") == "socks5h://127.0.0.1:1080"
    assert proxy_url("socks5://127.0.0.1:1080") == "socks5h://127.0.0.1:1080"
    assert proxy_url("http://127.0.0.1:1080") == "socks5h://127.0.0.1:1080"


def test_proxy_url_keeps_scheme_and_empty():
    assert proxy_url("http://x:1") == "http://x:1"
    assert proxy_url("") is None
    assert proxy_url("  ") is None


def test_gex_levels_尊重传入的max_age_s(monkeypatch):
    """默认 300s 缓存会让 10s 轮询一直吃旧值，故必须能按调用方要求缩短。"""
    import time as _time
    c = UwClient(base_url="http://x", api_key="k")
    calls = []

    def _fake_get(path, params=None):
        calls.append(path)
        return {"data": {"call_wall": "185.0", "put_wall": "178.0"}}

    monkeypatch.setattr(c, "_get_json", _fake_get)

    fake_now = [1000.0]
    monkeypatch.setattr(_time, "monotonic", lambda: fake_now[0])
    monkeypatch.setattr("app.integrations.uw.time.monotonic", lambda: fake_now[0])

    c.gex_levels("SPY", max_age_s=5.0)
    fake_now[0] += 6.0                      # 超过 5s：必须重新请求
    c.gex_levels("SPY", max_age_s=5.0)
    assert len(calls) == 2

    fake_now[0] += 1.0                      # 未超过 5s：必须命中缓存
    c.gex_levels("SPY", max_age_s=5.0)
    assert len(calls) == 2


def test_atm_chains_returns_rows(monkeypatch):
    c = UwClient(api_key="k")
    monkeypatch.setattr(c, "_get_json", lambda path: {"data": [{"x": 1}]})
    assert c.atm_chains("SPY", "2026-07-16") == [{"x": 1}]


GEX_PAYLOAD = {"data": {"call_wall": "600", "gamma_flip": "560",
                        "gamma_magnet": None, "put_wall": "550"}}


def test_gex_levels_parses_and_caches(monkeypatch):
    c = UwClient(api_key="k")
    calls = []
    monkeypatch.setattr(c, "_get_json", lambda p: (calls.append(p), GEX_PAYLOAD)[1])
    g = c.gex_levels("SPY")
    assert g["call_wall"] == Decimal("600")
    assert g["put_wall"] == Decimal("550")
    assert g["gamma_magnet"] is None
    c.gex_levels("SPY")
    assert len(calls) == 1  # 300s 缓存命中


def test_get_cached_gex_hits_same_clock_as_gex_levels(monkeypatch):
    # 回归护栏：get_cached_gex 曾误用 time.time() 读、gex_levels 用 time.monotonic() 写，
    # 两时钟差 Unix 纪元量级 → 恒判过期返 None → 前端距墙 wall 永远读不到。
    c = UwClient(api_key="k")
    monkeypatch.setattr(c, "_get_json", lambda p: GEX_PAYLOAD)
    assert c.get_cached_gex("SPY") is None       # 未命中
    c.gex_levels("SPY")                            # 写缓存
    hit = c.get_cached_gex("SPY")                  # 同帧立刻读必须命中
    assert hit is not None and hit["call_wall"] == Decimal("600")


def test_gex_levels_failure_raises(monkeypatch):
    c = UwClient(api_key="k")
    monkeypatch.setattr(c, "_get_json", lambda p: (_ for _ in ()).throw(OSError("timeout")))
    with pytest.raises(UwUnavailable):
        c.gex_levels("SPY")


def test_gex_levels_missing_key_raises():
    with pytest.raises(UwUnavailable):
        UwClient(api_key="").gex_levels("SPY")


def test_同一个client复用一条Session而不是每次新建连接(monkeypatch):
    """2026-08-15 实测（33 只票打中转站）：模块级 `requests.get` 每次都新建连接，
    单只中位 **0.74s**；换成复用 Session 后除首个请求外单只中位 **0.14s**，5 倍差。
    差的那 0.6s 全是 SOCKS5 + TLS 握手。

    距墙服务 33 只票、每轮全部重新握手、单只预算只有 3s —— 这正是当天
    「第一轮 0/33 成功、17 只读超时 + 16 只整轮超时」的根因。
    """
    import app.integrations.uw as uw

    module_level_calls = []

    def _boom(*a, **k):
        module_level_calls.append(a)
        raise AssertionError("不该走模块级 requests.get")

    monkeypatch.setattr(uw.requests, "get", _boom)

    sessions = []

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"data": {}}

    class _Sess:
        def __init__(self):
            sessions.append(self)
            self.gets = 0

        def mount(self, *a, **k):
            pass

        def get(self, url, **kw):
            self.gets += 1
            return _Resp()

    monkeypatch.setattr(uw.requests, "Session", _Sess)

    c = uw.UwClient(api_key="k", base_url="http://x")
    c._get_json("/a")
    c._get_json("/b")
    c._get_json("/c")

    assert module_level_calls == [], "还在用模块级 requests.get（每次新建连接）"
    assert len(sessions) == 1, f"建了 {len(sessions)} 条 Session，应当只有 1 条"
    assert sessions[0].gets == 3


def test_非200抛UwUnavailable并带上状态码(monkeypatch):
    """422（源站说这只票不行）必须能和 530/504（中转站自己挂了）区分开：
    前者该永久跳过，后者下一轮还要照常重试。状态码丢了就分不出来，
    只能二选一 —— 要么每 10s 给死票刷屏，要么一次中转站抖动把整池永久废掉。
    """
    import app.integrations.uw as uw

    class _Resp:
        status_code = 422
        text = '{"detail":"unprocessable"}'

        @staticmethod
        def json():
            return {}

    class _Sess:
        def mount(self, *a, **k):
            pass

        def get(self, url, **kw):
            return _Resp()

    monkeypatch.setattr(uw.requests, "Session", _Sess)

    c = uw.UwClient(api_key="k", base_url="http://x")
    with pytest.raises(UwUnavailable) as ei:
        c.gex_levels("^VIX")
    assert ei.value.status_code == 422


def test_market_tide_success_and_failure(monkeypatch):
    c = UwClient(api_key="k")
    monkeypatch.setattr(c, "_get_json", lambda path, params: {"data": [{"net_call_premium": 100}]})
    res = c.market_tide("2026-08-04")
    assert res == [{"net_call_premium": 100}]

    monkeypatch.setattr(c, "_get_json", lambda path, params: (_ for _ in ()).throw(OSError("timeout")))
    with pytest.raises(UwUnavailable):
        c.market_tide("2026-08-04")


# ------------------------------------------------------- spot_gamma_dir（±GEX 真值）

# 真实响应的形状（2026-08-25 实拉 SPY 截取两行）。注意每行给**三套**口径，
# 且服务端返回顺序不保证有序 —— 这里故意把新的一行放在前面。
SPOT_PAYLOAD = {"data": [
    {"time": "2026-08-24T20:00:58.000000Z", "price": "763.47",
     "gamma_per_one_percent_move_oi": "-13421666354.85",
     "gamma_per_one_percent_move_vol": "-142063492746.72",
     "gamma_per_one_percent_move_dir": "4424995651.75"},
    {"time": "2026-08-24T19:59:12.000000Z", "price": "763.20",
     "gamma_per_one_percent_move_oi": "-13421666354.85",
     "gamma_per_one_percent_move_vol": "-142063492746.72",
     "gamma_per_one_percent_move_dir": "-880000000.0"},
]}


def test_spot_gamma_dir_按时间取最新一行而不是数组末尾(monkeypatch):
    """服务端返回顺序不保证 —— 必须按 time 挑最新，不能直接取 data[-1]。

    这个用例里 data[-1] 是 19:59 的负值、真正最新的 20:00 是正值，
    取错就会得到相反的符号。
    """
    c = UwClient(base_url="http://x", api_key="k")
    monkeypatch.setattr(c, "_get_json", lambda p: SPOT_PAYLOAD)
    assert c.spot_gamma_dir("SPY") == "pos"


def test_spot_gamma_dir_只认dir口径不碰oi和vol(monkeypatch):
    """三个口径会给出相反答案：这行 oi/vol 都是负的、dir 是正的。

    盘中判断只能用 dir（按成交主动方向：客户主动买入 = 做市商被动卖出 = 负 Gamma）。
    oi 是全市场日频汇总模型的口径，日内基本不翻号（6 只票里 5 只整天符号不动），
    拿它做盘中判断等于把「市场状态」退化成一张固定的选股名单。
    完整实测表见 tests/test_frontend_no_derived_net_gex.py。
    """
    c = UwClient(base_url="http://x", api_key="k")
    monkeypatch.setattr(c, "_get_json", lambda p: SPOT_PAYLOAD)
    assert c.spot_gamma_dir("SPY") == "pos", "取到 neg 说明读的是 oi 或 vol"


def test_spot_gamma_dir_负值返回neg(monkeypatch):
    c = UwClient(base_url="http://x", api_key="k")
    payload = {"data": [{"time": "2026-08-24T20:00:00.000000Z",
                         "gamma_per_one_percent_move_dir": "-123.0"}]}
    monkeypatch.setattr(c, "_get_json", lambda p: payload)
    assert c.spot_gamma_dir("SPY") == "neg"


@pytest.mark.parametrize("payload", [
    {"data": []},                                                   # 空序列
    {"data": [{"time": "2026-08-24T20:00:00.000000Z"}]},            # 缺 dir 字段
    {"data": [{"time": "2026-08-24T20:00:00.000000Z",
               "gamma_per_one_percent_move_dir": None}]},           # dir 为 null
    {"data": [{"time": "2026-08-24T20:00:00.000000Z",
               "gamma_per_one_percent_move_dir": "abc"}]},          # 解析不了
    {},                                                             # 连 data 都没有
])
def test_spot_gamma_dir_取不到就返回None而不是抛(monkeypatch, payload):
    """取不到符号是「这只票暂时没有 ±GEX 可显示」，不是错误 —— 抛出去会让
    调用方把整只票判为失败、连墙位一起标 stale。"""
    c = UwClient(base_url="http://x", api_key="k")
    monkeypatch.setattr(c, "_get_json", lambda p: payload)
    assert c.spot_gamma_dir("SPY") is None


def test_spot_gamma_dir_零值返回None(monkeypatch):
    """恰好为 0 既不是正也不是负，不该硬塞进任何一边。"""
    c = UwClient(base_url="http://x", api_key="k")
    payload = {"data": [{"time": "2026-08-24T20:00:00.000000Z",
                         "gamma_per_one_percent_move_dir": "0"}]}
    monkeypatch.setattr(c, "_get_json", lambda p: payload)
    assert c.spot_gamma_dir("SPY") is None


def test_spot_gamma_dir_命中缓存不重复请求(monkeypatch):
    """单票 316KB（limit 参数实测无效、裁不掉），重复拉是纯浪费。"""
    c = UwClient(base_url="http://x", api_key="k", cache_ttl=60.0)
    calls = []
    monkeypatch.setattr(c, "_get_json", lambda p: (calls.append(p), SPOT_PAYLOAD)[1])
    assert c.spot_gamma_dir("SPY", max_age_s=60.0) == "pos"
    assert c.spot_gamma_dir("SPY", max_age_s=60.0) == "pos"
    assert len(calls) == 1


def test_spot_gamma_dir_缓存与gex_levels互不串味(monkeypatch):
    """两个方法共用 self._cache，键必须分开 —— 否则 gex_levels 的 dict
    会被当成 ±GEX 读出来。"""
    c = UwClient(base_url="http://x", api_key="k", cache_ttl=60.0)

    def _fake(path):
        return SPOT_PAYLOAD if "spot-exposures" in path else GEX_PAYLOAD

    monkeypatch.setattr(c, "_get_json", _fake)
    assert c.spot_gamma_dir("SPY") == "pos"
    assert c.gex_levels("SPY")["call_wall"] == Decimal("600")


def test_spot_gamma_dir_请求失败照常抛(monkeypatch):
    """网络层失败要抛 —— 与「拿到了但没有符号」区分开，调用方据此保留旧值。"""
    c = UwClient(base_url="http://x", api_key="k")
    monkeypatch.setattr(c, "_get_json", lambda p: (_ for _ in ()).throw(OSError("timeout")))
    with pytest.raises(UwUnavailable):
        c.spot_gamma_dir("SPY")


def test_spot_gamma_dir_没有key直接抛():
    with pytest.raises(UwUnavailable):
        UwClient(base_url="http://x", api_key="").spot_gamma_dir("SPY")


def test_spot_gamma_dir_打的是spot_exposures端点(monkeypatch):
    c = UwClient(base_url="http://x", api_key="k")
    calls = []
    monkeypatch.setattr(c, "_get_json", lambda p: (calls.append(p), SPOT_PAYLOAD)[1])
    c.spot_gamma_dir("spy")
    assert calls == ["/api/stock/SPY/spot-exposures"]


# ─────────── 快讯 headlines（盘中滚动条用，2026-09-05 加）───────────

def test_news_headlines_locks_source_and_limit(monkeypatch):
    """必须带 `sources`（单数 `source` 会被 UW 静默忽略），limit 上限是 100。

    UW 对不认识的参数不报错、直接当没看见 —— 2026-09-05 实测 `source=` / `tickers=` /
    `symbols=` / `sentiment=` 全部被无视，只有 `sources=` 和 `ticker=` 生效。所以
    参数名写错的后果是「悄悄拿回全量」，只能靠这里钉住。
    """
    c = UwClient(base_url="http://x", api_key="k")
    seen = {}

    def _fake_get(path, params=None):
        seen["path"], seen["params"] = path, params
        return {"data": [{"headline": "Fed's Waller backs hold", "source": "Tradex"}]}

    monkeypatch.setattr(c, "_get_json", _fake_get)
    rows = c.news_headlines(source="Tradex", limit=100)

    assert seen["path"] == "/api/news/headlines"
    assert seen["params"]["sources"] == "Tradex"
    assert seen["params"]["limit"] == 100
    assert rows[0]["headline"] == "Fed's Waller backs hold"


def test_news_headlines_caps_limit_at_100(monkeypatch):
    """填 200 会被 UW 直接打回 422（实测），客户端先自己夹住。"""
    c = UwClient(base_url="http://x", api_key="k")
    seen = {}
    monkeypatch.setattr(c, "_get_json",
                        lambda p, params=None: seen.update(params or {}) or {"data": []})
    c.news_headlines(source="Tradex", limit=500)
    assert seen["limit"] == 100


def test_news_headlines_without_key_raises():
    with pytest.raises(UwUnavailable):
        UwClient(base_url="http://x", api_key="").news_headlines()
