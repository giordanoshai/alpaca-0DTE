import asyncio
import logging
import os
import re
from logging.handlers import TimedRotatingFileHandler

from app.core.event_stream import (EventFilter, JsonlEventHandler,
                                   RepeatSuppressFilter)

# 获取项目的根目录 (假设 dailylogger.py 在 app/core/ 目录下)
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_LOG_DIR = os.path.join(BASE_DIR, "log")


class QueueLogHandler(logging.Handler):
    """
    Custom handler to push logs to an asyncio queue for streaming.
    """

    def __init__(self, log_queue: asyncio.Queue):
        super().__init__()
        self.log_queue = log_queue

    def emit(self, record):
        try:
            msg = self.format(record)
            # Create a non-blocking put if loop is running, else ignore
            try:
                loop = asyncio.get_running_loop()
                if loop.is_running():
                    self.log_queue.put_nowait(msg)
            except (RuntimeError, asyncio.QueueFull):
                pass
        except Exception:
            self.handleError(record)


# 全局的日志队列，确保所有按模块生成的 Logger 都可以把日志推送到同一个 SSE 前端输出流
global_log_queue = asyncio.Queue()


def _set_daily_suffix(handler, ext: str) -> None:
    """给按天轮转的 handler 同时设置 ``suffix`` 与 ``extMatch``。

    **只改 suffix 不改 extMatch 是个隐蔽陷阱，必须成对设置。**

    `TimedRotatingFileHandler.getFilesToDelete()` 靠 ``extMatch.fullmatch()``
    找出旧文件来做 ``backupCount`` 剪枝，而 ``when='midnight'`` 的默认 extMatch 是
    ``(?<!\\d)\\d{4}-\\d{2}-\\d{2}(?!\\d)``：

        "2026-08-04"        fullmatch -> True    （默认后缀，剪得掉）
        "2026-08-04.log"    fullmatch -> False   （加了扩展名，剪不掉）
        "2026-08-04.jsonl"  fullmatch -> False   （同上）

    也就是说，一旦给 suffix 加了扩展名却没同步 extMatch，剪枝正则就对不上自己
    生成的文件名，``getFilesToDelete()`` 恒返回空 —— 轮转看起来一切正常（按天切、
    文件名也对），只有「超过 backupCount 天后清理」这一环静默失效，日志会一直堆到
    磁盘满。2026-08-05 实测发现 `ttt.log` 与 `ttt.events.jsonl` 都中了这个招。

    守卫见 `tests/test_log_rotation_pruning.py`。
    """
    handler.suffix = f"%Y-%m-%d.{ext}"
    handler.extMatch = re.compile(rf"^\d{{4}}-\d{{2}}-\d{{2}}\.{re.escape(ext)}$", re.ASCII)


# 全局共享 Handlers，防止多个 Logger 实例往同一个文件写入和切割时发生冲突
_shared_file_handler = None
_shared_console_handler = None
_shared_queue_handler = None
_shared_jsonl_handler = None
_shared_suppressor = None

def _get_shared_handlers(target_dir: str):
    global _shared_file_handler, _shared_console_handler, _shared_queue_handler
    global _shared_jsonl_handler, _shared_suppressor

    if _shared_console_handler is None:
        # 统一日志格式，标注日志来自哪个文件以及多少行代码
        formatter = logging.Formatter(
            '%(asctime)s - %(levelname)s - [%(name)s] - [%(filename)s:%(lineno)d] - %(message)s'
        )
        
        # 1. 物理文件 Handler (按天拆分，所有模块共用 ttt.log)
        disable_file = os.environ.get("TT_DISABLE_FILE_LOG", "").strip().lower() in {
            "1", "true", "yes", "on",
        }
        if not disable_file:
            os.makedirs(target_dir, exist_ok=True)
            log_filename = os.path.join(target_dir, "ttt.log")
            _shared_file_handler = TimedRotatingFileHandler(
                log_filename, when='midnight', interval=1, backupCount=30, encoding='utf-8'
            )
            _set_daily_suffix(_shared_file_handler, "log")
            _shared_file_handler.setFormatter(formatter)
        
        # 2. 控制台 Console Handler
        _shared_console_handler = logging.StreamHandler()
        _shared_console_handler.setFormatter(formatter)
        
        # 3. 前端流式响应 SSE Handler
        _shared_queue_handler = QueueLogHandler(global_log_queue)
        _shared_queue_handler.setFormatter(formatter)

        # 4. 机读事件流 Handler（一行一个 JSON，只收含 event 键的业务事件）
        if not disable_file:
            _shared_jsonl_handler = JsonlEventHandler(
                os.path.join(target_dir, "ttt.events.jsonl"),
                when='midnight', interval=1, backupCount=30, encoding='utf-8',
            )
            _set_daily_suffix(_shared_jsonl_handler, "jsonl")
            _shared_jsonl_handler.addFilter(EventFilter())

        # 刷屏抑制挂在 handler 上而非 logger 上：logger 的 filter 不作用于子 logger
        # 传播上来的记录，而业务日志全走 tt.trade / tt.gate 等子 logger。
        # 四个 handler 共享同一实例，配合 record._suppress_decision 保证只计数一次。
        _shared_suppressor = RepeatSuppressFilter()
        for handler in (_shared_file_handler, _shared_console_handler,
                        _shared_queue_handler, _shared_jsonl_handler):
            if handler is not None:
                handler.addFilter(_shared_suppressor)

    return (_shared_file_handler, _shared_console_handler,
            _shared_queue_handler, _shared_jsonl_handler)


