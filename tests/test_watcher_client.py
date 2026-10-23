from app.watcher_client import WatcherClient


def _client(now=100.0, timeout=15.0):
    c = WatcherClient()
    c._timeout = timeout
    c._clock = lambda: _client.t  # 可控时钟
    _client.t = now
    return c


def test_update_message_caches_state_and_running():
    c = _client()
    c._handle_message({"type": "update", "ticker": "SPY",
                       "data": {"running": True, "trend_score": {"direction": "up"}}})
    assert c.states["SPY"]["trend_score"]["direction"] == "up"
    assert c._running is True


def test_signal_message_records_last_signal_ts():
    c = _client()
    c._handle_message({"type": "signal", "ticker": "QQQ", "data": {}})
    assert "QQQ" in c.last_signal_ts
    c._handle_message({"type": "signal_event", "ticker": "IWM", "data": {}})
    assert "IWM" in c.last_signal_ts


def test_signal_online_requires_connected_running_and_fresh():
    c = _client(now=100.0, timeout=15.0)
    c._ws_connected = True
    c._running = True
    c._last_msg_ts = 100.0
    assert c.signal_online is True
    # 消息超时 → 离线
    _client.t = 120.0
    assert c.signal_online is False
    # 恢复新鲜但断连 → 离线
    _client.t = 100.0
    c._ws_connected = False
    assert c.signal_online is False
    # 连着但 watcher 未 running → 离线
    c._ws_connected = True
    c._running = False
    assert c.signal_online is False


def test_next_backoff_doubles_and_caps_at_10():
    c = WatcherClient()
    assert c._next_backoff(1.0) == 2.0
    assert c._next_backoff(6.0) == 10.0
    assert c._next_backoff(10.0) == 10.0
