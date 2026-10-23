# -*- coding: utf-8 -*-
"""距墙服务：纯函数层。"""
from decimal import Decimal

import pytest

from app.services import gex_wall_service as svc


def test_to_payload_把Decimal墙位转成字符串():
    out = svc._to_payload({
        "call_wall": Decimal("185.0"), "put_wall": Decimal("178.0"),
        "gamma_flip": Decimal("181.0"), "gamma_magnet": Decimal("183.5"),
    })
    # 取两个墙位 + gamma_flip；magnet 不展示（2026-08-17 加 gamma_flip，前端用它
    # 跟实时价比大小推 Net GEX 正负号，不再是「只取两个墙位」）
    assert out == {"call_wall": "185.0", "put_wall": "178.0", "gamma_flip": "181.0"}


def test_to_payload_三者都缺时返回None():
    """三个结构位都没有等于没数据，不该占一个快照键位、在前端渲染出一行空的。"""
    assert svc._to_payload({"call_wall": None, "put_wall": None, "gamma_flip": None}) is None


def test_to_payload_单侧缺失仍然保留另一侧():
    out = svc._to_payload({"call_wall": None, "put_wall": Decimal("178.0")})
    assert out == {"call_wall": None, "put_wall": "178.0", "gamma_flip": None}


def test_to_payload_只有gamma_flip有值也不算没数据():
    """两道墙都没有、只有 gamma_flip 有值：仍要保留，不能因为墙位判空就把它一起丢了。"""
    out = svc._to_payload({"call_wall": None, "put_wall": None, "gamma_flip": Decimal("560")})
    assert out == {"call_wall": None, "put_wall": None, "gamma_flip": "560"}


@pytest.mark.parametrize("n,expected", [(0, 4), (1, 4), (4, 4), (12, 12), (32, 16), (60, 16)])
def test_worker_count_自适应并发度(n, expected):
    """下限 4：小池子别串行拖满窗口。上限 16：保护共用的中转站。"""
    assert svc._worker_count(n) == expected


def test_merge_round_成功的票写入新值且不是stale():
    # net_gex 是 2026-08-25 加的第四个字段；这里没传 net_gex 参数，
    # 且 previous 为空 —— 无旧值可继承，故为 None（见下面 ±GEX 那组用例）。
    out = svc.merge_round({}, {"SPY": {"call_wall": "600", "put_wall": "590"}}, now=123.0)
    assert out["SPY"] == {"call_wall": "600", "put_wall": "590",
                         "net_gex": None, "ts": 123.0, "stale": False}


def test_merge_round_失败的票保留旧值并标stale():
    previous = {"SPY": {"call_wall": "600", "put_wall": "590", "ts": 100.0, "stale": False}}
    out = svc.merge_round(previous, {"SPY": None}, now=123.0)
    assert out["SPY"]["call_wall"] == "600"      # 旧值保留
    assert out["SPY"]["ts"] == 100.0             # 时间戳停在上次成功那一刻
    assert out["SPY"]["stale"] is True


def test_merge_round_从未成功过的票不出现在快照里():
    """没有旧值可留，宁可不渲染，也不能显示一行空墙位。"""
    out = svc.merge_round({}, {"NFLX": None}, now=123.0)
    assert "NFLX" not in out


def test_merge_round_移出盯盘池的票自动消失():
    """results 的键就是本轮请求的池子，被删的票不该赖在快照里。"""
    previous = {"SPY": {"call_wall": "600", "put_wall": "590", "ts": 100.0, "stale": False}}
    out = svc.merge_round(previous, {"QQQ": {"call_wall": "520", "put_wall": "510"}}, now=123.0)
    assert "SPY" not in out
    assert "QQQ" in out


# ── 轮询循环 ──────────────────────────────────────────────────────────────
#
# fake 必须模拟**真实语义**，不能只返回好数据 —— 只模拟 happy path 的 fake
# 会让真 bug 全绿（docs/bug_08151139.MD 的分诊结论）。
# 故下面的 fake 覆盖：抛 UwUnavailable、字段为 None、请求挂住不返回。

import asyncio
import time

from app.integrations.uw import UwUnavailable