def setup_root_logger(log_dir: str = None, level: int = logging.INFO):
    """
    配置根 Logger，使所有使用 standard logging.getLogger(...) 的模块均能写日志到 log/ 目录。
    """
    target_dir = log_dir or DEFAULT_LOG_DIR
    file_h, console_h, queue_h, jsonl_h = _get_shared_handlers(target_dir)

    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    if file_h is not None and file_h not in root_logger.handlers:
        root_logger.addHandler(file_h)
    if console_h not in root_logger.handlers:
        root_logger.addHandler(console_h)
    if queue_h not in root_logger.handlers:
        root_logger.addHandler(queue_h)
    if jsonl_h is not None and jsonl_h not in root_logger.handlers:
        root_logger.addHandler(jsonl_h)

    # uvicorn --reload 底层用 watchfiles 轮询文件变化，其 INFO 级 "N change(s)
    # detected" 日志本身没有独立 level，会沿用 root 的 INFO 继承并被写入
    # ttt.log / 前端日志流，与业务日志无关，压到 WARNING 屏蔽掉。
    logging.getLogger("watchfiles").setLevel(logging.WARNING)
    # httpx 会把每个成功的 Supabase REST 请求以 INFO 打出来（URL 还带 columns
    # 参数），量大且不能帮助交易复盘。仅保留 WARNING/ERROR；业务成败由各模块
    # 自己的结构化日志记录。
    logging.getLogger("httpx").setLevel(logging.WARNING)
    # uvicorn 的访问日志是每个 HTTP 请求一行。2026-08-28 起入口用 log_config=None
    # 启动（让 uvicorn 的启停与回溯沿用 root 的 handler 落进 ttt.log —— 在那之前
    # 它的输出只在终端里，进程突然退出后现场是空的），代价是访问日志也会跟着进来：
    # 前端每秒都在轮询，不压掉会把交易日志淹了。启停/错误走 uvicorn.error，仍保留。
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def get_logger(name: str = "daily_logger", log_dir: str = None) -> logging.Logger:
    """
    获取日志记录器的工厂函数。
    所有模块共用 ttt.log 文件，通过日志内的 [name] 区分模块来源。
    """
    setup_root_logger(log_dir)
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    return logger


class DailyLogger:
    """
    为了向后兼容旧项目中的对象导入方式，保留此类。
    """

    def __init__(self, log_directory=DEFAULT_LOG_DIR, logger_name='daily_logger'):
        if log_directory in ('logs', 'log'):
            self.log_directory = DEFAULT_LOG_DIR
        else:
            self.log_directory = log_directory
            
        self.logger_name = logger_name
        self.logger = get_logger(self.logger_name, self.log_directory)
        self.log_queue = global_log_queue

    def get_logger(self):
        """获取日志记录器实例"""
        return self.logger

    def get_log_queue(self):
        return self.log_queue


# 初始化全局默认实例及 Root Logger
setup_root_logger()
daily_logger = DailyLogger()
logger = daily_logger.get_logger()
log_queue = daily_logger.get_log_queue()
