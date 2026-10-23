# -*- coding: utf-8 -*-
"""Alpaca REST 必须带超时 —— 2026-08-25 watchdog 卡死 8 分 48 秒的根因。

现场（北京时间）：
  23:06:34.415  提交 QQQ 认沽 ×4
  23:06:34.551  券商已成交 1.24，订单流面板立刻显示「已成交」
  23:06:34~23:10:43  持仓总览里这笔仓**完全不存在**，看不到也砍不掉
  23:10:43.077  watchdog 恢复，仓才出现
  23:16:22      触发 −40% 止损，亏 192

watchdog 的心跳 `trade_rest_reconciled` 从 23:01:55 断到 23:10:43，
恢复时抛 `RemoteDisconnected`。历史日志里同样的「持仓还在、心跳静默 70 秒以上」共 13 次。

根因不在业务代码：alpaca-py 的 RESTClient 调
``self._session.request(method, url, **opts)`` 时 opts 里没有 timeout
（``alpaca/common/rest.py:195``），requests 拿到 ``timeout=None`` 就是无限期等；
请求又必须走 SOCKS5 代理，连接被挂住（不断开也不回数据）时只能干等对端掐断。
而 watchdog 主循环是 ``await asyncio.to_thread(_watchdog_tick, ...)``，
这一个线程卡住 = 整个 watchdog 停摆，止损/超时平仓/触墙检查全停。
"""
from __future__ import annotations

import pytest

import app.integrations.alpaca.client as client_mod


def _probe_timeout(sdk_client) -> object:
    """拦在 requests 的 send 上，取出这次请求真正生效的 timeout。

    不打桩 alpaca 内部：走的是 SDK 真实的 ``_request → _session.request`` 链路，
    这样「SDK 换了实现导致注入失效」也会被这条测试抓住。
    """
    captured: dict = {}

    def _fake_send(prepared, **kwargs):
        captured.update(kwargs)
        raise _Intercepted

    sdk_client._session.send = _fake_send
    with pytest.raises(_Intercepted):
        sdk_client.get("/account")
    assert captured, "没有拦到请求，SDK 的请求链路可能变了"
    return captured.get("timeout")


class _Intercepted(Exception):
    """只用于把请求拦在真正发出去之前。"""


@pytest.fixture()
def clients(monkeypatch):
    """一套隔离的客户端工厂：不读 .env、不设代理、不连 mock 端点。"""
    monkeypatch.setattr(client_mod, "read_keys", lambda: ("dummy-key", "dummy-secret"))
    monkeypatch.setattr(client_mod, "apply_proxy", lambda: "socks5h://127.0.0.1:1080")
    monkeypatch.setattr(client_mod, "endpoint_override", lambda: None)
    return client_mod.AlpacaClients()


def test_交易客户端必须带有限超时(clients):
    """下单/查单/查持仓走的就是这个客户端；它卡住 watchdog 就整个停摆。"""
    timeout = _probe_timeout(clients.trading())

    assert timeout is not None, (
        "Alpaca 交易 REST 没有超时 —— 连接被挂住时会无限期等，"
        "watchdog 一拍卡死则止损/超时平仓全部停摆（2026-08-25 卡了 8 分 48 秒）"
    )
    connect_s, read_s = timeout
    assert 0 < connect_s <= 10, f"连接超时不合理：{connect_s}"
    assert 0 < read_s <= 30, f"读超时不合理：{read_s}"


def test_行情客户端超时更宽但仍有上限(clients):
    """期权链/到期日**单发请求真的会跑到 16 秒**，按交易档的 15s 砍会让人开不了仓。

    实据：2026-08-21 META 一发 `chain_ms=16233`（`chain_calls=1`）、
    2026-08-24 AMD 一发 `expiry_ms=16217`。这一档在开仓选约路径上，必须放宽；
    但仍要有上限，否则又回到"永远等下去"。
    """
    _, trading_read = _probe_timeout(clients.trading())

    for name, sdk_client in (("stock", clients.stock()), ("option", clients.option())):
        timeout = _probe_timeout(sdk_client)
        assert timeout is not None, f"{name} 客户端没有超时"
        _, read_s = timeout
        assert read_s > 16.7, (
            f"{name} 读超时 {read_s}s 砍得比历史最慢的一发选约请求（16.7s）还短，会开不了仓"
        )
        assert read_s <= 60, f"{name} 读超时 {read_s}s 太长，等同于没有上限"
        assert read_s > trading_read, (
            "行情档必须比交易档宽：交易档是 watchdog 的命脉要收紧，"
            f"实际 行情 {read_s}s vs 交易 {trading_read}s"
        )


def test_调用方显式传的超时不被覆盖(clients):
    """注入的是**默认值**；individual 调用点若自己指定更长/更短，必须照它的来。"""
    sdk_client = clients.trading()
    captured: dict = {}

    def _fake_send(prepared, **kwargs):
        captured.update(kwargs)
        raise _Intercepted

    sdk_client._session.send = _fake_send
    with pytest.raises(_Intercepted):
        sdk_client._session.request("GET", "http://127.0.0.1:1/none", timeout=1.5)

    assert captured.get("timeout") == 1.5


def test_重复安装不叠加包装(clients):
    """工厂是惰性单例，同一个 session 可能被多次经手；包装必须幂等。"""
    sdk_client = clients.trading()
    first = sdk_client._session.request

    client_mod.install_rest_timeout(sdk_client)
    client_mod.install_rest_timeout(sdk_client)

    assert sdk_client._session.request is first, "重复安装把 request 又包了一层"