class _FakeUw:
    """按 ticker 决定行为的 UwClient 替身。"""

    def __init__(self, behavior: dict, net_gex: dict | None = None):
        self.behavior = behavior
        self.calls: list[str] = []
        self.max_age_seen: list[float] = []
        # ±GEX 的行为单独一张表：ticker -> "pos"/"neg"/None/"raise"，默认 "pos"。
        # **必须模拟这个方法** —— 替身少一个方法时 refresh_once 里那层
        # `except Exception` 会把 AttributeError 一起吞掉，于是 ±GEX 那条路
        # 一次都没真跑过，测试却全绿。
        self.net_gex_behavior = net_gex or {}
        self.net_gex_calls: list[str] = []
        self.net_gex_max_age_seen: list[float] = []

    def spot_gamma_dir(self, ticker, *, max_age_s=300.0):
        self.net_gex_calls.append(ticker)
        self.net_gex_max_age_seen.append(max_age_s)
        b = self.net_gex_behavior.get(ticker, "pos")
        if b == "raise":
            raise UwUnavailable("中转站超时")
        return b

    def gex_levels(self, ticker, *, max_age_s=300.0):
        self.calls.append(ticker)
        self.max_age_seen.append(max_age_s)
        b = self.behavior.get(ticker, "ok")
        if isinstance(b, tuple) and b[0] == "http":
            raise UwUnavailable(f"HTTP {b[1]}", status_code=b[1])
        if b == "raise":
            raise UwUnavailable("中转站超时")
        if b == "null":
            return {"call_wall": None, "put_wall": None,
                    "gamma_flip": None, "gamma_magnet": None}
        if b == "hang":
            # 只睡 2s：够超过用例里 monkeypatch 的 0.5s 整轮超时即可。
            # ⚠️ 不要写大 —— 线程**取消不掉**，task.cancel() 只解除 await，
            # 线程还在跑；专属线程池的非 daemon 线程最终由解释器 atexit join，
            # 写成 30s 就是给整个测试进程的退出加 30 秒。
            time.sleep(2.0)
        return {"call_wall": Decimal("185.0"), "put_wall": Decimal("178.0"),
                "gamma_flip": None, "gamma_magnet": None}


@pytest.fixture(autouse=True)
def _clean_state():
    """每条用例都从空快照起步，避免互相污染模块级状态。"""
    svc._WALLS = {}
    svc._GENERATION = 0
    svc._ROUND_SEQ = 0        # 不重置的话，用例之间会互相决定「这一轮拉不拉 ±GEX」
    svc._UNSUPPORTED.clear()
    yield
    svc._WALLS = {}
    svc._GENERATION = 0
    svc._ROUND_SEQ = 0
    svc._UNSUPPORTED.clear()


def test_refresh_once_写入快照并递增generation():
    fake = _FakeUw({})
    asyncio.run(svc.refresh_once(lambda: ["SPY", "QQQ"], fake))
    snap = svc.snapshot()
    assert snap["SPY"]["call_wall"] == "185.0"
    assert snap["QQQ"]["stale"] is False
    assert svc.generation() == 1


def test_refresh_once_传了缩短的缓存上限():
    """默认 300s 会让 10s 轮询 30 轮里 29 轮拿旧值。"""
    fake = _FakeUw({})
    asyncio.run(svc.refresh_once(lambda: ["SPY"], fake))
    assert fake.max_age_seen == [svc._GEX_MAX_AGE_S]


def test_refresh_once_单只抛异常不影响其他票():
    fake = _FakeUw({"SPY": "raise"})
    asyncio.run(svc.refresh_once(lambda: ["SPY", "QQQ"], fake))
    snap = svc.snapshot()
    assert "SPY" not in snap            # 从未成功过 → 不出现
    assert snap["QQQ"]["call_wall"] == "185.0"


def test_refresh_once_失败时保留上一轮的值并标stale():
    ok = _FakeUw({})
    asyncio.run(svc.refresh_once(lambda: ["SPY"], ok))
    bad = _FakeUw({"SPY": "raise"})
    asyncio.run(svc.refresh_once(lambda: ["SPY"], bad))
    snap = svc.snapshot()
    assert snap["SPY"]["call_wall"] == "185.0"   # 旧值还在
    assert snap["SPY"]["stale"] is True


