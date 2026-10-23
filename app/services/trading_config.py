# -*- coding: utf-8 -*-
"""
运行时交易配置
策略参数从 app/config.py 读取默认值，叠加 strategy_overrides.json 覆盖层（前端弹窗可改，持久化）；
提醒开关在运行时可通过 API 动态切换（重启恢复 config.py 默认）。
"""

import os
import json
import copy

from app.config import (ALERT_SWITCHES, ALERT_SOUNDS,
                         STRATEGY_A_PARAMS, STRATEGY_B_PARAMS,
                         STRATEGY_D_PARAMS, STRATEGY_E_PARAMS, STRATEGY_F_PARAMS,
                         STRATEGY_G_PARAMS, STRATEGY_H_PARAMS,
                         STRATEGY_C2_PARAMS, STRATEGY_C3_PARAMS)

# 运行时提醒开关（API 可动态修改，重启后恢复 config.py 默认值）
_alert_switches: dict = dict(ALERT_SWITCHES)

# ── 策略参数覆盖层 ──────────────────────────────────────────────────────────────
# 默认值（config.py 里的 dict，永不修改）；键与 get_config()['signal_strategy'] 一致。
_DEFAULTS: dict[str, dict] = {
    "A":  STRATEGY_A_PARAMS,
    "B":  STRATEGY_B_PARAMS,
    "D":  STRATEGY_D_PARAMS,
    "E":  STRATEGY_E_PARAMS,
    "F":  STRATEGY_F_PARAMS,
    "G":  STRATEGY_G_PARAMS,
    "H":  STRATEGY_H_PARAMS,
    "C2": STRATEGY_C2_PARAMS,
    "C3": STRATEGY_C3_PARAMS,
}

# 覆盖文件路径（项目根目录）
_OVERRIDES_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "strategy_overrides.json",
)

# 运行时参数 = 深拷贝默认值，再叠加覆盖层。get_config() 与引擎实例化都读这里。
_runtime_params: dict[str, dict] = {k: copy.deepcopy(v) for k, v in _DEFAULTS.items()}


def _load_overrides() -> None:
    """启动时把 strategy_overrides.json 叠加到运行时参数上（只叠加合法键）。"""
    if not os.path.exists(_OVERRIDES_PATH):
        return
    try:
        with open(_OVERRIDES_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"[trading_config] 读取 {_OVERRIDES_PATH} 失败: {e}")
        return
    for key, patch in (data or {}).items():
        if key not in _DEFAULTS or not isinstance(patch, dict):
            continue
        for pname, val in patch.items():
            if pname in _DEFAULTS[key]:
                try:
                    _runtime_params[key][pname] = _coerce(_DEFAULTS[key][pname], val)
                except Exception:
                    pass


def _save_overrides() -> None:
    """把运行时参数与默认值的差异（仅被改动的项）写回覆盖文件；空覆盖则删除文件。"""
    overrides: dict[str, dict] = {}
    for key, params in _runtime_params.items():
        diff = {p: v for p, v in params.items() if v != _DEFAULTS[key].get(p)}
        if diff:
            overrides[key] = diff
    try:
        if overrides:
            with open(_OVERRIDES_PATH, "w", encoding="utf-8") as f:
                json.dump(overrides, f, ensure_ascii=False, indent=2)
        elif os.path.exists(_OVERRIDES_PATH):
            os.remove(_OVERRIDES_PATH)
    except Exception as e:
        print(f"[trading_config] 写入 {_OVERRIDES_PATH} 失败: {e}")


def _coerce(default_val, new_val):
    """按默认值类型强转新值（bool/int/float/str），保证参数类型稳定。"""
    if isinstance(default_val, bool):
        if isinstance(new_val, bool):
            return new_val
        if isinstance(new_val, str):
            return new_val.strip().lower() in ("true", "1", "yes", "on")
        return bool(new_val)
    if isinstance(default_val, int):
        return int(round(float(new_val)))
    if isinstance(default_val, float):
        return float(new_val)
    return str(new_val)


def init():
    _load_overrides()


def get(key: str, default=None):
    """兼容旧调用：支持 'tiger.enabled' 点分隔路径查询"""
    from app import config as _cfg
    keys  = key.split(".")
    value = vars(_cfg)
    for k in keys:
        if isinstance(value, dict):
            value = value.get(k)
        else:
            return default
    return value if value is not None else default


def get_config() -> dict:
    """返回完整运行时配置（供 watcher_manager 实例化引擎用）"""
    return {
        "alerts": ALERT_SOUNDS,
        "signal_strategy": {
            "v7_alerts_enabled": _alert_switches.get("v7", True),
            "a_alerts_enabled":  _alert_switches.get("a",  True),
            "b_alerts_enabled":  _alert_switches.get("b",  True),
            "d_alerts_enabled":  _alert_switches.get("d",  True),
            "e_alerts_enabled":  _alert_switches.get("e",  True),
            "f_alerts_enabled":  _alert_switches.get("f",  True),
            "g_alerts_enabled":  _alert_switches.get("g",  True),
            "h_alerts_enabled":  _alert_switches.get("h",  True),
            "c2_alerts_enabled": _alert_switches.get("c2", True),
            "c3_alerts_enabled": _alert_switches.get("c3", True),
            "A":  _runtime_params["A"],
            "B":  _runtime_params["B"],
            "D":  _runtime_params["D"],
            "E":  _runtime_params["E"],
            "F":  _runtime_params["F"],
            "G":  _runtime_params["G"],
            "H":  _runtime_params["H"],
            "C2": _runtime_params["C2"],
            "C3": _runtime_params["C3"],
        },
    }


