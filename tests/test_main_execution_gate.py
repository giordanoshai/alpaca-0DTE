from app.order_exec import _execution_enabled


def test_execution_enabled_for_watcher_and_live():
    assert _execution_enabled("watcher") is True
    assert _execution_enabled("live") is True


def test_execution_disabled_for_real():
    assert _execution_enabled("real") is False
