"""规则模板系统 —— ttt_template_store 一行一个配置模板。

每行结构：数据库列 ``id`` + ``name`` + ``payload`` + ``updated_at``。payload 只包含
这一份模板的 params / reason / status，不再把全部模板、历史和当前状态塞进 id=1 的
整包 JSONB。保存只新增模板；加载才切换当前配置；非当前模板可以删除。

配置变更始终以“保存新版本 → 显式加载”的方式完成，包括美股盘中。这样盘中可以
及时调整点差等入场阀门，同时仍保留完整快照、配置名称和修改理由审计。Supabase 是
唯一真源；不做本地 JSON 缓存，也不自动播种任何默认配置。
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, replace as dc_replace
from datetime import datetime, time as dtime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.validator import RuleConfig
from app.validator.constants import (
    DAILY_PROFIT_TARGET,
    ENTRY_CUTOFF_MINS,
    MAX_TRADES_PER_DAY,
    ORDER_TIMEOUT_S,
    POSITION_TOLERANCE,
)

from app.core.dailylogger import get_logger

logger = get_logger("tt.templates")

_ET = ZoneInfo("America/New_York")
_DB_TABLE = "ttt_template_store"


class TemplatePersistenceError(RuntimeError):
    """配置模板写库失败；显式保存/加载/删除不能假装成功。"""


class TemplateLockedError(RuntimeError):
    """旧版盘中配置锁异常（保留导入兼容；当前配置版本流不再按时段锁定）。"""


@dataclass(frozen=True)
class Template:
    """一份完整规则配置（validator + 执行/watchdog 参数）。"""

    name: str
    max_daily_loss: Decimal         # 日亏熔断
    max_weekly_loss: Decimal        # 周亏熔断
    max_position_size: Decimal
    opening_blackout_min: Decimal
    min_delta: Decimal
    max_spread_pct: Decimal
    stop_pct: Decimal               # 止损百分比（watchdog）
    max_loss_per_trade: Decimal     # $ 金额止损线（watchdog）
    max_hold_mins: Decimal          # 最长持仓
    cooldown_seconds: int
    position_tolerance: Decimal = POSITION_TOLERANCE  # ⑥ 手数守卫容差（尾字段带默认，兼容旧持久化）
    max_trades_per_day: int = MAX_TRADES_PER_DAY      # ⑨ 今日交易次数上限
    daily_profit_target: Decimal = DAILY_PROFIT_TARGET  # ⑨ 日止盈锁利线
    entry_cutoff_mins: Decimal = ENTRY_CUTOFF_MINS              # 午后禁入分钟线（入场筛选二期）
    order_timeout_s: Decimal = ORDER_TIMEOUT_S                  # ⑥ 在途挂单超时自动撤单秒数（0=停用）
    # 2026-09-01 删除的字段：`chase_sigma_k`（追高 σ 倍数）、`pivot_er_threshold`
    # （震荡效率比门槛）、`mtf_5m_dead_zone`（方向闸 5min 死区）、
    # `stall_exhaust_lo/hi`（MACD 动能旧区间）。四条闸删了，这些参数没有读取方，
    # 设置页上那五个输入框同时撤掉。
    # 库里存着的旧记录仍带这些键，`_template_from_dict` 只遍历本类字段、忽略多余
    # 键，所以旧行照常加载，不需要跑任何迁移 SQL。

    def to_rule_config(self) -> RuleConfig:
        return RuleConfig(
            # 交易票池不是模板参数。生产链路必须由 WATCHER 本帧票池覆盖；
            # 空 tuple 是安全缺省，避免任何调用者绕过注入后误用历史固定票池。
            whitelist=(),
            max_daily_loss=self.max_daily_loss, max_weekly_loss=self.max_weekly_loss,
            max_position_size=self.max_position_size, opening_blackout_min=self.opening_blackout_min,
            min_delta=self.min_delta, max_spread_pct=self.max_spread_pct,
            position_tolerance=self.position_tolerance,
            max_trades_per_day=self.max_trades_per_day, daily_profit_target=self.daily_profit_target,
            entry_cutoff_mins=self.entry_cutoff_mins,
        )

    def to_dict(self) -> dict:
        d = asdict(self)
        return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in d.items()}


class TemplateStore:
    """管理逐行模板 + 当前加载 + 待加载（盘中冻结）。"""

    def __init__(self, templates: dict[str, Template], active_name: str,
                 record_meta: dict[str, dict] | None = None):
        if active_name not in templates:
            raise ValueError(f"active 模板不存在: {active_name!r}")
        self._templates = dict(templates)
        self._active = active_name
        self._pending: str | None = None
        self._meta: dict[str, dict] = {}
        supplied = record_meta or {}
        for index, name in enumerate(self._templates, start=1):
            meta = dict(supplied.get(name) or {})
            meta.setdefault("id", index)
            meta.setdefault("reason", "系统默认")
            meta.setdefault("created_at", _now_iso())
            meta.setdefault("updated_at", meta["created_at"])
            self._meta[name] = meta

    def list_names(self) -> list[str]:
        return list(self._templates.keys())

    def get(self, name: str) -> Template:
        return self._templates[name]

    def active(self) -> Template:
        return self._templates[self._active]

    def active_name(self) -> str:
        return self._active

    def pending_name(self) -> str | None:
        return self._pending

    def active_id(self) -> int:
        return int(self._meta[self._active]["id"])

    def pending_id(self) -> int | None:
        return int(self._meta[self._pending]["id"]) if self._pending else None

    def name_for_id(self, template_id: int) -> str:
        for name, meta in self._meta.items():
            if int(meta["id"]) == int(template_id):
                return name
        raise KeyError(template_id)

    def records(self) -> list[dict]:
        """返回前端/缓存使用的一行一模板结构，按更新时间倒序。"""
        rows = []
        for name, template in self._templates.items():
            meta = self._meta[name]
            status = "pending" if name == self._pending else "active" if name == self._active else "saved"
            rows.append({
                "id": int(meta["id"]),
                "name": name,
                "reason": meta.get("reason") or "",
                "created_at": meta.get("created_at") or "",
                "updated_at": meta.get("updated_at") or "",
                "status": status,
                "params": template.to_dict(),
            })
        return sorted(rows, key=lambda row: (row["updated_at"], row["id"]), reverse=True)

    def add(self, template_id: int, template: Template, *, reason: str,
            created_at: str | None = None, updated_at: str | None = None) -> None:
        if template.name in self._templates:
            raise ValueError(f"配置名称已存在: {template.name}")
        if any(int(meta["id"]) == int(template_id) for meta in self._meta.values()):
            raise ValueError(f"配置 ID 已存在: {template_id}")
        ts = created_at or _now_iso()
        self._templates[template.name] = template
        self._meta[template.name] = {
            "id": int(template_id),
            "reason": reason,
            "created_at": ts,
            "updated_at": updated_at or ts,
        }

    def remove(self, template_id: int) -> str:
        name = self.name_for_id(template_id)
        if name in (self._active, self._pending):
            raise ValueError("当前加载或待加载配置不能删除")
        del self._templates[name]
        del self._meta[name]
        return name

    def switch(self, name: str, *, frozen: bool) -> str:
        """加载模板。盘中进入待加载，否则立即成为当前配置。"""
        if name not in self._templates:
            raise KeyError(name)
        if frozen:
            self._pending = name
            return "pending"
        self._active = name
        self._pending = None
        return "active"

    def roll_session(self) -> bool:
        """换日：把盘中的待加载模板切为当前模板。"""
        if self._pending is not None:
            self._active = self._pending
            self._pending = None
            return True
        return False


# ============================================================
# 盘中冻结判定 + 持久化 + 单例
# ============================================================

def is_market_hours(now: datetime | None = None) -> bool:
    """美东 9:30–16:00 工作日 = 盘中（配置冻结）。"""
    et = (now or datetime.now(timezone.utc)).astimezone(_ET)
    if et.weekday() >= 5:
        return False
    return dtime(9, 30) <= et.time() < dtime(16, 0)


def _assert_config_unlocked(now: datetime | None = None) -> None:
    if is_market_hours(now):
        raise TemplateLockedError("美股盘中不可加载配置模板；请在盘前或盘后手动加载")


_FIELDS = [f for f in Template.__dataclass_fields__ if f != "name"]
# 2026-08-26 trend_strictness 删除后当前没有字符串型模板字段。留着空元组是因为
# _template_from_dict / _coerce_params 两处都按「字符串字段直接原样收下」分支写的，
# 下次再加字符串参数时把键名填回来即可。
_STR_KEYS: tuple[str, ...] = ()
_BOOL_KEYS: tuple[str, ...] = ()   # 2026-08-11 tp_wall_enabled 随 GEX 下线后暂无布尔参数
_TRUE_WORDS = frozenset({"true", "1", "on", "yes"})
_FALSE_WORDS = frozenset({"false", "0", "off", "no"})


def _as_bool(v) -> bool:
    """布尔字段解析。非法值必须抛错——静默当成 False 会悄悄关掉止盈。"""
    if isinstance(v, bool):
        return v
    word = str(v).strip().lower()
    if word in _TRUE_WORDS:
        return True
    if word in _FALSE_WORDS:
        return False
    raise ValueError(f"非法布尔值: {v!r}")


def _template_from_dict(name: str, d: dict) -> Template:
    kw = {}
    for f in _FIELDS:
        if f not in d:
            continue  # 旧持久化缺新字段（如 position_tolerance）→ 走 dataclass 默认
        v = d[f]
        if f in _STR_KEYS:
            kw[f] = str(v)
        elif f in _BOOL_KEYS:
            kw[f] = _as_bool(v)
        elif f in ("cooldown_seconds", "max_trades_per_day"):
            kw[f] = int(v)
        else:
            kw[f] = Decimal(str(v))
    return Template(name=name, **kw)


def _payload_for_record(record: dict) -> dict:
    params = dict(record["params"])
    params.pop("name", None)
    return {
        "params": params,
        "reason": record.get("reason") or "",
        "status": record["status"],
        "created_at": record.get("created_at") or record.get("updated_at") or _now_iso(),
    }


def _db_save_records(records: list[dict]) -> bool:
    """逐行 upsert；每个 payload 只属于该行 name 对应的一份配置。"""
    try:
        from app import db
        with db.connect() as conn:
            cur = conn.cursor()
            for record in records:
                updated_at = record.get("updated_at") or _now_iso()
                cur.execute(
                    f"""
                    insert into {_DB_TABLE} (id, name, payload, updated_at)
                    values (%s, %s, %s, %s)
                    on conflict (id) do update
                    set name = excluded.name,
                        payload = excluded.payload,
                        updated_at = excluded.updated_at
                    """,
                    (
                        int(record["id"]),
                        record["name"],
                        json.dumps(_payload_for_record(record), ensure_ascii=False),
                        updated_at,
                    ),
                )
            conn.commit()
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning(json.dumps({"event": "template_rows_save_error", "error": str(e)}, ensure_ascii=False))
        return False


def _db_load_records() -> list[dict] | None:
    """读取新逐行格式；发现旧整包时直接报错，不迁移、不兼容。"""
    try:
        from app import db
        with db.connect() as conn:
            cur = conn.cursor()
            cur.execute(f"select id, name, payload, updated_at from {_DB_TABLE} order by id")
            raw_rows = cur.fetchall()
        records = []
        for template_id, name, payload, updated_at in raw_rows:
            if not name or not isinstance(payload, dict) or not isinstance(payload.get("params"), dict):
                continue
            records.append({
                "id": int(template_id),
                "name": str(name),
                "params": dict(payload["params"]),
                "reason": str(payload.get("reason") or ""),
                "status": str(payload.get("status") or "saved"),
                "created_at": str(payload.get("created_at") or ""),
                "updated_at": updated_at.isoformat() if hasattr(updated_at, "isoformat") else str(updated_at or ""),
            })
        if raw_rows and not records:
            raise TemplatePersistenceError("检测到旧版 id=1 整包；请先删除旧行")
        return records
    except TemplatePersistenceError:
        raise
    except Exception as e:  # noqa: BLE001
        logger.warning(json.dumps({"event": "template_rows_load_error", "error": str(e)}, ensure_ascii=False))
        return None


def _store_from_records(records: list[dict]) -> TemplateStore:
    if not records:
        raise ValueError("没有配置记录")
    templates: dict[str, Template] = {}
    meta: dict[str, dict] = {}
    for row in records:
        name = str(row["name"]).strip()
        if not name or name in templates:
            raise ValueError(f"配置名称为空或重复: {name!r}")
        templates[name] = _template_from_dict(name, row["params"])
        meta[name] = {
            "id": int(row["id"]),
            "reason": row.get("reason") or "",
            "created_at": row.get("created_at") or row.get("updated_at") or "",
            "updated_at": row.get("updated_at") or row.get("created_at") or "",
        }
    active_row = next((row for row in records if row.get("status") == "active"), records[0])
    store = TemplateStore(templates, active_name=active_row["name"], record_meta=meta)
    pending_row = next((row for row in records if row.get("status") == "pending"), None)
    if pending_row:
        store._pending = pending_row["name"]  # noqa: SLF001
    return store


def _save(store: TemplateStore) -> None:
    records = store.records()
    if not _db_save_records(records):
        raise TemplatePersistenceError("配置模板写入数据库失败")


def _db_delete_record(template_id: int) -> bool:
    try:
        from app import db
        with db.connect() as conn:
            cur = conn.cursor()
            cur.execute(f"delete from {_DB_TABLE} where id = %s", (int(template_id),))
            if cur.rowcount != 1:
                conn.rollback()
                return False
            conn.commit()
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning(json.dumps({"event": "template_row_delete_error", "error": str(e)}, ensure_ascii=False))
        return False


def _load_store() -> TemplateStore:
    records = _db_load_records()
    if records is None:
        raise TemplatePersistenceError("无法读取 ttt_template_store，且系统不使用本地配置缓存")
    if not records:
        raise TemplatePersistenceError("ttt_template_store 为空；请先直接向数据库写入至少一份配置")
    return _store_from_records(records)


_STORE: TemplateStore | None = None


def get_store() -> TemplateStore:
    global _STORE
    if _STORE is None:
        _STORE = _load_store()
    return _STORE


def _now_iso(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()


# ── 设置页可保存的参数 ──────────────────────────────────────────────────────
# 可经 API 修改的键（白名单制）。交易票池不属于模板字段，唯一真源是 watcher watch_list。
# 刻意排除：
#   max_position_size  —— T1 铁律 $500 仓位锁死（用户明令禁改禁提）
#   position_tolerance —— 手数守卫容差，不属用户日常调参
ALLOWED_PARAM_KEYS = frozenset({
    "opening_blackout_min", "entry_cutoff_mins",
    "min_delta", "max_spread_pct", "max_trades_per_day",
    "max_daily_loss", "max_weekly_loss", "cooldown_seconds", "daily_profit_target",
    "stop_pct", "max_loss_per_trade", "max_hold_mins",
    "order_timeout_s",
})

_INT_KEYS = ("cooldown_seconds", "max_trades_per_day")


def _coerce_params(changes: dict[str, str]) -> dict:
    """字符串值 → Template 字段类型。非法键/值抛 KeyError/InvalidOperation。"""
    out = {}
    for k, v in changes.items():
        if k not in ALLOWED_PARAM_KEYS:
            raise KeyError(k)
        if k in _STR_KEYS:
            out[k] = str(v)
        elif k in _BOOL_KEYS:
            out[k] = _as_bool(v)
        else:
            out[k] = int(v) if k in _INT_KEYS else Decimal(str(v))
    return out


def save_template(name: str, changes: dict[str, str], reason: str,
                  now: datetime | None = None) -> dict:
    """新增一行配置模板；保存不改变当前加载配置。"""
    store = get_store()
    clean_name = name.strip()
    if not clean_name:
        raise ValueError("配置名称必填")
    if len(clean_name) > 80:
        raise ValueError("配置名称不能超过 80 个字符")
    if clean_name in store.list_names():
        raise ValueError(f"配置名称已存在: {clean_name}")
    kw = _coerce_params(changes)
    template_id = max((int(row["id"]) for row in store.records()), default=0) + 1
    if template_id > 32767:
        raise ValueError("配置数量已达到 smallint ID 上限")
    snapshot = dc_replace(store.active(), name=clean_name, **kw)
    ts = _now_iso(now)
    store.add(template_id, snapshot, reason=reason, created_at=ts, updated_at=ts)
    try:
        _save(store)
    except Exception:
        store.remove(template_id)
        raise
    return {
        "status": "saved",
        "active": store.active_name(),
        "template": next(row for row in store.records() if row["id"] == template_id),
    }


def load_template(template_id: int, now: datetime | None = None) -> dict:
    """从数据库模板列表加载配置并立即生效。"""
    _assert_config_unlocked(now)
    store = get_store()
    name = store.name_for_id(template_id)
    before_active, before_pending = store._active, store._pending  # noqa: SLF001
    status = store.switch(name, frozen=False)
    try:
        _save(store)
    except Exception:
        store._active, store._pending = before_active, before_pending  # noqa: SLF001
        raise
    return {
        "status": status,
        "active": store.active_name(),
        "pending": store.pending_name(),
        "template": next(row for row in store.records() if row["id"] == int(template_id)),
    }


def delete_template(template_id: int, now: datetime | None = None) -> dict:
    """删除一个非当前、非待加载模板；数据库删除失败时内存不动。"""
    store = get_store()
    name = store.name_for_id(template_id)
    if name in (store.active_name(), store.pending_name()):
        raise ValueError("当前加载或待加载配置不能删除")
    if not _db_delete_record(template_id):
        raise TemplatePersistenceError("数据库拒绝删除配置；请检查 DELETE 权限")
    removed = store.remove(template_id)
    return {"deleted": True, "id": int(template_id), "name": removed}
