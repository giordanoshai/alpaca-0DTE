"""代理配置的取值优先级与格式规范化。

现场：`.env` 写着 `HTTP_PROXY = 10.0.0.1:1081`，但系统里残留了一个
用户级环境变量 `HTTP_PROXY=http://127.0.0.1:1080`，于是 Yahoo 一直走系统那个、改
`.env` 毫无反应且没有任何日志——本文件把三条修复钉住：
1. `.env` 解析器要能读出键名带数字的 `SOCKS5_PROXY`（旧正则 `[A-Za-z_]+` 读不出）；
2. 代理键以 `.env` 文件为准（其余配置键仍是「进程环境变量优先」，mock 联调靠它）；
3. 拼 WS 代理地址时不得给已带 scheme 的地址再套一层 `http://`。
"""
import pytest

from app import config as cfg
from app.integrations.alpaca import client as ac
from app.watcher.quote_loops import QuoteLoopsMixin


def test_read_env_reads_key_with_digits(tmp_path, monkeypatch):
    """键名含数字的 SOCKS5_PROXY 必须能读出来。"""
    (tmp_path / ".env").write_text(
        "HTTP_PROXY = 10.0.0.1:1081\nSOCKS5_PROXY = 10.0.0.1:1080\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(ac, "_BASE", tmp_path)

    assert ac._read_env()["SOCKS5_PROXY"] == "10.0.0.1:1080"
    assert ac._read_env()["HTTP_PROXY"] == "10.0.0.1:1081"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("10.0.0.1:1081", "http://10.0.0.1:1081"),
        ("  10.0.0.1:1081  ", "http://10.0.0.1:1081"),
        ("http://10.0.0.1:1081", "http://10.0.0.1:1081"),
        ("socks5h://127.0.0.1:1080", "socks5h://127.0.0.1:1080"),
        ("", ""),
        ("   ", ""),
    ],
)
def test_normalize_http_proxy(raw, expected):
    """只给缺 scheme 的补 `http://`，已有 scheme 的原样返回（不能拼成 http://socks5h://…）。"""
    assert cfg.normalize_http_proxy(raw) == expected


def test_proxy_prefers_env_file_over_process_env(monkeypatch):
    """代理键反向优先：`.env` 文件压过系统环境变量。"""
    monkeypatch.setattr(cfg, "_read_env_file", lambda: {"HTTP_PROXY": "10.0.0.1:1081"})
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1080")

    assert cfg.proxy_setting("HTTP_PROXY") == "10.0.0.1:1081"


def test_proxy_falls_back_to_process_env_when_file_has_none(monkeypatch):
    """`.env` 没写代理时，仍然沿用系统环境变量。"""
    monkeypatch.setattr(cfg, "_read_env_file", lambda: {})
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1080")

    assert cfg.proxy_setting("HTTP_PROXY") == "http://127.0.0.1:1080"


def test_ws_proxy_ignores_alpaca_socks5_pollution(monkeypatch):
    """Alpaca 的 apply_proxy() 把全进程 HTTP_PROXY 改成 socks5h:// 后，

    Massive/UW 的 WS 代理地址不能被污染成 `http://socks5h://host:port`。
    """
    monkeypatch.setattr(cfg, "HTTP_PROXY", "10.0.0.1:1081")
    monkeypatch.setenv("HTTP_PROXY", "socks5h://127.0.0.1:1080")

    assert QuoteLoopsMixin()._get_proxy_url() == "http://10.0.0.1:1081"


def test_ws_proxy_returns_none_when_unconfigured(monkeypatch):
    """没配代理时返回 None，websockets.connect(proxy=None) 走直连。"""
    monkeypatch.setattr(cfg, "HTTP_PROXY", "")
    monkeypatch.delenv("HTTP_PROXY", raising=False)

    assert QuoteLoopsMixin()._get_proxy_url() is None
