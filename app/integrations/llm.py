# -*- coding: utf-8 -*-
"""Anthropic 兼容格式的模型端点封装（翻译/分档用）。

**本层只管发请求收文本，不认识"新闻"** —— 业务判断全在 `app/services/` 里。

端点特性（2026-09-05 实测，Qwen3.8-Flash-Next 经 Anthropic 兼容层）：

- **慢**：极短请求 12.5s、单条翻译 15.0s、8 条一批 65.6s。默认超时因此给到 180s，
  远大于常规 HTTP 客户端 —— 调用方必须放在后台协程里，绝不能挂在请求路径上。
- **输出前有一段看不见的思考**：8 条输入才 381 token，输出却顶满 1500 并把 JSON
  写截断了。所以 `max_tokens` 要按每条 250 给，且解析必须容忍残缺（见
  `parse_json_array`）。
- **出口只认 `LLM_PROXY` 一个来源，绝不跟着进程环境变量走**（`trust_env=False`）。
  端点按**客户端 IP** 限流（`ip_rate_limit_exceeded`），所以「从哪儿出去」直接决定
  能不能用。2026-09-10 实测同一时刻三条出口：HTTP 代理 1081 → 200；SOCKS 代理
  1080 → 429；服务器直连 → 429。踩过的坑见 `complete()` 里那段注释。
"""
from __future__ import annotations

import json
import re

import requests

from app.config import env

LLM_ENDPOINT = env("LLM_ENDPOINT", "")
LLM_API_KEY = env("LLM_API_KEY", "")
LLM_MODEL = env("LLM_MODEL", "")

#: 这一层走哪条线出去。**空 = 明确直连**，不是「交给环境变量决定」。
#: 单独一个键而不是复用 `HTTP_PROXY`：那个键是给行情源用的，两件事要能分开调——
#: 端点按客户端 IP 限流，而各条出口的额度是各算各的。
LLM_PROXY = env("LLM_PROXY", "")

# 8 条一批实测 65.6s。低于这个数批量必然全军覆没，留足余量。
_DEFAULT_TIMEOUT = 180.0


class LlmUnavailable(Exception):
    """模型端点不可用。带上状态码，让调用方能区分「限流，等下再来」和「配置错了」。"""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class LlmClient:
    def __init__(self, endpoint: str | None = None, api_key: str | None = None,
                 model: str | None = None, timeout: float = _DEFAULT_TIMEOUT,
                 proxy: str | None = None):
        self._endpoint = endpoint if endpoint is not None else LLM_ENDPOINT
        self._key = api_key if api_key is not None else LLM_API_KEY
        self._model = model if model is not None else LLM_MODEL
        self.timeout = timeout
        self._proxy = proxy if proxy is not None else LLM_PROXY

    def complete(self, prompt: str, max_tokens: int = 1024) -> str:
        """发一轮对话，返回拼好的文本。失败一律抛 LlmUnavailable。"""
        # 没配就别发 —— 空 key 打过去只是白等一个 401，还占着超时。
        key = self._key if self._key is not None else LLM_API_KEY
        endpoint = self._endpoint or LLM_ENDPOINT
        model = self._model or LLM_MODEL
        if not key:
            raise LlmUnavailable("缺少 LLM_API_KEY")
        if not endpoint:
            raise LlmUnavailable("缺少 LLM_ENDPOINT")

        headers = {
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        proxy = self._proxy if self._proxy is not None else LLM_PROXY
        proxies = {"http": proxy, "https": proxy} if proxy else None
        try:
            # **必须走 Session 并关掉 trust_env，`requests.post()` 做不到这件事。**
            #
            # 2026-09-10 的「快讯全变英文」就出在这里：进程里继承着
            # `https_proxy=http://127.0.0.1:1081`，而 Alpaca 那一侧的 `apply_proxy()`
            # 会在**运行期**把四个代理变量整个改写成 `socks5h://127.0.0.1:1080`
            # （构造 SDK 客户端前调用，全进程生效）。requests 默认 `trust_env=True`，
            # 于是这一层的请求跟着别人设的变量走，落在一条正在挨端点按 IP 限流的
            # 出口上——每次调用 0.3 秒被 429 拒掉，整批翻译降级成英文原文，而日志
            # 只说「模型不可用」，从任何地方都看不出是「因为你走了 Alpaca 的代理」。
            #
            # **关 trust_env 和显式传 proxies 缺一不可**：只显式传能压过环境变量
            # （requests 对环境代理用的是 setdefault），但**想直连时传的是 None**，
            # 那一刻环境变量又会重新生效——而「直连」正是这一层的默认。
            with requests.Session() as session:
                session.trust_env = False
                response = session.post(
                    endpoint, headers=headers, proxies=proxies, timeout=self.timeout,
                    json={
                        "model": model,
                        "max_tokens": max_tokens,
                        "messages": [{"role": "user", "content": prompt}],
                    },
                )
        except Exception as e:  # noqa: BLE001 —— 超时/连接错都归一成同一个异常
            raise LlmUnavailable(f"请求失败: {type(e).__name__}: {e}") from e

        if response.status_code >= 400:
            raise LlmUnavailable(
                f"HTTP {response.status_code}: {response.text[:200]}",
                status_code=response.status_code,
            )

        payload = response.json() or {}
        return "".join(
            block.get("text", "")
            for block in payload.get("content", [])
            if isinstance(block, dict)
        )


_OBJ_RE = re.compile(r"\{[^{}]*\}")


def parse_json_array(text: str) -> list[dict]:
    """从模型输出里抠出一个 JSON 对象数组，**容忍截断和前后废话**。

    为什么不能直接 `json.loads`：这个模型 ① 爱在 JSON 前后加解释（即使要求只输出
    JSON）② token 用完就把数组写断在半途，末尾既没有 `}` 也没有 `]`。实测 8 条批量
    在 max_tokens=1500 下只写到第 5 条。

    策略：先试完整解析，失败则逐个捞出配平的 `{...}` 片段 —— 写完的那些照常用，
    半截的那条丢掉。丢一条远好过整批作废。
    """
    if not text:
        return []

    start, end = text.find("["), text.rfind("]")
    if start >= 0 and end > start:
        try:
            parsed = json.loads(text[start:end + 1])
            if isinstance(parsed, list):
                return [x for x in parsed if isinstance(x, dict)]
        except json.JSONDecodeError:
            pass  # 落到下面逐个捞

    out: list[dict] = []
    for chunk in _OBJ_RE.findall(text[start:] if start >= 0 else text):
        try:
            obj = json.loads(chunk)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out
