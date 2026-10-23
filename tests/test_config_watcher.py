from app import config


def test_env_falls_back_to_default_when_missing():
    assert config.env("TT_NONEXISTENT_KEY_XYZ", "fallback") == "fallback"


def test_watcher_constants_present_and_typed():
    assert config.WATCHER_BASE_URL.startswith("http")
    assert config.WATCHER_WS_URL.startswith("ws")
    assert isinstance(config.WATCHER_SIGNAL_TIMEOUT_S, float)
    assert config.WATCHER_SIGNAL_TIMEOUT_S > 0
