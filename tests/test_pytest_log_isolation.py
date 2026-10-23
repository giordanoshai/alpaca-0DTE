"""pytest 不得与运行程序共写 log/ttt.log。"""
import os

from app.core import dailylogger


def test_test_process_disables_physical_runtime_log():
    assert os.environ.get("TT_DISABLE_FILE_LOG") == "1"
    assert dailylogger._shared_file_handler is None
