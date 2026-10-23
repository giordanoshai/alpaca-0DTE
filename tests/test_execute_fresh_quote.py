"""开仓必须用**提交这一刻**的盘口定价 —— 修「第一次点买入必被撤，第二次立刻成交」。

2026-08-03 实测 8 笔首单挂在过期 ask 上 5 秒未成被系统撤单，3.5 秒后重试挂高一档立即成交：

    SOFI  首单 ask 0.40 → 撤 → 重试 0.43 → 成交 0.41
    CRWV  首单 ask 4.31 → 撤 → 重试 4.45 → 成交 4.40
    IWM   首单 ask 0.75 → 撤 → 重试 0.80 → 成交 0.78

根因：`execute()` 用闸门评估时的期权链快照 `sel.bid/sel.ask` 定价，提交前不再取一次
实时报价。而中间还隔着 UW delta 与期权链拉取，0DTE 上一两秒就足以让 ask 抬走一整档。加仓 (`add_position`) 与卡片预览
(`contract_preview`) 本来就走 `option_quote(fresh=True)`，只有开仓漏了。

降级纪律：刷新失败**绝不拦下单**，回落选约快照 —— 报价接口抖动不该把入场掐掉。
"""
from decimal import Decimal
from types import SimpleNamespace

from fastapi.testclient import TestClient

import app.main as m
import app.exec_entry as exec_entry
import app.routers.orders as orders
from app.integrations.alpaca.data import SelectedContract
from app.order_gate import MarketContext
from app.services.contract_selection import contract_for_validation
from app.validator import ValidationResult

client = TestClient(m.app)

_CODE = "QQQ260716C00500000"


class _CapturingExec:
    """记录真正提交给券商的限价。"""

    submitted: list = []

    def position_qty(self, _symbol):
        """券商侧无仓。开仓前要核对持仓归属（防与 SOS/人工持同一合约）。"""
        return 0

    def submit(self, req):
        _CapturingExec.submitted.append(req)
        return {"id": "fake-order-fresh-quote", "status": "accepted"}


def _setup(monkeypatch, *, option_quote):
    """闸门放行、合约固定，只让报价来源可变。

    选约快照定在 bid/ask = 1.00/1.02（对应实盘里那份「过期的 ask」）。
    """
    # REGISTRY/JOURNAL 由 conftest 的 autouse fixture 逐用例清空，
    # 否则同一合约第二次进来会撞上 execute 的「在途/持仓去重」直接被拒。
    _CapturingExec.submitted = []
    sel = SelectedContract(_CODE, Decimal("1.00"), Decimal("1.02"),
                           delta=Decimal("0.47"), delta_source="uw")
    ctx = MarketContext(signal=None, account=None,
                        contract=contract_for_validation(sel), selected=sel)
    alpaca = SimpleNamespace(option_quote=option_quote)
    src = SimpleNamespace(name="watcher", alpaca=alpaca,
                          market_context=lambda *_: ctx)
    monkeypatch.setattr(orders, "current_source", lambda: src)
    monkeypatch.setattr(exec_entry, "evaluate_with_context",
                        lambda *a, **k: (ValidationResult(allowed=True, reason="", rule="", qty=1), ctx))
    monkeypatch.setattr(exec_entry, "AlpacaTrading", _CapturingExec)
    return src


def _execute():
    r = client.post("/api/execute",
                    json={"ticker": "QQQ", "direction": "long", "qty": 1})
    assert r.status_code == 200, r.text
    return r.json()


def _submitted_limit() -> Decimal:
    assert len(_CapturingExec.submitted) == 1
    return Decimal(str(_CapturingExec.submitted[-1].limit_price))


def test_limit_uses_refreshed_ask_not_stale_snapshot(monkeypatch):
    """盘口在评估之后抬到 1.10/1.12 → 必须挂 1.12，而不是快照里的 1.02。"""
    seen = []

    def quote(code, *, fresh=False):
        seen.append((code, fresh))
        return Decimal("1.10"), Decimal("1.12")

    _setup(monkeypatch, option_quote=quote)
    _execute()
    assert seen == [(_CODE, True)]        # 必须显式要最新价，不能吃缓存
    assert _submitted_limit() == Decimal("1.12")


def test_falls_back_to_snapshot_when_quote_unavailable(monkeypatch):
    """报价取不到 → 用选约快照下单，不得拒单。"""
    _setup(monkeypatch, option_quote=lambda code, *, fresh=False: None)
    body = _execute()
    assert body["executed"] is True
    assert _submitted_limit() == Decimal("1.02")


def test_falls_back_to_snapshot_when_quote_raises(monkeypatch):
    """报价接口抛异常 → 同样回落快照，入场链路不被掐断。"""
    def boom(code, *, fresh=False):
        raise RuntimeError("Alpaca 报价超时")

    _setup(monkeypatch, option_quote=boom)
    body = _execute()
    assert body["executed"] is True
    assert _submitted_limit() == Decimal("1.02")


def test_works_when_source_has_no_alpaca(monkeypatch):
    """非可执行档/桩数据源没有 alpaca 属性时不得抛错。"""
    _CapturingExec.submitted = []
    sel = SelectedContract(_CODE, Decimal("1.00"), Decimal("1.02"),
                           delta=Decimal("0.47"), delta_source="uw")
    ctx = MarketContext(signal=None, account=None,
                        contract=contract_for_validation(sel), selected=sel)
    src = SimpleNamespace(name="watcher", market_context=lambda *_: ctx)
    monkeypatch.setattr(orders, "current_source", lambda: src)
    monkeypatch.setattr(exec_entry, "evaluate_with_context",
                        lambda *a, **k: (ValidationResult(allowed=True, reason="", rule="", qty=1), ctx))
    monkeypatch.setattr(exec_entry, "AlpacaTrading", _CapturingExec)
    body = _execute()
    assert body["executed"] is True
    assert _submitted_limit() == Decimal("1.02")


def test_explicit_limit_price_is_not_overridden_by_refresh(monkeypatch):
    """用户手填限价时，刷新出的盘口不得改写他的价 —— price_type=limit 是显式指令。"""
    _setup(monkeypatch, option_quote=lambda code, *, fresh=False: (Decimal("1.10"), Decimal("1.12")))
    r = client.post("/api/execute", json={
        "ticker": "QQQ", "direction": "long", "qty": 1,
        "price_type": "limit", "limit_price": "0.90",
    })
    assert r.status_code == 200, r.text
    assert _submitted_limit() == Decimal("0.90")