# ── 策略参数增删改（前端弹窗用）───────────────────────────────────────────────

def get_strategy_params() -> dict:
    """返回所有策略的当前运行时参数 + 默认值（供前端渲染表单/判断是否被改过）。"""
    return {
        "params":   copy.deepcopy(_runtime_params),
        "defaults": copy.deepcopy(_DEFAULTS),
    }


def update_strategy_params(key: str, patch: dict) -> dict:
    """更新某策略参数并持久化。返回 (ok, merged_params_or_error)。"""
    if key not in _DEFAULTS:
        return {"ok": False, "error": f"unknown strategy '{key}'"}
    if not isinstance(patch, dict):
        return {"ok": False, "error": "patch must be an object"}

    default = _DEFAULTS[key]
    unknown = [p for p in patch if p not in default]
    if unknown:
        return {"ok": False, "error": f"unknown params: {unknown}"}

    for pname, val in patch.items():
        try:
            _runtime_params[key][pname] = _coerce(default[pname], val)
        except Exception as e:
            return {"ok": False, "error": f"param '{pname}' invalid: {e}"}

    _save_overrides()
    return {"ok": True, "params": copy.deepcopy(_runtime_params[key])}


def reset_strategy_params(key: str) -> dict:
    """把某策略参数恢复为 config.py 默认值并持久化。"""
    if key not in _DEFAULTS:
        return {"ok": False, "error": f"unknown strategy '{key}'"}
    _runtime_params[key] = copy.deepcopy(_DEFAULTS[key])
    _save_overrides()
    return {"ok": True, "params": copy.deepcopy(_runtime_params[key])}


# ── ticker 档位（user_watchlist.gear）─────────────────────────────────────────
# gear 非空 = 订阅 + 指定 spread@K 档位（映射见 config.STRATEGY_H_GEARS）。
# 启动后首次调用时全量加载并缓存；换血/改档后调 refresh_gear_map() 或重启生效。
_gear_map: dict[str, str] | None = None


def _load_gear_map() -> dict[str, str]:
    try:
        from app.core.sb_client import get_supabase_client
        from app.config import USER_ID
        client = get_supabase_client()
        rows = client.table('user_watchlist').select('ticker,gear').eq('user_id', USER_ID).execute().data or []
        return {r['ticker'].upper(): r['gear'] for r in rows if r.get('gear')}
    except Exception as e:
        print(f"[trading_config] 加载 gear 档位失败(用默认档): {e}")
        return {}


def get_ticker_gear(ticker: str):
    """返回该 ticker 的 gear 档位名（user_watchlist.gear），未配置返回 None。"""
    global _gear_map
    if _gear_map is None:
        _gear_map = _load_gear_map()
        if _gear_map:
            print(f"[trading_config] gear 档位已加载: {len(_gear_map)} 个 ticker")
    return _gear_map.get(ticker.upper())


def refresh_gear_map() -> int:
    """强制重载 gear 档位缓存，返回条数（watchlist 换血/改档后调用）。"""
    global _gear_map
    _gear_map = _load_gear_map()
    return len(_gear_map)


# ── 全局 spread 系数（TODO P2.5 风险滑杆，2026-07-13）─────────────────────────
# 生效阈值 = max(0.15, 票档基准阈值 × 全局系数)。内存态，不写库，重启复位为 1.0；
# 只影响 spread_threshold，不影响 confirm_bars（gear 本身仍决定 confirm_bars）。
_global_spread_factor: float = 1.0
_H_SPREAD_FLOOR = 0.15


def get_global_spread_factor() -> float:
    return _global_spread_factor


def set_global_spread_factor(factor: float) -> float:
    """设置全局 spread 系数（内存态，不写库）。夹在 [0.5, 3.0] 防止拖出离谱值，返回夹紧后的值。"""
    global _global_spread_factor
    _global_spread_factor = max(0.5, min(3.0, float(factor)))
    return _global_spread_factor


def get_h_base_threshold(ticker: str) -> float:
    """某 ticker 的 H 策略 spread_threshold 基准值：有 gear 用 gear 档位值，否则用当前运行时默认值。"""
    from app.config import STRATEGY_H_GEARS
    gear = get_ticker_gear(ticker)
    if gear and gear in STRATEGY_H_GEARS:
        return float(STRATEGY_H_GEARS[gear]['spread_threshold'])
    return float(_runtime_params['H'].get('spread_threshold', STRATEGY_H_PARAMS['spread_threshold']))


def resolve_h_spread_threshold(ticker: str) -> float:
    """该 ticker 当前生效的 spread_threshold = max(0.15, 票档基准 × 全局系数)。"""
    return round(max(_H_SPREAD_FLOOR, get_h_base_threshold(ticker) * _global_spread_factor), 4)


def set_alert_enabled(strategy: str, enabled: bool) -> bool:
    """切换某个策略的提醒开关（strategy: 'v7' | 'a' | 'b'）"""
    key = strategy.lower()
    if key not in _alert_switches:
        return False
    _alert_switches[key] = enabled
    return True


def get_alert_switches() -> dict:
    """返回三个提醒开关的当前状态"""
    return dict(_alert_switches)


# 模块导入时即加载覆盖层，保证任何 get_config()/引擎实例化都拿到持久化参数
_load_overrides()
