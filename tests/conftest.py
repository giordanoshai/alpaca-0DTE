"""全局测试隔离：
1) 模板存储（Supabase ttt_template_store 唯一真源）——测试进程不得触网/读写真实配置，
   每个测试拿到一条隔离的数据库配置记录。
2) 交易记录器落库（ttt_trades/ttt_intercept_log）——默认阻断 app.db.connect，防止跑通
   /api/execute 等全链路路由的测试（只 mock 了券商、没 mock trade_recorder）意外写穿生产库
   （2026-07-23 曾把假 order_id 写进 ttt_trades，造成撤不掉的幽灵待成交单，见 order_exec bug）。
"""
from __future__ import annotations

import os

# 必须在导入任何 app 模块前关闭物理文件 handler；pytest 的模拟错误只进测试终端，
# 不再污染运行程序使用的 log/ttt.log。
os.environ["TT_DISABLE_FILE_LOG"] = "1"

import pytest

import app.templates as tmpl
from app.services.account_state import invalidate_closed_trades_cache


@pytest.fixture(autouse=True)
def _isolate_watcher_pool(monkeypatch):
    """多数闸门测试需要一个已启动票池；具体空池/异常行为由专门测试自行覆盖。"""
    import app.watcher.watcher_manager as watcher_manager

    monkeypatch.setattr(
        watcher_manager.GLOBAL_WATCHER,
        "get_all_states",
        lambda: {"QQQ": {}, "IWM": {}, "SPY": {}},
    )


@pytest.fixture(autouse=True)
def _reset_closed_trades_cache():
    """成交历史本地缓存是模块级全局态 → 每个测试前后清空，防跨测试污染风控输入。"""
    invalidate_closed_trades_cache()
    yield
    invalidate_closed_trades_cache()


@pytest.fixture(autouse=True)
def _isolate_template_store(monkeypatch):
    seed = tmpl.Template(
        name="测试基准",
        max_daily_loss=tmpl.Decimal("150"), max_weekly_loss=tmpl.Decimal("300"),
        max_position_size=tmpl.Decimal("500"), opening_blackout_min=tmpl.Decimal("10"),
        min_delta=tmpl.Decimal("0.40"), max_spread_pct=tmpl.Decimal("0.03"),
        stop_pct=tmpl.Decimal("0.20"), max_loss_per_trade=tmpl.Decimal("80"),
        max_hold_mins=tmpl.Decimal("15"), cooldown_seconds=600,
    )
    now = "2026-01-01T00:00:00+00:00"
    rows = [{
        "id": 1, "name": seed.name, "params": seed.to_dict(), "reason": "测试基准",
        "status": "active", "created_at": now, "updated_at": now,
    }]
    monkeypatch.setattr(tmpl, "_db_load_records", lambda: rows)
    monkeypatch.setattr(tmpl, "_db_save_records", lambda records: True)
    monkeypatch.setattr(tmpl, "_db_delete_record", lambda template_id: True)
    monkeypatch.setattr(tmpl, "_STORE", None)
    yield
    tmpl._STORE = None


@pytest.fixture(autouse=True)
def _market_gate_open(monkeypatch):
    """把行情窗口默认钉成「开着」，并逐用例清掉它的缓存。

    两个都必须做：

    1. `market_gate` 会去够券商时钟（Alpaca `/v2/clock`）——**测试里绝不能真发
       网络请求**，慢且看网络脸色。
    2. 它的结果带 30 秒模块级缓存，而全套测试跑完才 14 秒 —— 不清的话**第一个
       用例的判断会决定后面所有用例**。2026-09-02 实测：两条 market_tide 用例
       因此挂掉，现象是明明模拟的是盘中 15:59，闸却是关的。

    绝大多数用例写的都是盘中场景，所以默认放行。要测非盘中的自己 monkeypatch
    掉 `_fetch_clock`（`tests/test_market_gate.py` 就是这么干的，它在用例体内
    覆盖，晚于本 fixture 生效，不受影响）。
    """
    from app.core import market_gate

    class _AlwaysOpen:
        is_open = True
        session_date = None

    market_gate.reset_cache()
    monkeypatch.setattr(market_gate, "_fetch_clock", _AlwaysOpen)
    yield
    market_gate.reset_cache()


