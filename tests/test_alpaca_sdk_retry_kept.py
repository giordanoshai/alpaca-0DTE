# -*- coding: utf-8 -*-
"""SDK 自带的 429 重试必须留着 —— 它是目前唯一压得住请求速率的东西。

2026-08-14 当天立、当天推翻、当天删的一段历史，写在这里防止再来一次。

**诊断没错**：08-13 夜盘 `contract_ms` 的 3494 / 3504 / 3532ms 三个样本聚在 38ms
带宽内，确实是 alpaca-py 命中 429 后 `time.sleep(3)`（0.43.5 `common/rest.py`：
`DEFAULT_RETRY_ATTEMPTS=3` / `DEFAULT_RETRY_WAIT_SECONDS=3` / `[429, 504]`）。

**结论完全错了**：那 3 秒不是病，是药。把三个客户端的 `_retry / _retry_wait` 置 0 后——

    08-13（SDK 重试在） 429 合计 ~3 次/天   → 3 个 3.5s 尖峰
    08-14（关掉之后）   429 **280 次/分钟**，连续 41 分钟（合计 11,055）

本层退避 `data.py:_RATE_LIMIT_BACKOFF_S = (0.25, 0.5)` 比 SDK 的 3s 快一个量级，
退完立刻再打 → 429 → 再退 0.25s，正反馈锁死。当晚三笔代价：

  1. **10 笔下单被 429 拒掉**（`rule=datasource`、`reason=too many requests`，
     SMCI/QQQ/INTC/IREN，21:42–22:49 整晚）；
  2. **幽灵仓**：`atomic_close` 在「撤单已发出、部分成交尚未记账」那一瞬被 429 抛断
     （`watchdog_exit.py:416` 捕获 → 保护态锁存），本地少记 1 张、券商实际 0 张，
     永久不一致 → 每秒一条 `trade_watchdog_result`「无可平仓数量」直到重启
     （`INTC260814P00104000`，22:32:24–22:33:14，46 条）；
  3. 尖峰确实没了，因为退避没了。

**要拿掉它，先做完这两件事**（见 docs/reports/2026-08-14-当日改动审核与后续计划.md）：
  P0 把请求量降下来（`contract_preview` 全池轮询、单次最多 5 段串行扩宽），实测 429 归零；
  P1 `trading.py` 按幂等性分层退避（只读/撤单可重试、`submit_order` 一次都不许）。
顺序不能反 —— 08-14 就是反着来的。

同日另一处决定：`_harden()` 里那个 `(5.0, 10.0)` 会话挂起兜底超时也一并删除，
`client.py` 回到 `6a4600b` 原样。它当天从未触发过，且 `requests.Timeout` 不在 SDK 的
`[429, 504]` 里、不会被重试，落点与上面的 429 完全相同。代价是「卡住的调用永久占住
一个 `to_thread` 工作线程」这种故障重新失去防护 —— 正解在 P2（让 `atomic_close`
的异常路径不再产生幽灵仓），而不是在客户端层加超时。

---

## 2026-08-25 更新：超时**加回来了**，关重试**仍然禁止**

上面那句「代价」当晚就应验了，而且不止一次：`watchdog_loop` 是
`await asyncio.to_thread(_watchdog_tick, ...)` 串行一个线程，一发查持仓的 REST 卡住
= 整个 watchdog 停摆。08-25 卡了 **8 分 48 秒**（23:01:55 → 23:10:43，恢复时抛
`RemoteDisconnected`）；期间一笔 23:06:34.551 就已在券商成交的 QQQ 认沽在持仓总览里
空窗 4 分 09 秒 —— 用户看不到也砍不掉。翻全部历史日志，同形态的静默共 **13 次**，
最长 55 分钟（08-10 22:11→23:06）。这段窗口里止损、超时平仓、触墙检查全部停摆。

**加回来的前提都满足了**，逐条对上面那段：

1. **致命的那半没有碰**：`_retry` / `_retry_wait` 一个字没动，本文件前两条断言照旧。
   08-14 的教训是「关重试」，不是「加超时」—— 两件事当时被打包在一个 `_harden()` 里，
   删的时候也就一起删了。
2. **幽灵仓这条已被 P2 覆盖**：2026-08-14 盘中落地的
   `watchdog_exit.execute_latched_risk_exit` 收尾解锁，对异常来自 429 还是超时一视同仁。
   `docs/reports/2026-08-14-当日改动审核与后续计划.md` §三原文即写着
   「真要重新加超时，先读 `tests/test_alpaca_sdk_retry_kept.py`」—— 这就是那次阅读。
3. **429 早已不是活跃问题**：08-15 起至 08-25 共 11 天，`alpaca_rate_limited` **零条**。
4. **不再用统一档，避免 08-10 那次「收紧把慢调用变成拒单」**：
   交易档 `(5s, 15s)` 只盖下单/撤单/查单/查持仓（实测最慢 1.29s，余量 11 倍）；
   行情档 `(5s, 45s)` 盖期权链/到期日/批量报价 —— 单发请求真的会跑到 16 秒
   （08-21 META `chain_ms=16233` 且 `chain_calls=1`、08-24 AMD `expiry_ms=16217`），
   旧的统一 10s 会把开仓选约直接砍掉。两档见
   `tests/test_alpaca_rest_timeout.py`。

**所以现在的红线只剩一条**：可以加超时，**不得顺手关掉 SDK 重试**。
下次再想动这块，先把上面两段历史读完。
"""
from __future__ import annotations