def test_refresh_once_墙位全None的票不进快照():
    fake = _FakeUw({"NFLX": "null"})
    asyncio.run(svc.refresh_once(lambda: ["NFLX"], fake))
    assert "NFLX" not in svc.snapshot()


def test_refresh_once_整轮超时时已完成的结果仍然保留(monkeypatch):
    """一只票挂住不能让整轮颗粒无收。"""
    monkeypatch.setattr(svc, "_WALL_ROUND_TIMEOUT_S", 0.5)
    fake = _FakeUw({"SPY": "hang"})
    asyncio.run(svc.refresh_once(lambda: ["SPY", "QQQ"], fake))
    snap = svc.snapshot()
    assert "SPY" not in snap                      # 超时且无旧值
    assert snap["QQQ"]["call_wall"] == "185.0"    # 正常的那只照常写入


def test_值一个没动的一轮不递增generation():
    """generation 是 trading_ws 的变更 token。无条件 +1 会让整个 walls dict
    **每 10 秒广播给每个客户端一次、通宵不停**，即使墙位一个数都没动
    —— 顺带每轮把 hub 的 _seq 推高、snapshot 缓存作废。

    ⚠️ 比对不能带上 `ts`：它每轮都是 time.time()，参与比对就等于永远判定为变化。
    """
    fake = _FakeUw({})
    asyncio.run(svc.refresh_once(lambda: ["SPY", "QQQ"], fake))
    g1 = svc.generation()
    assert g1 == 1

    asyncio.run(svc.refresh_once(lambda: ["SPY", "QQQ"], fake))
    assert svc.generation() == g1, "墙位没变却推了一帧"


def test_stale翻转要递增generation():
    """值虽然是旧的那份，但整行要从正常色变灰显 —— 前端得收到这一帧。"""
    asyncio.run(svc.refresh_once(lambda: ["SPY"], _FakeUw({})))
    g1 = svc.generation()
    asyncio.run(svc.refresh_once(lambda: ["SPY"], _FakeUw({"SPY": "raise"})))
    assert svc.generation() == g1 + 1
    assert svc.snapshot()["SPY"]["stale"] is True


def test_refresh_once_空池子不报错也不递增generation():
    fake = _FakeUw({})
    asyncio.run(svc.refresh_once(lambda: [], fake))
    assert svc.snapshot() == {}
    assert svc.generation() == 0
    assert fake.calls == []


def test_缺少UW_KEY时不发请求也不刷屏(monkeypatch):
    """没配 key 时每轮 32 只票各抛一次异常没有意义，直接整轮跳过。"""
    monkeypatch.setattr(svc, "_UW_KEY", "")
    fake = _FakeUw({})
    asyncio.run(svc.refresh_once(lambda: ["SPY", "QQQ"], fake))
    assert fake.calls == []
    assert svc.snapshot() == {}


def test_拉取跑在专属线程池而不是默认池():
    """默认线程池（本机 28 槽）上还挂着 `app/routers/orders.py:101` 的**点击→提交**、
    market_tide 那个 15s 超时的调用、market_feed / trading_state / option_candidates。

    距墙每 10 秒要占最多 16 槽、每槽最长 ~3s。`asyncio.to_thread` 走的正是那个共享池，
    等于把下单排在一堆 UW HTTP 后面 —— 而下单延迟正在查（点击→提交实测 max 14.7s，
    券商侧 55-500ms 无辜）。本模块自己写的解耦约束第 3 条是「失败不外溢」，
    线程池同样不能外溢。
    """
    import threading

    seen: list[str] = []

    class _NameProbe:
        def gex_levels(self, ticker, *, max_age_s=300.0):
            seen.append(threading.current_thread().name)
            return {"call_wall": Decimal("185.0"), "put_wall": Decimal("178.0")}

    asyncio.run(svc.refresh_once(lambda: ["SPY", "QQQ"], _NameProbe()))

    assert seen, "探针没被调到"
    assert all(name.startswith("gexwall") for name in seen), \
        f"跑在了默认池上（asyncio.to_thread）：{seen}"


# ── 跳过没有 GEX 的标的 ────────────────────────────────────────────────────
#
# 两类失败必须分开处理，混为一谈会各自坏一头：
#   · 永久（源站按 ticker 拒绝，422/404）→ 每 10s 重试一次纯粹刷屏，还白占一个并发槽
#   · 临时（中转站 5xx / 读超时 / 429）  → 按失败拉黑等于抖动一次就把整池永久废掉


