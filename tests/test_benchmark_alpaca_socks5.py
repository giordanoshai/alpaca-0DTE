from __future__ import annotations

import pytest

from scripts.benchmark_alpaca_socks5 import (
    choose_symbols,
    percentile,
    socks5h_url,
    summarize_records,
)


def test_socks5h_url_validates_and_uses_remote_dns():
    assert socks5h_url("127.0.0.1", 1080) == "socks5h://127.0.0.1:1080"
    with pytest.raises(ValueError):
        socks5h_url("", 1080)
    with pytest.raises(ValueError):
        socks5h_url("127.0.0.1", 70000)


def test_percentile_interpolates():
    assert percentile([10.0, 20.0, 30.0, 40.0], 0.5) == 25.0
    assert percentile([10.0, 20.0, 30.0, 40.0], 0.95) == pytest.approx(38.5)


def test_summarize_records_reports_average_extremes_and_errors():
    records = [
        {"metric": "rest_chain", "ok": True, "latency_ms": 10.0},
        {"metric": "rest_chain", "ok": True, "latency_ms": 30.0},
        {"metric": "rest_chain", "ok": False, "latency_ms": None},
    ]
    row = summarize_records(records)["rest_chain"]
    assert row == {
        "success_count": 2,
        "error_count": 1,
        "average_ms": 20.0,
        "minimum_ms": 10.0,
        "maximum_ms": 30.0,
        "p50_ms": 20.0,
        "p95_ms": 29.0,
    }


def test_choose_symbols_prefers_strikes_nearest_range_center():
    symbols = [
        "SPXW260727C07200000",
        "SPXW260727P07240000",
        "SPXW260727C07250000",
        "SPXW260727P07250000",
        "SPXW260727C07300000",
    ]
    assert choose_symbols(symbols, 7200, 7300, count=3) == [
        "SPXW260727C07250000",
        "SPXW260727P07250000",
        "SPXW260727P07240000",
    ]
