"""uvicorn 监听地址与端口的配置读取。

本机开发保持 `127.0.0.1:8002` 不变，部署到服务器时靠 `.env` 里的 TT_HOST /
TT_PORT 覆盖成 `0.0.0.0:8010`——目的是别为了部署维护两份代码。

端口写错（空串、非数字、超出范围）时回退默认值而不是抛异常：这两个值只在
`app/main.py` 的 `__main__` 里用一次，抛出来就是「服务起不来且只有一行
ValueError」，回退加告警能让人看见自己写错了，服务还照常在默认端口上。
"""
from __future__ import annotations

import pytest

from app.config import server_bind


def test_defaults_to_local_dev_bind(monkeypatch):
    monkeypatch.delenv("TT_HOST", raising=False)
    monkeypatch.delenv("TT_PORT", raising=False)
    assert server_bind() == ("127.0.0.1", 8002)


def test_env_overrides_to_server_bind(monkeypatch):
    monkeypatch.setenv("TT_HOST", "0.0.0.0")
    monkeypatch.setenv("TT_PORT", "8010")
    assert server_bind() == ("0.0.0.0", 8010)


def test_host_and_port_override_independently(monkeypatch):
    """只配一个也要生效——服务器上可能只想换端口不换绑定地址。"""
    monkeypatch.delenv("TT_HOST", raising=False)
    monkeypatch.setenv("TT_PORT", "8011")
    assert server_bind() == ("127.0.0.1", 8011)


@pytest.mark.parametrize("bad", ["", "   ", "abc", "80.5", "0", "-1", "65536"])
def test_invalid_port_falls_back_to_default(monkeypatch, bad):
    monkeypatch.delenv("TT_HOST", raising=False)
    monkeypatch.setenv("TT_PORT", bad)
    assert server_bind() == ("127.0.0.1", 8002)


def test_surrounding_whitespace_tolerated(monkeypatch):
    """`.env` 里手写容易带空格，别因此起不来。"""
    monkeypatch.setenv("TT_HOST", "  0.0.0.0  ")
    monkeypatch.setenv("TT_PORT", " 8010 ")
    assert server_bind() == ("0.0.0.0", 8010)