def test_指数代码根本不发请求():
    """`^VIX` 是 `app/config.py:128` 与 `pool_screener.py:43` 故意放进票池的大盘
    context 标的 —— 不可交易、没有期权链，自然没有 GEX。UW 2026-08-15 18:23:07
    对 `/api/stock/%5EVIX/gex-levels` 明确回 422。

    这类符号靠符号本身就能判定，不必先打一次请求再挨拒。
    """
    fake = _FakeUw({})
    asyncio.run(svc.refresh_once(lambda: ["^VIX", "SPY"], fake))
    assert fake.calls == ["SPY"], f"给指数代码发了请求：{fake.calls}"
    assert "^VIX" not in svc.snapshot()


def test_源站按ticker拒绝就不再重试():
    fake = _FakeUw({"FOO": ("http", 422)})
    asyncio.run(svc.refresh_once(lambda: ["FOO", "SPY"], fake))
    assert "FOO" in fake.calls, "第一轮总要试一次才知道不行"

    fake2 = _FakeUw({"FOO": ("http", 422)})
    asyncio.run(svc.refresh_once(lambda: ["FOO", "SPY"], fake2))
    assert fake2.calls == ["SPY"], "422 之后还在每轮重试"


@pytest.mark.parametrize("behavior", [("http", 530), ("http", 504), ("http", 429), "raise"])
def test_中转站故障不得把票拉黑(behavior):
    """530（Argo Tunnel 断，2026-08-15 21 点实测）、504、429、读超时都是**整站
    且临时**的。按失败拉黑 → 中转站抖一下 33 只票全废，而且永远不会自己恢复。
    """
    fake = _FakeUw({"SPY": behavior})
    asyncio.run(svc.refresh_once(lambda: ["SPY"], fake))

    fake2 = _FakeUw({})
    asyncio.run(svc.refresh_once(lambda: ["SPY"], fake2))
    assert fake2.calls == ["SPY"], f"{behavior} 被误当成永久失败"
    assert svc.snapshot()["SPY"]["call_wall"] == "185.0"


@pytest.mark.parametrize("code", [401, 403])
def test_鉴权失败也不拉黑(code):
    """401/403 是 key 的问题、全局的，不是"这只票没有 GEX"。
    按 ticker 拉黑会在一次 key 抖动之后把整个池子清空且不可恢复。
    """
    fake = _FakeUw({"SPY": ("http", code)})
    asyncio.run(svc.refresh_once(lambda: ["SPY"], fake))

    fake2 = _FakeUw({})
    asyncio.run(svc.refresh_once(lambda: ["SPY"], fake2))
    assert fake2.calls == ["SPY"], f"HTTP {code} 被误当成这只票没有 GEX"


def test_整轮超时必须短于轮询间隔():
    """否则上一轮还没收完下一轮就开跑，请求在共用的中转站上叠罗汉。"""
    assert svc._WALL_ROUND_TIMEOUT_S < svc._WALL_POLL_INTERVAL_S


def test_缓存上限必须短于轮询间隔():
    """`max_age_s` ≥ 轮询间隔的话，每轮都会命中上一轮的缓存，墙位永远不更新。"""
    assert svc._GEX_MAX_AGE_S < svc._WALL_POLL_INTERVAL_S


def test_active_tickers_只取keys不构造state():
    """每 10s 调一次只为拿代码列表；get_all_states() 会为每只票构造完整 state，太重。"""
    from app.watcher.watcher_manager import WatcherManager

    m = WatcherManager.__new__(WatcherManager)     # 不跑 __init__，避免拉起真实依赖
    m._watchers = {"SPY": object(), "QQQ": object()}
    assert sorted(m.active_tickers()) == ["QQQ", "SPY"]




# ------------------------------------------------------------- ±GEX（net_gex）
#
# 节拍设计：±GEX 走**独立的小轮次**，排在距墙那一轮之后，不跟它抢同一个 10s 窗口。
# 原因是线程池只有 16 条线程，每票直接多发一个请求就只能同时跑 8 只票，
# 33 只要 4 批 × 8s = 32s，远超整轮上限，结果是大批 stale。
# 又因为 ±GEX 是慢变量（SPY 2026-08-24 全天翻 15 次，约 26 分钟一次），
# 每 2 轮（60s）拉一次足够。


