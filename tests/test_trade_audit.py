"""交易审计日志：字段可序列化、带数据环境；第三方成功请求不污染业务日志。"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from decimal import Decimal

from app import trade_recorder
from app.core import dailylogger


def test_audit_is_structured_and_adds_data_source(monkeypatch):
    messages: list[str] = []
    monkeypatch.setenv("DATA_SOURCE", "mock")
    monkeypatch.setattr(trade_recorder.audit_logger, "info", messages.append)

    trade_recorder.audit(
        "trade_order_submitted",
        order_id="order-1",
        price=Decimal("1.23"),
        at=datetime(2026, 7, 26, 14, 30, tzinfo=timezone.utc),
    )

    payload = json.loads(messages[0])
    assert payload == {
        "event": "trade_order_submitted",
        "data_source": "mock",
        "order_id": "order-1",
        "price": "1.23",
        "at": "2026-07-26 14:30:00+00:00",
        # 2026-08-04 日志地基：trade_id 未显式传入时回落 order_id。
        "trade_id": "order-1",
    }


def test_audit_logging_failure_never_breaks_trade(monkeypatch):
    monkeypatch.setattr(
        trade_recorder.audit_logger, "info",
        lambda *_: (_ for _ in ()).throw(RuntimeError("log disk full")),
    )
    assert trade_recorder.audit("trade_test", order_id="order-1") is None


def test_setup_root_logger_suppresses_httpx_success_noise(tmp_path):
    httpx_logger = logging.getLogger("httpx")
    httpx_logger.setLevel(logging.INFO)

    dailylogger.setup_root_logger(str(tmp_path))

    assert httpx_logger.level == logging.WARNING
