"""事件流 —— 把结构化业务事件旁路成一条机读 JSONL。

为什么不迁移调用点：现状绝大多数业务事件走 `trade_recorder.audit()`，另有
相当数量直接写 `logger.info(json.dumps(...))`，分散在多个文件里，其中
watchdog / exec_entry / exec_close 等交易热路径上分布较集中（具体处数随代码
演进变化，不在此处写死精确值，以免与实际脱节）。为改日志格式去动下单/
平仓代码，风险收益不对等。改在 handler 层拦截：两种写法全部自动进流，三方库
因不发 JSON 被天然挡掉，以后新增埋点不论写法都自动进流。

设计依据：docs/superpowers/specs/2026-08-04-交易日志地基-design.md
"""
from __future__ import annotations

import json
import logging
import random
import re
import time
from collections import OrderedDict
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler

# handler 注入的信封字段。业务事件若自带同名键，原值挪到 `<key>_evt`，两边都不丢。
ENVELOPE_KEYS: tuple[str, ...] = (
    "ts", "ts_epoch", "run_id", "mode", "lv", "logger", "src",
)

# 进程运行段标识。取代靠 `db_worker_loop started` 猜重启点——2026-08-03 那晚
# 重启 11 次，切段全靠数这条无关日志。格式既唯一，又一眼看出起始时刻，且按时间排序。
RUN_ID: str = (
    f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{random.randrange(16 ** 4):04x}"
)


def _parse_event_payload(record: logging.LogRecord) -> dict | None:
    """尝试把 ``record`` 的消息体解析成「含 event 键的 JSON 对象」，解析不出返回 None。

    从 ``EventFilter.filter()`` 内联逻辑中抽出，供 ``RepeatSuppressFilter``
    的 ``_key()``/``_of()`` 共用——**绝不能让 RepeatSuppressFilter 依赖
    EventFilter 一定已经在本条记录上先跑过**。审查发现的 Critical 缺陷：
    `setup_root_logger()` 的 handler 挂载顺序是
    ``[file_h, console_h, queue_h, jsonl_h]``，只有排最后的 jsonl_h 挂了
    `EventFilter`；`Logger.callHandlers()` 按此顺序逐个 `handle()`，第一个
    评估 `RepeatSuppressFilter` 的永远是 file_h，那一刻 `record.
    _event_payload` 根本不存在——若 `RepeatSuppressFilter` 只会读现成的
    `_event_payload`、自己不具备解析能力，「按 event 字段归并」这条主分支
    在真实链路里就从未生效（判定结果一旦被 `record._suppress_decision`
    缓存，轮到 jsonl_h 跑完 `EventFilter` 也不会重新判定）。这里让
    `RepeatSuppressFilter` 自己也具备与 `EventFilter` 完全一致的解析能力，
    使 handler 挂载顺序变成无关变量，不必靠约定顺序去规避。

    调用方（`EventFilter.filter()` 与 `RepeatSuppressFilter._key()`/
    ``_of()``）命中的都是下单/平仓/风控热路径，整个函数包一层顶层
    try/except，解析失败统一返回 ``None``（代表"解析不出"，由调用方决定
    退化策略），绝不向上抛出。
    """
    try:
        try:
            msg = record.getMessage().strip()
        except Exception:  # noqa: BLE001 — 格式化失败视为解析失败
            return None
        if not msg.startswith("{"):
            return None  # 三方库纯文本在这里一次字符串比较即被排除
        try:
            payload = json.loads(msg)
        except (ValueError, TypeError):
            return None
        if not isinstance(payload, dict) or "event" not in payload:
            return None
        return payload
    except Exception:  # noqa: BLE001 — 顶层兜底，见上方 docstring
        return None