def test_should_pull_net_gex_每两轮才拉一次():
    """距墙 30s 一轮；±GEX 每 2 轮 = 60s 一次，对一天翻 15 次的量足够。"""
    got = [svc._should_pull_net_gex(i) for i in range(6)]
    assert got == [True, False, True, False, True, False]


def test_merge_round_不传net_gex时沿用上一轮的值():
    """非 ±GEX 轮次：墙位照常更新，±GEX 保持不变 —— 不能因为本轮没拉就抹成空。"""
    previous = {"SPY": {"call_wall": "600", "put_wall": "590", "gamma_flip": "595",
                        "net_gex": "neg", "ts": 100.0, "stale": False}}
    results = {"SPY": {"call_wall": "601", "put_wall": "591", "gamma_flip": "596"}}
    out = svc.merge_round(previous, results, now=123.0)
    assert out["SPY"]["call_wall"] == "601"      # 墙位是新的
    assert out["SPY"]["net_gex"] == "neg"        # ±GEX 沿用
    assert out["SPY"]["stale"] is False


def test_merge_round_传了net_gex就用新值():
    previous = {"SPY": {"call_wall": "600", "put_wall": "590", "gamma_flip": "595",
                        "net_gex": "neg", "ts": 100.0, "stale": False}}
    results = {"SPY": {"call_wall": "600", "put_wall": "590", "gamma_flip": "595"}}
    out = svc.merge_round(previous, results, now=123.0, net_gex={"SPY": "pos"})
    assert out["SPY"]["net_gex"] == "pos"


def test_merge_round_拉到了但没数据要写None而不是沿用旧值():
    """「在 dict 里但值是 None」= 这轮确实拉到了、这只票就是没有 ±GEX。
    这是最新事实，必须覆盖旧值，否则一只票会永远挂着几小时前的符号。"""
    previous = {"SPY": {"call_wall": "600", "put_wall": "590", "gamma_flip": "595",
                        "net_gex": "neg", "ts": 100.0, "stale": False}}
    results = {"SPY": {"call_wall": "600", "put_wall": "590", "gamma_flip": "595"}}
    out = svc.merge_round(previous, results, now=123.0, net_gex={"SPY": None})
    assert out["SPY"]["net_gex"] is None


def test_merge_round_这只票本轮拉失败则沿用旧值():
    """「不在 dict 里」= 这只票这轮没拉成功，跟「拉到了但没数据」是两回事。"""
    previous = {"SPY": {"call_wall": "600", "put_wall": "590", "gamma_flip": "595",
                        "net_gex": "neg", "ts": 100.0, "stale": False},
                "QQQ": {"call_wall": "500", "put_wall": "490", "gamma_flip": "495",
                        "net_gex": "pos", "ts": 100.0, "stale": False}}
    results = {"SPY": {"call_wall": "600", "put_wall": "590", "gamma_flip": "595"},
               "QQQ": {"call_wall": "500", "put_wall": "490", "gamma_flip": "495"}}
    out = svc.merge_round(previous, results, now=123.0, net_gex={"SPY": "pos"})
    assert out["SPY"]["net_gex"] == "pos"        # 拉到了
    assert out["QQQ"]["net_gex"] == "pos"        # 没拉到，沿用旧值（本来就是 pos）


def test_merge_round_新票没有旧值时net_gex为None():
    out = svc.merge_round({}, {"SPY": {"call_wall": "600", "put_wall": "590",
                                       "gamma_flip": "595"}}, now=123.0)
    assert out["SPY"]["net_gex"] is None


def test_merge_round_墙位失败时net_gex也跟着旧值一起留下():
    """整只票 stale：墙位与 ±GEX 是同一份快照的两半，不该一半新一半旧。"""
    previous = {"SPY": {"call_wall": "600", "put_wall": "590", "gamma_flip": "595",
                        "net_gex": "neg", "ts": 100.0, "stale": False}}
    out = svc.merge_round(previous, {"SPY": None}, now=123.0, net_gex={"SPY": "pos"})
    assert out["SPY"]["stale"] is True
    assert out["SPY"]["net_gex"] == "neg"


