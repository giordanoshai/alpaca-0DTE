# -*- coding: utf-8 -*-
"""模型端点客户端（app/integrations/llm.py）。

这一层只管把提示词发出去、把文本拿回来，不认识"新闻"。

两个必须钉住的现实（2026-09-05 实测那个端点得出）：
1. **慢**：极短请求 12.5s、单条翻译 15s、8 条一批 65s。超时值必须给足，且超时要抛成
   可识别的异常，让上层能降级而不是把整条链带崩。
2. **输出会被截断**：8 条批量在 max_tokens=1500 下只写到第 5 条，JSON 数组没闭合。
   这个模型输出前还有一段看不见的思考，token 消耗远超预期，所以截断是常态不是意外 ——
   解析必须能从残缺的数组里把已完整的对象捞出来。
"""
from __future__ import annotations

import json

import pytest

from app.integrations.llm import LlmClient, LlmUnavailable, parse_json_array


class _Resp:
    def __init__(self, status: int, payload=None, text: str = ""):
        self.status_code = status
        self._payload = payload
        self.text = text or json.dumps(payload or {})

    def json(self):
        return self._payload


def _ok(text: str) -> _Resp:
    return _Resp(200, {"content": [{"type": "text", "text": text}],
                       "usage": {"input_tokens": 10, "output_tokens": 5}})


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr("app.integrations.llm.LLM_ENDPOINT", "https://fake/v1/messages")
    monkeypatch.setattr("app.integrations.llm.LLM_API_KEY", "test-key")
    monkeypatch.setattr("app.integrations.llm.LLM_MODEL", "test-model")
    return LlmClient()


class _FakeSession:
    """替掉整个 Session，顺手把生产代码设的 trust_env 和 proxies 记下来。

    **不能只 stub `requests.post`**：关掉 `trust_env` 只有 Session 做得到，而那正
    是这次要钉住的东西（见「出口」那一节）——只 stub post 的话那条纪律在测试里根本
    走不到，测试全绿而线上照样跟着进程环境变量跑。
    """

    last: dict = {}
    handler = staticmethod(lambda url, **kw: None)

    def __init__(self):
        self.trust_env = True          # requests 的真实默认，好看出生产改没改

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def post(self, url, **kw):
        _FakeSession.last = {"url": url, "trust_env": self.trust_env, **kw}
        return _FakeSession.handler(url, **kw)


def _stub_post(monkeypatch, fn):
    _FakeSession.last = {}
    _FakeSession.handler = staticmethod(fn)
    monkeypatch.setattr("app.integrations.llm.requests.Session", _FakeSession)


def test_complete_returns_text(client, monkeypatch):
    _stub_post(monkeypatch, lambda *a, **k: _ok("美联储官员沃勒表示支持维持利率不变。"))
    assert client.complete("翻译", max_tokens=100) == "美联储官员沃勒表示支持维持利率不变。"


def test_request_carries_auth_and_model(client, monkeypatch):
    seen = {}

    def fake(url, **kw):
        seen["url"] = url
        seen.update(kw)
        return _ok("ok")

    _stub_post(monkeypatch, fake)
    client.complete("hi", max_tokens=64)

    assert seen["url"] == "https://fake/v1/messages"
    assert seen["headers"]["x-api-key"] == "test-key"
    assert seen["headers"]["anthropic-version"], "缺 anthropic-version 头，端点是 Anthropic 兼容格式"
    assert seen["json"]["model"] == "test-model"
    assert seen["json"]["max_tokens"] == 64


def test_missing_key_does_not_send_request(monkeypatch):
    """没配密钥就别发请求 —— 空 key 打过去只会白等一个 401。"""
    monkeypatch.setattr("app.integrations.llm.LLM_API_KEY", "")
    sent = []
    _stub_post(monkeypatch, lambda *a, **k: sent.append(1))

    with pytest.raises(LlmUnavailable):
        LlmClient().complete("hi", max_tokens=10)
    assert sent == []


def test_timeout_raises_llm_unavailable(client, monkeypatch):
    """端点单次要 15 秒，超时是常态。必须抛成上层认得的异常好走降级。"""
    import requests as _rq

    def boom(*a, **k):
        raise _rq.exceptions.ReadTimeout("timed out")

    _stub_post(monkeypatch, boom)
    with pytest.raises(LlmUnavailable):
        client.complete("hi", max_tokens=10)


def test_http_error_carries_status_code(client, monkeypatch):
    _stub_post(monkeypatch, lambda *a, **k: _Resp(429, text="rate limited"))
    with pytest.raises(LlmUnavailable) as e:
        client.complete("hi", max_tokens=10)
    assert e.value.status_code == 429


def test_timeout_is_generous_enough_for_batch(client):
    """8 条一批实测 65 秒。超时值低于这个数，批量必然全军覆没。"""
    assert client.timeout >= 90