class EventFilter(logging.Filter):
    """只放行「消息体是含 event 键的 JSON 对象」的记录。

    解析结果挂到 ``record._event_payload``，handler 直接取用，不重复 json.loads。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        # 结构性兜底：把整个函数体包一层顶层 try/except。解析本身已委托给
        # `_parse_event_payload()`（内部自带同款顶层兜底），这里只再兜一层
        # 剩下唯一还会抛的地方——`record._event_payload = payload` 这次赋值
        # （例如 record 是无 __dict__ 的 slots 对象时会抛 AttributeError）。
        # 调用方是 logger.info()，命中的正是下单/平仓热路径，日志失败绝不能
        # 影响交易，这是项目铁律。
        try:
            payload = _parse_event_payload(record)
            if payload is None:
                return False
            record._event_payload = payload
            return True
        except Exception:  # noqa: BLE001 — 顶层兜底，见上方注释
            return False


# ---- mode（mock / paper / live）------------------------------------------
# `paper=True` 硬编码在 app/integrations/alpaca/client.py:172。core 属基础设施层，
# 不得反向 import integrations（CLAUDE.md 单向依赖），故由 integrations 侧在构造
# client 的地方调用 set_mode() 注册——真相只有一处，将来接 live 不会漏改。
_mode: str = "unknown"


def set_mode(mode: str) -> None:
    global _mode
    _mode = str(mode or "unknown")


def get_mode() -> str:
    return _mode


class JsonlEventHandler(TimedRotatingFileHandler):
    """把业务事件写成一行一个 JSON。

    只覆写 ``format()``，**不改 record**——4 个 handler 共享同一条 LogRecord，
    在这里改 msg 会污染 ttt.log 与 SSE 流。

    2026-08-04 审查（Task 6 第 1 轮修复）：``__init__`` 把落盘编码默认成
    UTF-8，不再单靠调用方人肉记得传 ``encoding='utf-8'``。这条流上跑的业务
    字段常年混着中文（``reason="手动开仓"``、``reason="watchdog:超时 15分"``
    等），``TimedRotatingFileHandler`` 未显式传 ``encoding`` 时会退化用
    ``locale.getpreferredencoding()``——非 UTF-8 系统 locale 下会把中文写成
    乱码，且这类错误只有在业务字段真的带非 ASCII 内容、又用 UTF-8 读回校验
    时才会暴露（生产侧 ``app/core/dailylogger.py`` 一直显式传参能跑对，纯属
    人肉纪律撑住，不是结构性保证；本仓库另有多处测试构造该类时没传
    ``encoding``，只是恰好都用纯 ASCII payload 没踩上）。这里只兜底默认值，
    调用方仍可显式传 ``encoding`` 覆盖——显式优先于隐式，不移除既有的显式传参。
    """

    def __init__(self, *args, **kwargs) -> None:
        kwargs.setdefault("encoding", "utf-8")
        super().__init__(*args, **kwargs)

    def format(self, record: logging.LogRecord) -> str:
        payload = getattr(record, "_event_payload", None) or {}
        out = {
            "ts": datetime.fromtimestamp(record.created).astimezone()
                          .isoformat(timespec="milliseconds"),
            "ts_epoch": round(record.created, 3),
            "run_id": RUN_ID,
            "mode": get_mode(),
            "lv": record.levelname,
            "logger": record.name,
            "src": f"{record.filename}:{record.lineno}",
        }
        # 键冲突消解策略（无损 + 确定性）：业务字段撞上信封字段名时先挪去
        # `<key>_evt`；但业务 payload 里可能本来就有一个字面叫 `ts_evt` 的
        # 键（甚至 `ts_evt2`……），这时 `<key>_evt` 这个目标位也可能已被
        # 占用——若直接赋值会静默覆盖、丢掉一侧的值，违反“两边都不丢”的
        # 初衷。因此这里不直接赋值，而是：目标键若已被占用，在其后追加
        # 递增序号（`ts_evt` -> `ts_evt2` -> `ts_evt3` ...）直到找到空位，
        # 绝不覆盖已有键。`payload.items()` 的遍历顺序等于 JSON 源文本的
        # 键序，对同一份输入恒定不变，因此这套消解结果对同一输入也是
        # 确定性的（顺序不同的等价输入允许落在不同的序号上，但不会丢值）。
        for key, value in payload.items():
            target = f"{key}_evt" if key in ENVELOPE_KEYS else key
            if target in out:
                base, n = target, 2
                target = f"{base}{n}"
                while target in out:
                    n += 1
                    target = f"{base}{n}"
            out[target] = value
        return json.dumps(out, ensure_ascii=False, default=str,
                          separators=(",", ":"))


_DIGITS = re.compile(r"[-+]?\d[\d.,:]*")

# `_streaks` 软上限。JSON 分支按 `event` 字段归并，key 空间天然有限；但非
# JSON 分支的 key 只替换数字，异常文本/文件路径/符号名等任何非数字的可变
# 内容都会各自撑出一个独立 key，长期运行的交易进程里会慢慢涨。超限后按
# 最近活跃时间批量淘汰最旧的一批，不引入定时线程（见 `_evict_if_needed`）。
_MAX_STREAKS = 512
_EVICT_BATCH = 128


class RepeatSuppressFilter(logging.Filter):
    """连续重复的 WARNING/ERROR 只出首条，窗口到点吐一条计数汇总。

    **只抑制 WARNING/ERROR，INFO 一律放行。** 心跳事件看着像噪声，但
    active_positions 会 0→1，按抹数字比对会把状态变化吃掉——离线折叠可以
    （原始日志还在），写入时丢弃则永久不可恢复。

    **挂在 handler 上，不是 logger 上。** Python 里 logger 的 filter 只作用于
    直接投递到该 logger 的记录，**不作用于子 logger 传播上来的记录**；业务日志
    全走 tt.trade / tt.gate 等子 logger，挂 root logger 一条都拦不到。
    handler 的 filter 才作用于所有传播来的记录。

    由此同一条记录会被 4 个 handler 各评一次，故判定结果缓存在
    ``record._suppress_decision`` 上，保证只计数一次。

    **`_key()`/`_of()` 不依赖 `EventFilter` 已经跑过。** 判定结果既然被首次
    评估的结果缓存，「首次评估发生在哪个 handler 上」就至关重要——
    `setup_root_logger()` 的挂载顺序是 `[file_h, console_h, queue_h,
    jsonl_h]`，只有排最后的 jsonl_h 挂了 `EventFilter`，第一个评估本
    filter 的永远是没挂 `EventFilter` 的 file_h，那一刻 `record.
    _event_payload` 还不存在。若 `_key()`/`_of()` 只会读现成的
    `_event_payload`，「按 event 字段归并」这条主分支在真实链路里就从未
    生效（曾是一个 Critical 缺陷）。故 `_key()`/`_of()` 都通过 `_payload()`
    自带与 `EventFilter` 一致的解析兜底，使挂载顺序变成无关变量。

    权衡：短于窗口的 streak 只丢计数，**事件本身一条不丢**（首条必出）。
    要连尾账都不丢就得起定时线程，为边际收益增加常驻 moving part，不做。

    **失败方向与 `EventFilter` 相反。** `EventFilter` 判定失败时
    `return False`——不放行进机读流，代价只是这条记录不进 JSONL，无害。
    这里判定失败必须 `return True`——放行、不抑制。因为这个 filter 的
    唯一职责是"少让几条重复日志落盘"，一旦判定链本身出错，最坏结果只能
    是"这次没抑制成功、多了几行重复日志"，绝不能是"把一条真实告警在
    异常里悄悄吞掉"。后人照抄 `EventFilter` 那套 `return False` 会在这里
    把线上的真实告警吞掉，务必留意这处方向差异。

    **`_streaks` 无锁，不是线程安全的（2026-08-04 审查提示，故意不加锁）。**
    多线程并发 `filter()` 会并发读写这个 `OrderedDict`，最坏情况是
    `move_to_end` 与 `_evict_if_needed` 的淘汰竞态导致 `KeyError`；但整条判定
    路径包在 `filter()` 的顶层 try/except 里，异常会被兜住并按上一条方向
    `return True`（放行），不会抛给调用方。代价最多是"这次没抑制成功、多了
    几行重复日志"，方向仍然安全。这里特意不加锁——日志写入路径不该为一个
    尽力而为的降噪功能引入新的同步开销，后人不要"顺手"给 `_streaks` 加锁
    "修复"线程安全，那是在拿热路径开销换一个本就无害的边界情况。
    """

    def __init__(self, window_s: float = 60.0, monotonic=time.monotonic) -> None:
        super().__init__()
        self.window_s = float(window_s)
        self._monotonic = monotonic
        # key -> [窗口起点, 计数, 首条墙钟, 末条墙钟]；用 OrderedDict 做简单
        # LRU：每次访问（新建/命中/翻窗重置）都挪到尾部，超限时从头部
        # （最久未活跃）批量淘汰，见 `_evict_if_needed`。
        self._streaks: "OrderedDict[tuple, list]" = OrderedDict()

    def _payload(self, record: logging.LogRecord) -> dict | None:
        """取事件 payload：优先读现成的 ``_event_payload``（省一次解析），
        没有就自己按 `EventFilter` 那套判定尝试解析——**不能依赖 EventFilter
        一定已经在本条记录上先跑过**，见 `_parse_event_payload()` docstring
        里记录的 Critical 缺陷（handler 挂载顺序导致的归并失效）。
        """
        payload = getattr(record, "_event_payload", None)
        if isinstance(payload, dict):
            return payload
        return _parse_event_payload(record)

    def _key(self, record: logging.LogRecord) -> tuple:
        payload = self._payload(record)
        if isinstance(payload, dict):
            tail = payload.get("event")
        else:
            try:
                tail = _DIGITS.sub("#", record.getMessage())
            except Exception:  # noqa: BLE001
                tail = "<unformattable>"
        return (record.name, record.filename, record.lineno, record.levelno, tail)

    def _of(self, record: logging.LogRecord) -> str:
        payload = self._payload(record)
        if isinstance(payload, dict) and payload.get("event"):
            return str(payload["event"])
        return f"{record.name}:{record.filename}:{record.lineno}"

    def _evict_if_needed(self) -> None:
        """`_streaks` 超过软上限时，从头部（最久未活跃）淘汰到低水位。

        触顶后一次性淘汰到 `_MAX_STREAKS - _EVICT_BATCH` 这条低水位线，而
        不是刚好淘汰回 `_MAX_STREAKS`——否则每来一条撑出新 key 的记录都会
        再次触顶、变成「稳态下逐条淘汰」，起不到批量摊薄开销的作用。淘汰到
        低水位后，要再攒够 `_EVICT_BATCH` 条增量才会重新触顶。不引入定时
        线程，淘汰只发生在写入路径上。
        """
        if len(self._streaks) <= _MAX_STREAKS:
            return
        target = max(_MAX_STREAKS - _EVICT_BATCH, 0)
        while len(self._streaks) > target:
            self._streaks.popitem(last=False)

    def filter(self, record: logging.LogRecord) -> bool:
        # 顶层结构性兜底，与 `EventFilter.filter()` 同款动机：调用方是
        # logger.warning()/error()，命中的正是风控/下单/平仓等热路径，
        # 日志失败绝不能影响交易。**方向与 EventFilter 相反，见类 docstring**
        # ——这里失败必须 `return True`（放行），不能 `return False`。
        try:
            cached = getattr(record, "_suppress_decision", None)
            if cached is not None:
                return cached
            decision = self._decide(record)
            record._suppress_decision = decision
            return decision
        except Exception:  # noqa: BLE001 — 顶层兜底；失败方向：放行(True)
            return True

    def _decide(self, record: logging.LogRecord) -> bool:
        if record.levelno < logging.WARNING:
            return True
        key = self._key(record)
        now = self._monotonic()
        wall = datetime.fromtimestamp(record.created).astimezone().isoformat(
            timespec="milliseconds")
        streak = self._streaks.get(key)
        if streak is None:
            self._streaks[key] = [now, 0, wall, wall]
            self._evict_if_needed()
            return True  # 首条必出，原样
        if now - streak[0] >= self.window_s:
            if streak[1] > 0:
                # 窗口已到点，且窗口期间确实抑制过记录：用旧 streak 累计的
                # 计数生成汇总。触发翻窗的这一条本身不计入旧窗口（它没被
                # 抑制，是被就地改写成了汇总事件），也不计入新窗口（新窗口
                # 从空 streak 重新起算）。若在判断窗口是否到期之前就无条件
                # 计入旧 streak，会把「首条必出」之外多算出一条，count 会
                # 多 1（已用测试锁死）。
                record.msg = json.dumps({
                    "event": "log_suppressed", "of": self._of(record),
                    "count": streak[1], "window_s": self.window_s,
                    "first_ts": streak[2], "last_ts": streak[3],
                }, ensure_ascii=False, separators=(",", ":"))
                record.args = ()
                record._event_payload = json.loads(record.msg)
            # streak[1] == 0：窗口到期但期间一条重复都没出现（复发间隔本身
            # 就比窗口长，例如低频重试/周期性对账失败）。这种情况绝不能走
            # 上面的改写分支——那会生成一条「抑制了 0 条」的空汇总，把这条
            # 真实告警的内容永久顶掉，此后每次复发都会重演，比不装这个
            # filter 还差。当作全新 streak：原样放行，不改写 record。
            self._streaks[key] = [now, 0, wall, wall]
            self._streaks.move_to_end(key)
            self._evict_if_needed()
            return True
        streak[1] += 1
        streak[3] = wall
        self._streaks.move_to_end(key)
        return False