def test_content_key_把net_gex算进指纹():
    """±GEX 翻号必须触发推帧 —— 不算进指纹的话，墙位没动时前端永远看不到翻号。"""
    a = {"SPY": {"call_wall": "600", "put_wall": "590", "gamma_flip": "595",
                 "net_gex": "neg", "stale": False}}
    b = {"SPY": {"call_wall": "600", "put_wall": "590", "gamma_flip": "595",
                 "net_gex": "pos", "stale": False}}
    assert svc._content_key(a) != svc._content_key(b)


# ------------------------------------------------- ±GEX 小轮次在 refresh_once 里的接线


def test_refresh_once_首轮就拉ampGEX并写进快照():
    fake = _FakeUw({}, net_gex={"SPY": "neg"})
    asyncio.run(svc.refresh_once(lambda: ["SPY"], fake))
    assert fake.net_gex_calls == ["SPY"]
    assert svc.snapshot()["SPY"]["net_gex"] == "neg"


def test_refresh_once_第二轮不拉ampGEX但沿用上一轮的符号():
    """±GEX 每 2 轮才拉一次（60s）；中间那轮墙位照常更新、符号不能被抹掉。"""
    fake = _FakeUw({}, net_gex={"SPY": "neg"})
    asyncio.run(svc.refresh_once(lambda: ["SPY"], fake))     # seq=0 拉
    asyncio.run(svc.refresh_once(lambda: ["SPY"], fake))     # seq=1 不拉
    assert fake.net_gex_calls == ["SPY"], "第二轮不该再打 spot-exposures"
    assert svc.snapshot()["SPY"]["net_gex"] == "neg", "符号被抹掉了"


def test_refresh_once_ampGEX拉失败时沿用旧符号():
    ok = _FakeUw({}, net_gex={"SPY": "neg"})
    asyncio.run(svc.refresh_once(lambda: ["SPY"], ok))
    bad = _FakeUw({}, net_gex={"SPY": "raise"})
    svc._ROUND_SEQ = 0                                        # 让它再次落在拉取轮
    asyncio.run(svc.refresh_once(lambda: ["SPY"], bad))
    snap = svc.snapshot()
    assert snap["SPY"]["net_gex"] == "neg"                    # 旧符号还在
    assert snap["SPY"]["stale"] is False                      # 墙位本身是新的，不算 stale


def test_refresh_once_ampGEX传了缩短的缓存上限():
    """默认 300s 缓存 > 实际间隔 60s，会让符号永不更新。"""
    fake = _FakeUw({})
    asyncio.run(svc.refresh_once(lambda: ["SPY"], fake))
    assert fake.net_gex_max_age_seen == [svc._NET_GEX_MAX_AGE_S]
    assert svc._NET_GEX_MAX_AGE_S < svc._WALL_POLL_INTERVAL_S * svc._NET_GEX_EVERY_N_ROUNDS


def test_refresh_once_只有符号翻号也要涨generation(monkeypatch):
    """墙位一动不动、只有 ±GEX 翻号时，generation 必须涨，否则前端永远看不到。"""
    monkeypatch.setattr(svc, "_NET_GEX_EVERY_N_ROUNDS", 1)
    a = _FakeUw({}, net_gex={"SPY": "pos"})
    asyncio.run(svc.refresh_once(lambda: ["SPY"], a))
    g1 = svc.generation()
    b = _FakeUw({}, net_gex={"SPY": "neg"})
    asyncio.run(svc.refresh_once(lambda: ["SPY"], b))
    assert svc.snapshot()["SPY"]["net_gex"] == "neg"
    assert svc.generation() == g1 + 1


def test_refresh_once_符号没变则不涨generation(monkeypatch):
    """反向守卫：别把「每轮强推一帧」当成修好了。"""
    monkeypatch.setattr(svc, "_NET_GEX_EVERY_N_ROUNDS", 1)
    fake = _FakeUw({}, net_gex={"SPY": "pos"})
    asyncio.run(svc.refresh_once(lambda: ["SPY"], fake))
    g1 = svc.generation()
    asyncio.run(svc.refresh_once(lambda: ["SPY"], fake))
    assert svc.generation() == g1