import pytest

import app.integrations.alpaca.client as c


@pytest.fixture()
def clients(monkeypatch):
    """隔离出一份新的工厂：不碰全局 CLIENTS，也不真的改进程环境变量。"""
    monkeypatch.setattr(c, "read_keys", lambda: ("dummy-key", "dummy-secret"))
    monkeypatch.setattr(c, "apply_proxy", lambda: "socks5h://127.0.0.1:1080")
    monkeypatch.setattr(c, "endpoint_override", lambda: None)
    return c.AlpacaClients()


@pytest.mark.parametrize("factory", ["trading", "stock", "option"])
def test_不得关闭SDK自带重试(factory, clients):
    """本文件顶部那三笔代价就是这条断言的来历。改动前请先读完。"""
    client = getattr(clients, factory)()

    assert getattr(client, "_retry", 0) > 0, (
        f"{factory} 客户端的 SDK 重试被关掉了 —— 429 会从 3 次/天涨到 280 次/分钟"
    )
    assert getattr(client, "_retry_wait", 0) > 0, (
        "3 秒退避是唯一在起作用的限速器，不是 contract_ms 尖峰的病因"
    )


def test_真实SDK的重试默认值仍是可依赖的():
    """靠的是 SDK 的默认值，就得盯着它 —— 升级把默认关掉了，这里要响。"""
    from alpaca.data.historical.option import OptionHistoricalDataClient

    real = OptionHistoricalDataClient("dummy-key", "dummy-secret")

    assert getattr(real, "_retry", 0) > 0, "SDK 默认重试没了，本层退避必须同步加大"
    assert getattr(real, "_retry_wait", 0) > 0


@pytest.mark.parametrize("factory", ["trading", "stock", "option"])
def test_加超时不得顺带关重试(factory, clients):
    """2026-08-25 把挂起超时加回来了；这条盯住它别再把重试一起带走。

    08-14 的 `_harden()` 把「关重试」和「加超时」打包在一起，删的时候也一起删了。
    现在只保留后者：`install_rest_timeout()` **只碰 session.request 的默认 timeout**，
    不得触碰 `_retry` / `_retry_wait`。详见本文件顶部 2026-08-25 那一节。
    """
    client = getattr(clients, factory)()

    assert c.install_rest_timeout(client) is False, (
        f"{factory} 客户端出厂时没装上超时 —— 工厂里漏了 install_rest_timeout()"
    )
    assert getattr(client, "_retry", 0) > 0, "装超时的同时把 SDK 重试关了，先读本文件顶部"
    assert getattr(client, "_retry_wait", 0) > 0


def test_超时注入不得偷偷改动SDK重试默认值():
    """直接对真实 SDK 客户端装一次，确认它只动 timeout、不动别的。"""
    from alpaca.data.historical.option import OptionHistoricalDataClient

    real = OptionHistoricalDataClient("dummy-key", "dummy-secret")
    before = (real._retry, real._retry_wait, real._retry_codes)

    assert c.install_rest_timeout(real, kind="data") is True

    assert (real._retry, real._retry_wait, real._retry_codes) == before
