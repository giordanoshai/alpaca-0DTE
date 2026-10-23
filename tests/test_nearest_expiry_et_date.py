"""nearest_expiry 必须按美东交易日筛合约，不能用本机日期。

本机在 UTC+8 时，美东下午盘（12:00–16:00 ET）正对应北京时间次日 00:00–04:00，
`dt.date.today()` 已经跳到次日。传给 `expiration_date_gte` 就把当天到期的
0DTE 合约整段过滤掉 —— 不报错，而是**静默选到次日到期的合约**，
希腊值与定价口径全偏（2026-08-15 代码审查 CRITICAL-1）。
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal as D
from types import SimpleNamespace

from app.integrations.alpaca.data import AlpacaData, ClockInfo


class FakeTradingClient:
    """按 expiration_date_gte 真实过滤，让「选错到期日」的后果显形。"""

    def __init__(self, expiries):
        self.expiries = list(expiries)
        self.requests = []

    def get_option_contracts(self, request):
        self.requests.append(request)
        gte = request.expiration_date_gte
        matched = [e for e in self.expiries if e >= gte]
        return SimpleNamespace(
            option_contracts=[SimpleNamespace(expiration_date=e) for e in matched]
        )


def test_nearest_expiry_uses_et_session_date_not_local_today(monkeypatch):
    # 构造「本机已跨天、美东仍是前一交易日」：不依赖测试实际运行日期。
    local_today = dt.date.today()
    et_session = local_today - dt.timedelta(days=1)

    data = AlpacaData(cache_ttl=20)
    client = FakeTradingClient([et_session, local_today])
    monkeypatch.setattr(data, "_tc", lambda: client)
    monkeypatch.setattr(data, "clock", lambda: ClockInfo(True, D("60"), et_session))

    result = data.nearest_expiry("QQQ")

    assert client.requests, "应当发起了一次期权合约查询"
    assert client.requests[0].expiration_date_gte == et_session, (
        "筛选下界必须是美东交易日；用本机日期会把当天到期的 0DTE 过滤掉"
    )
    assert result == et_session, "应当选中当天到期的 0DTE，而不是次日合约"
