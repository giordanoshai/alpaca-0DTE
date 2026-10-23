"""BuyIntentTracker：create/get/resolve 基本行为（execute_submit/execute_run 的支撑数据结构）。"""
from app.order_state import BuyIntentTracker


def test_create_returns_processing_intent():
    t = BuyIntentTracker()
    it = t.create("qqq", "long")
    assert it.ticker == "QQQ"          # 统一大写
    assert it.status == "processing"
    assert it.reason is None and it.result is None
    assert t.get(it.intent_id) is it


def test_resolve_updates_status_reason_result():
    t = BuyIntentTracker()
    it = t.create("QQQ", "long")
    t.resolve(it.intent_id, status="rejected", reason="开盘冷却中", result={"executed": False})
    assert it.status == "rejected"
    assert it.reason == "开盘冷却中"
    assert it.result == {"executed": False}


def test_resolve_unknown_id_is_noop():
    t = BuyIntentTracker()
    t.resolve("nope", status="error", reason="x")  # 不应抛错


def test_get_unknown_returns_none():
    t = BuyIntentTracker()
    assert t.get("nope") is None


def test_to_dict_shape():
    t = BuyIntentTracker()
    it = t.create("QQQ", "long")
    d = it.to_dict()
    assert set(d) == {"intent_id", "ticker", "direction", "status", "reason", "result"}