# ---------------- 出口：走哪条线出去 ----------------

def test_不跟着进程里继承来的代理走(client, monkeypatch):
    """**这是 2026-09-10 那次「快讯全是英文」的根因。**

    进程里继承着 `https_proxy=http://127.0.0.1:1081`，而 Alpaca 那一侧的
    `apply_proxy()` 会在**运行期**把四个代理变量整个改写成 `socks5h://127.0.0.1:1080`
    （构造 SDK 客户端前调用，全进程生效）。requests 默认 `trust_env=True`，于是这一层
    的请求跟着别人设的变量走——当天实测那个出口正在挨端点的按 IP 限流（HTTP 429
    `ip_rate_limit_exceeded`），每次调用 0.3 秒被拒，翻译整批降级成英文原文。

    **本层的出口只能由本层的配置决定。** 别人在进程里设什么都不该影响它——那条链
    从任何界面、任何日志上都看不出来（日志只说「模型不可用」，不会说「因为你走了
    Alpaca 的代理」）。
    """
    _stub_post(monkeypatch, lambda url, **kw: _ok("ok"))
    client.complete("hi", max_tokens=10)
    assert _FakeSession.last["trust_env"] is False, \
        "必须关掉 trust_env，否则跟着进程里别人设的代理变量走"


def test_显式配了代理就走那个代理(client, monkeypatch):
    """**关掉 trust_env 和显式传代理，两件事缺一不可。**

    只关 trust_env 得到的是「铁定直连」——而当天实测服务器直连出口同样在挨限流
    （只有 1081 那条通），所以必须还能显式指定走哪儿。
    """
    _stub_post(monkeypatch, lambda url, **kw: _ok("ok"))
    LlmClient(endpoint="https://fake/v1/messages", api_key="k", model="m",
              proxy="http://127.0.0.1:1081").complete("hi", max_tokens=10)
    assert _FakeSession.last["proxies"] == {
        "http": "http://127.0.0.1:1081", "https": "http://127.0.0.1:1081"}
    assert _FakeSession.last["trust_env"] is False


def test_没配代理就是真的直连(client, monkeypatch):
    """没配 = 直连，而且是**明确的**直连，不是「交给环境变量决定」。

    这两者在代码里长得几乎一样（都不传 proxies），可结果完全不同——后者正是这次
    的 bug。
    """
    _stub_post(monkeypatch, lambda url, **kw: _ok("ok"))
    client.complete("hi", max_tokens=10)
    assert not _FakeSession.last.get("proxies")
    assert _FakeSession.last["trust_env"] is False


def test_代理默认从env读不用调用方每次传(monkeypatch):
    """`LlmClient()` 是无参构造的（`news_feed_service` 就这么用），配置只能自己读。

    要靠调用方记得传的话，漏一处就是「那一条路悄悄走了别人的代理」。
    """
    monkeypatch.setattr("app.integrations.llm.LLM_ENDPOINT", "https://fake/v1/messages")
    monkeypatch.setattr("app.integrations.llm.LLM_API_KEY", "k")
    monkeypatch.setattr("app.integrations.llm.LLM_MODEL", "m")
    monkeypatch.setattr("app.integrations.llm.LLM_PROXY", "http://127.0.0.1:1081")
    _stub_post(monkeypatch, lambda url, **kw: _ok("ok"))

    LlmClient().complete("hi", max_tokens=10)
    assert _FakeSession.last["proxies"]["https"] == "http://127.0.0.1:1081"


# ---------------- 截断容错 ----------------

def test_parse_json_array_plain():
    assert parse_json_array('[{"i":0,"cn":"甲"},{"i":1,"cn":"乙"}]') == [
        {"i": 0, "cn": "甲"}, {"i": 1, "cn": "乙"}]


def test_parse_json_array_ignores_surrounding_prose():
    """模型爱在前后加解释，即使要求只输出 JSON。"""
    out = parse_json_array('好的，结果如下：\n[{"i":0,"cn":"甲"}]\n希望有帮助')
    assert out == [{"i": 0, "cn": "甲"}]


def test_parse_json_array_recovers_truncated_tail():
    """实测现场：写到一半 token 用完，数组没闭合。已完整的那些必须捞回来。"""
    truncated = '[\n {"i":0,"cn":"日元趋势变化","lv":1},\n {"i":1,"cn":"特朗普称控制海峡","lv":2},\n {"i":2,"cn'
    out = parse_json_array(truncated)
    assert [x["i"] for x in out] == [0, 1]
    assert out[1]["lv"] == 2


def test_parse_json_array_returns_empty_on_garbage():
    assert parse_json_array("模型今天不想干活") == []
    assert parse_json_array("") == []