@pytest.fixture(autouse=True)
def _isolate_env_file(monkeypatch):
    """隔离 config.env() 的 .env 文件回退：不隔离时 monkeypatch.delenv 删掉的键会被
    env() 从开发者本机 .env 重新读回（如 DATA_SOURCE=uw），导致测试起真实行情循环
    死锁挂起（2026-07-24 test_start_defaults_to_yahoo_without_data_source_env 实测）。
    测试内需要某配置时用 monkeypatch.setenv——os.environ 优先级高于文件回退。"""
    monkeypatch.setattr("app.config._read_env_file", lambda: {})


@pytest.fixture(autouse=True)
def _isolate_order_state():
    """持仓登记/订单流/买入受理号都是模块级单例（内存态）→ 每个测试前后清空。

    2026-07-25 起 validator 规则 8 会经 order_gate.existing_exposure 读 REGISTRY 算同标的
    敞口，于是**任何跑过下单链路的测试留下的仓，都会顶掉后面某个闸门测试的额度**
    （实测：test_execute_reuses_market_context 留下 $601 的 QQQ 仓 → test_gate 里一个
    本该放行的用例被判"同标的持仓合计超上限"，单独跑却过——典型的顺序相关假失败）。
    """
    from app.order_state import BUY_INTENTS, JOURNAL, REGISTRY
    from app.trade_update_stream import TRADE_UPDATE_STREAM
    persist_enabled = JOURNAL._persist_enabled
    JOURNAL._persist_enabled = False
    REGISTRY._pos.clear(); JOURNAL.clear(); BUY_INTENTS.clear()
    TRADE_UPDATE_STREAM.clear()
    TRADE_UPDATE_STREAM.connected = False
    yield
    REGISTRY._pos.clear(); JOURNAL.clear(); BUY_INTENTS.clear()
    JOURNAL._persist_enabled = persist_enabled
    TRADE_UPDATE_STREAM.clear()
    TRADE_UPDATE_STREAM.connected = False


@pytest.fixture(autouse=True)
def _isolate_trade_recorder_db(monkeypatch):
    """默认阻断 app.db.connect：trade_recorder 的 record_* 都是 best-effort（异常只告警、
    绝不抛），阻断后全部静默 no-op，测试串起真实路由链路也不会写生产库。需要真正断言写库
    行为的测试（tests/test_trade_recorder.py 的 wire fixture）在自己的测试体内再次
    monkeypatch app.db.connect 覆盖本 fixture 即可（同一 monkeypatch 对象，后设置的生效）。
    """
    def _blocked():
        raise RuntimeError("app.db.connect blocked in tests — see conftest._isolate_trade_recorder_db")
    monkeypatch.setattr("app.db.connect", _blocked)


@pytest.fixture(autouse=True)
def _isolate_default_test_credentials(monkeypatch):
    """确保在未配置本地私有 .env 的开源/CI 环境下，测试默认获得隔离的 Mock 凭据，不会因缺少本地真实 key 导致用例假失败。"""
    import app.services.gex_wall_service as gws
    monkeypatch.setattr(gws, "_UW_KEY", "mock-test-uw-key")

    import app.integrations.alpaca.client as alpaca_client
    orig_read_env = alpaca_client._read_env

    def _fallback_test_env():
        env = orig_read_env()
        if not env.get("ALAPCA_AKEY") and not env.get("ALPACA_AKEY") and not env.get("ACCESS_KEY"):
            env["ALAPCA_AKEY"] = "mock-alpaca-key"
        if not env.get("ALPACA_SKEY") and not env.get("ACCESS_SECERT"):
            env["ALPACA_SKEY"] = "mock-alpaca-secret"
        return env

    monkeypatch.setattr(alpaca_client, "_read_env", _fallback_test_env)
