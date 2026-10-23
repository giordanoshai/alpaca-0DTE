# -*- coding: utf-8 -*-
"""Alpaca endpoint 覆盖：让 tt_trading 能连本地 mock 服务器。

trading_test 的 README 一直教用户配 ALPACA_END_POINT，但 client.py 从来没读过它
——三个 SDK 客户端都硬连真实 Alpaca，配了也没用（2026-07-25 审查发现）。
"""
import pytest

import app.integrations.alpaca.client as c


def test_returns_none_when_not_configured(monkeypatch):
    """没配 → None → 走真实 Alpaca，生产行为一个字不变。"""
    monkeypatch.setattr(c, "_read_env", lambda: {})
    assert c.endpoint_override() is None


def test_reads_endpoint_from_env(monkeypatch):
    monkeypatch.setattr(c, "_read_env", lambda: {"ALPACA_END_POINT": "http://127.0.0.1:9000"})
    assert c.endpoint_override() == "http://127.0.0.1:9000"


def test_strips_trailing_slash(monkeypatch):
    monkeypatch.setattr(c, "_read_env", lambda: {"ALPACA_END_POINT": "http://127.0.0.1:9000/"})
    assert c.endpoint_override() == "http://127.0.0.1:9000"


def test_strips_v2_suffix(monkeypatch):
    """README 教的是写到 /v2，但 SDK 的 url_override 要的是根地址，会自己拼版本段。"""
    monkeypatch.setattr(c, "_read_env", lambda: {"ALPACA_END_POINT": "http://127.0.0.1:9000/v2"})
    assert c.endpoint_override() == "http://127.0.0.1:9000"


def test_blank_value_treated_as_unset(monkeypatch):
    monkeypatch.setattr(c, "_read_env", lambda: {"ALPACA_END_POINT": "   "})
    assert c.endpoint_override() is None


def test_derives_local_trading_stream_from_rest_mock(monkeypatch):
    monkeypatch.setattr(c, "_read_env", lambda: {
        "ALPACA_END_POINT": "http://127.0.0.1:9000/v2",
    })
    assert c.trading_stream_endpoint_override() == "ws://127.0.0.1:9000/stream"
    assert c.is_loopback_url(c.trading_stream_endpoint_override()) is True
    assert c.option_stream_endpoint_override() == (
        "ws://127.0.0.1:9000/v1beta1/indicative"
    )


def test_no_trading_stream_override_without_mock(monkeypatch):
    monkeypatch.setattr(c, "_read_env", lambda: {})
    assert c.trading_stream_endpoint_override() is None


def test_apply_proxy_sets_no_proxy_for_localhost(monkeypatch):
    """apply_proxy() 注入代理时同时设置 NO_PROXY，确保本机 mock 绕过代理。

    当 .env 有 HTTP_PROXY 时，requests 库默认会把所有请求（包括 127.0.0.1）转发到代理。
    设置 NO_PROXY=127.0.0.1,localhost 可让本机请求绕过代理，从而连接本地 mock 服务器。
    """
    import os
    # 隔离环境变量，避免污染测试进程
    monkeypatch.setattr(c, "_read_env", lambda: {"HTTP_PROXY": "http://proxy.example.com:8080"})
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)

    c.apply_proxy()

    # 验证 NO_PROXY 和 no_proxy 都被设置且包含 127.0.0.1 和 localhost
    no_proxy_upper = os.environ.get("NO_PROXY", "")
    no_proxy_lower = os.environ.get("no_proxy", "")

    assert "127.0.0.1" in no_proxy_upper, f"NO_PROXY 缺少 127.0.0.1: {no_proxy_upper}"
    assert "localhost" in no_proxy_upper, f"NO_PROXY 缺少 localhost: {no_proxy_upper}"
    assert "127.0.0.1" in no_proxy_lower, f"no_proxy 缺少 127.0.0.1: {no_proxy_lower}"
    assert "localhost" in no_proxy_lower, f"no_proxy 缺少 localhost: {no_proxy_lower}"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("127.0.0.1:1080", "socks5h://127.0.0.1:1080"),
        ("socks5://127.0.0.1:1080", "socks5h://127.0.0.1:1080"),
        ("socks5h://127.0.0.1:1080", "socks5h://127.0.0.1:1080"),
        ("  socks5h://proxy.local:9000  ", "socks5h://proxy.local:9000"),
    ],
)
def test_normalize_proxy_url_forces_remote_dns(raw, expected):
    assert c.normalize_proxy_url(raw) == expected


def test_normalize_proxy_url_rejects_http_proxy():
    with pytest.raises(ValueError, match="SOCKS5"):
        c.normalize_proxy_url("http://127.0.0.1:1080")


def test_apply_proxy_overwrites_existing_proxy_with_alpaca_socks5(monkeypatch):
    monkeypatch.setattr(c, "_read_env", lambda: {"ALPACA_PROXY_URL": "127.0.0.1:1080"})
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        monkeypatch.setenv(var, "http://old-proxy.invalid:8080")

    assert c.apply_proxy() == "socks5h://127.0.0.1:1080"
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        assert c.os.environ[var] == "socks5h://127.0.0.1:1080"


def test_alpaca_proxy_url_falls_back_to_legacy_http_proxy_key(monkeypatch):
    monkeypatch.delenv("ALPACA_PROXY_URL", raising=False)
    monkeypatch.setattr(c, "_read_env", lambda: {"HTTP_PROXY": "127.0.0.1:1080"})
    assert c.alpaca_proxy_url() == "socks5h://127.0.0.1:1080"
