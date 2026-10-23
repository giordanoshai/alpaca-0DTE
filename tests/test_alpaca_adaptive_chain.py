"""Alpaca 期权链只查已知方向，并按 0.5%→1%→2%→6% 自适应扩宽。"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal as D
from types import SimpleNamespace

from app.integrations.alpaca.data import AlpacaData


def _snap(*, bid="1.00", ask="1.02", delta="0.42"):
    return SimpleNamespace(
        latest_quote=SimpleNamespace(bid_price=D(bid), ask_price=D(ask)),
        greeks=SimpleNamespace(delta=None if delta is None else D(delta)),
    )


class FakeOptionClient:
    def __init__(self, chains):
        self.chains = list(chains)
        self.requests = []

    def get_option_chain(self, request):
        self.requests.append(request)
        index = min(len(self.requests) - 1, len(self.chains) - 1)
        return self.chains[index]


def _data(monkeypatch, chains):
    data = AlpacaData(cache_ttl=20)
    client = FakeOptionClient(chains)
    monkeypatch.setattr(data, "_oc", lambda: client)
    monkeypatch.setattr(data, "nearest_expiry", lambda _ticker: dt.date(2026, 7, 27))
    return data, client


def _width(request, price=500.0):
    return round((float(request.strike_price_lte) / price) - 1.0, 4)


def test_stops_at_half_percent_when_candidate_is_eligible(monkeypatch):
    chain = {"QQQ260727C00500000": _snap(delta="0.42")}
    data, client = _data(monkeypatch, [chain])

    result = data.contract_candidates(
        "QQQ",
        "long",
        D("500"),
        min_delta=D("0.40"),
        max_spread_pct=D("0.03"),
        max_contract_cost=D("600"),
        template_version="7",
    )

    assert [row[1] for row in result] == ["QQQ260727C00500000"]
    assert [_width(req) for req in client.requests] == [0.005]
    assert str(client.requests[0].type).lower().endswith("call")


def test_expands_until_an_eligible_candidate_exists(monkeypatch):
    too_low_delta = {"QQQ260727P00500000": _snap(delta="-0.30")}
    eligible = {"QQQ260727P00495000": _snap(delta="-0.41")}
    data, client = _data(monkeypatch, [too_low_delta, eligible])

    result = data.contract_candidates(
        "QQQ",
        "short",
        D("500"),
        min_delta=D("0.40"),
        max_spread_pct=D("0.03"),
        max_contract_cost=D("600"),
        template_version="7",
    )

    assert [row[1] for row in result] == ["QQQ260727P00495000"]
    assert [_width(req) for req in client.requests] == [0.005, 0.01]
    assert all(str(req.type).lower().endswith("put") for req in client.requests)


def test_spx_uses_spxw_option_root(monkeypatch):
    data, client = _data(monkeypatch, [{"SPXW260727C07250000": _snap()}])

    data.contract_candidates(
        "SPX",
        "long",
        D("7250"),
        min_delta=D("0.40"),
        max_spread_pct=D("0.03"),
        max_contract_cost=D("600"),
        template_version="7",
    )

    assert client.requests[0].underlying_symbol == "SPXW"


def test_template_version_is_part_of_candidate_cache_identity(monkeypatch):
    chain = {"QQQ260727C00500000": _snap()}
    data, client = _data(monkeypatch, [chain])
    kwargs = dict(
        ticker="QQQ",
        direction="long",
        price=D("500"),
        min_delta=D("0.40"),
        max_spread_pct=D("0.03"),
        max_contract_cost=D("600"),
    )

    data.contract_candidates(**kwargs, template_version="7")
    data.contract_candidates(**kwargs, template_version="7")
    data.contract_candidates(**kwargs, template_version="8")

    assert len(client.requests) == 2
