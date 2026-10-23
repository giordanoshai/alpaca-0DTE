"""T2 策略参数（可配、受盘中冻结约束）默认值。

阶段0 用内存默认值；后续接 Supabase `trading_config`（append-only、版本留痕、
盘中冻结、每笔交易记录当时生效的 config version）。T1 铁律常量另见
app/validator/constants.py（禁止参数化）。

"""
from __future__ import annotations

from decimal import Decimal

from app.validator import RuleConfig

import os
import re
from pathlib import Path

_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


def _read_env_file() -> dict[str, str]:
    out: dict[str, str] = {}
    if _ENV_PATH.exists():
        for line in _ENV_PATH.read_text(encoding="utf-8").splitlines():
            m = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$", line)
            if m:
                out[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    return out


def _init_env():
    """将 .env 文件中的配置同步填充至 os.environ（若 os.environ 中无显式覆盖）。"""
    for k, v in _read_env_file().items():
        if k not in os.environ:
            os.environ[k] = v


_init_env()


def env(key: str, default: str) -> str:
    """读取配置：优先 os.environ，其次 .env 文件，最后 default。"""
    v = os.environ.get(key)
    if v:
        return v
    return _read_env_file().get(key) or default


def proxy_setting(key: str) -> str:
    """代理专用取值：`.env` 文件优先于进程环境变量（与 env() 的优先级相反）。

    其余配置键保持「进程环境变量优先」——mock 联调靠 `DATA_SOURCE=mock` 这类临时覆盖。
    唯独代理必须反过来：AGENT.md 硬性要求外部行情 API 走 `.env` 里配的代理，而系统里
    残留一个用户级 `HTTP_PROXY` 就会静默盖掉 `.env`，症状是「改了 .env 却毫无反应」且
    不产生任何日志（2026-09-01 Yahoo 一直走系统那个 127.0.0.1:1080 即此因）。
    """
    return (_read_env_file().get(key) or "").strip() or env(key, "")


def normalize_http_proxy(raw: str) -> str:
    """把代理值补成带 scheme 的完整 URL：`host:port` → `http://host:port`。

    `.env` 里习惯只写 `host:port`，requests 会自己补 scheme，但 aiohttp / websockets
    这类库不会，拿到裸 `host:port` 直接抛错。已带 scheme 的原样返回——**不能无脑拼
    前缀**，否则 `socks5h://…` 会被拼成连不上的 `http://socks5h://…`。
    """
    value = (raw or "").strip()
    if value and "://" not in value:
        value = f"http://{value}"
    return value


_BIND_HOST_DEFAULT = "127.0.0.1"
_BIND_PORT_DEFAULT = 8002


def server_bind() -> tuple[str, int]:
    """uvicorn 的监听地址与端口，默认 `127.0.0.1:8002`。

    部署到服务器时在那边的 `.env` 里写 `TT_HOST=0.0.0.0` / `TT_PORT=8010`，
    本机 `.env` 两项都不配、开发习惯不变——这样两边共用一份代码。

    端口写错时回退默认值而不是抛异常：这两个值只在 `app/main.py` 的 `__main__`
    里用一次，抛出来就是「服务起不来、日志里只有一行 ValueError」；回退加一行
    告警既能让人看见自己写错了，服务又照常在默认端口上起来。
    """
    host = env("TT_HOST", _BIND_HOST_DEFAULT).strip() or _BIND_HOST_DEFAULT

    raw_port = env("TT_PORT", "").strip()
    if not raw_port:
        return host, _BIND_PORT_DEFAULT
    try:
        port = int(raw_port)
    except ValueError:
        port = 0
    if not (1 <= port <= 65535):
        print(f"[!] TT_PORT={raw_port!r} 不是 1~65535 的整数，"
              f"回退到默认端口 {_BIND_PORT_DEFAULT}")
        return host, _BIND_PORT_DEFAULT
    return host, port


# ── [Deprecated] 外部盯盘进程连接配置 ─────────────────────────────────────────
# 2026-07-22：4in1_watcher 已整合进进程内（app/watcher/），以下仅 watcher_client.py 使用。
WATCHER_BASE_URL = env("WATCHER_BASE_URL", "http://127.0.0.1:8000")
WATCHER_WS_URL = env("WATCHER_WS_URL", "ws://127.0.0.1:8000/ws")
WATCHER_SIGNAL_TIMEOUT_S = float(env("WATCHER_SIGNAL_TIMEOUT_S", "15"))

# ── UW (Unusual Whales): GEX, Gamma Levels & Market Tide ─────────────────────
UW_ENDPOINT = env("UW_ENDPOINT", "https://api.unusualwhales.com")
UW_API_KEY = env("UW_API_KEY", "")
_uw_rest = UW_ENDPOINT.rstrip("/")
_uw_ws_default = _uw_rest.replace("https://", "wss://").replace("http://", "ws://") + "/socket"
UW_WS_ENDPOINT = env("UW_WS_ENDPOINT", "") or _uw_ws_default

HTTP_PROXY = proxy_setting("HTTP_PROXY")
SOCKS5_PROXY = proxy_setting("SOCKS5_PROXY")

# ── Alpaca：REST + Option/Trading WS 统一显式 SOCKS5h，代理端解析 DNS ───────
ALPACA_PROXY_URL = env("ALPACA_PROXY_URL", "socks5h://127.0.0.1:1080")
OPTION_WS_SYMBOL_BUDGET = int(env("OPTION_WS_SYMBOL_BUDGET", "120"))
ALPACA_TRADING_STREAM_URL = env("ALPACA_TRADING_STREAM_URL", "")
ALPACA_TRADE_RECONCILE_INTERVAL_S = float(
    env("ALPACA_TRADE_RECONCILE_INTERVAL_S", "30")
)
ALPACA_TRADE_DISCONNECTED_RECONCILE_INTERVAL_S = float(
    env("ALPACA_TRADE_DISCONNECTED_RECONCILE_INTERVAL_S", "1")
)

# ── Alpaca 期权报价实时流（⑥ watchdog）：免费档 indicative；签 OPRA 后 env 改 opra 即可 ──
ALPACA_OPTION_FEED = env("ALPACA_OPTION_FEED", "indicative")
OPTION_STREAM_URL = env(
    "OPTION_STREAM_URL", f"wss://stream.data.alpaca.markets/v1beta1/{ALPACA_OPTION_FEED}")

# VWAP σ 的 ATR 地板系数：σ_eff = max(σ_session, VWAP_SIGMA_ATR_FLOOR × ATR14)。
# σ_session 的样本随交易日从 0 根 bar 长起，开盘早段是高方差估计，会让
# (price−vwap)/σ 这个偏离倍数在开盘头几分钟冲到几万倍。
#
# ⚠️ **2026-09-01 起它只服务卡片上那行「距 VWAP ±x σ」，不再是闸门参数**：
#    追高闸（原规则 7）同日删除，原常量名 `CHASE_SIGMA_ATR_FLOOR` 随之改名 ——
#    留着一个以已删规则命名的常量，下一个人会以为那道闸还在。地板本身保留，
#    是因为没有它开盘卡片会显示「≥99.9σ」这种没意义的数。
#
# 原标定（追高闸时代，仍是这个数的由来）：12 标的 × 43 天 × 19.6 万分钟判定点，
# 开盘 1-10min 拦截率 22.8%（第 2 分钟 47.5%）、31-60min 只有 7.5%；取 1.0 后
# 开盘 1-10min 降至 5.9%，31-60min 仅由 7.5% 动到 7.3%。
# spec: docs/superpowers/specs/2026-08-08-追高闸σ开盘失真-ATR地板-design.md
VWAP_SIGMA_ATR_FLOOR = Decimal("1.0")


def default_rule_config() -> RuleConfig:
    """未注入 WATCHER 票池时的安全缺省规则（空票池 = 禁止新开仓）。"""
    return RuleConfig(whitelist=())

SUPABASE_URL = env("SUPABASE_URL", "")
SUPABASE_SERVICE_ROLE_KEY = env("SUPABASE_SERVICE_ROLE_KEY", "")

USER_ID = "29795d82-c53b-43e5-985b-cb67b565d7bd"

# 顶部状态栏固定显示的 ticker
MARKET_STATUS_TICKERS = ["SPY", "QQQ", "^VIX"]

APP_TITLE   = "4in1 Watcher"
APP_VERSION = "1.0.0"
HOST        = "0.0.0.0"
PORT        = 8000
DEBUG       = True


# ══════════════════════════════════════════════════════════════════════════════
# 策略注册表 — 新增策略时只需在此处加一行，后端自动生效。
#
# 字段说明：
#   enabled : bool — 是否参与回测 / 实时监控（False 时策略完全不跑，alert 无意义）
#   alert   : bool — 策略运行时是否触发前端声音 + 弹窗（enabled=True 时才有效）
#                    用途：新策略上线初期可 enabled=True, alert=False 静默观察
#   desc    : str  — 人类可读描述（用于日志和前端 tooltip）
#
# 前端仍需手动添加一个按钮（index.html + app.js 各一行）。
# ══════════════════════════════════════════════════════════════════════════════

STRATEGY_REGISTRY: dict[str, dict] = {
    "v7": {"enabled": False,  "alert": True,  "desc": "V7 4in1 多周期共振（金叉/死叉/形态）"},
    "a":  {"enabled": False,  "alert": True,  "desc": "策略A：V4 交叉 + Regime 追赶确认"},
    "b":  {"enabled": False,  "alert": True,  "desc": "策略B：事件驱动（缩量整理/突破/回踩/衰竭）"},
    "d":  {"enabled": False,  "alert": True,  "desc": "策略D：V4 交叉后价格破位验证"},
    "e":  {"enabled": False,  "alert": True,  "desc": "策略E：V4 交叉宽松 Close 锚定 + 允许回踩"},
    "f":  {"enabled": False,  "alert": True,  "desc": "策略F：V4 gap 连续扩大动量验证"},
    "g":  {"enabled": False,   "alert": True,  "desc": "策略G：EMA快速穿越 + VWAP + SMA趋势过滤"},
    "h":  {"enabled": True,   "alert": True,  "desc": "策略H：EMA9/21交叉 + spread@K确认闸（2026-07定稿，分档gear+同向再响升级）"},
    "c2": {"enabled": False,   "alert": True,  "desc": "策略C2：EMA + MACD + RSI 三指标共振 V2.1（F2/F3/F4 过滤）"},
    "c3": {"enabled": False,  "alert": True,  "desc": "策略C3：EMA + MACD + RSI 原始触发 + 5min 噪音否决器"},
}

# 由注册表自动派生，不要手动维护
ALERT_SWITCHES: dict[str, bool] = {k: v["enabled"] and v["alert"] for k, v in STRATEGY_REGISTRY.items()}
ENABLED_STRATEGIES: set[str]    = {k for k, v in STRATEGY_REGISTRY.items() if v["enabled"]}

# 提醒音效文件（相对于 static/ 目录）
ALERT_SOUNDS = {
    "signal":     "alert.wav",     # 买卖信号音效
    "stop_loss":  "alert_2.wav",   # 止损触发音效
    "take_profit": "alert_2.wav",  # 止盈触发音效
}


REGIME_PARAMS = {
    # Regime 净分 = bull_score - bear_score，范围 -10 ~ +10
    # 以下阈值用于策略 A 的置信度判断（见 STRATEGY_A_PARAMS.regime_high_confidence_threshold）
    # 也可在前端 TradingView market_regime.pine 中对应修改
    "strong_bull_thresh":   7,   # >= 7：强多
    "weak_bull_thresh":     2,   # >= 2：偏多
    "weak_bear_thresh":    -2,   # <= -2：偏空
    "strong_bear_thresh":  -7,   # <= -7：强空
    # 介于 -2 ~ +2 为震荡（mixed），bull/bear 均低于 3 为横盘（flat）
}


# 注：V7_PARAMS / V4_PARAMS 两个参数块已于 2026-08-15 随
# `app/strategies/four_in_one_v7.py` 与 `four_in_one_v4.py` 一同删除——
# 它们除了定义处外全项目零引用。git 历史 c7ce8de 可取回。


# ══════════════════════════════════════════════════════════════════════════════
# 策略 A 参数（V4 交叉 + Regime 追赶确认）
# 对应 app/strategies/signal_engine_a.py → SignalEngineA
# ══════════════════════════════════════════════════════════════════════════════

STRATEGY_A_PARAMS = {
    # ── 交叉确认窗口 ──────────────────────────────────────────────────────────
    "pending_window":   5,  # 死叉/金叉发生后，等待 regime 追赶的最大根数
                            # 超过此窗口仍未确认则过期，不再触发信号

    # ── 防抖（假交叉过滤）────────────────────────────────────────────────────
    "debounce_bars":    2,  # 反向交叉需持续 N 根才算确认反转，防止频繁切换

    # ── Regime 追赶确认 ───────────────────────────────────────────────────────
    # 策略 A 核心逻辑：V4 死叉后，需要 regime 分也连续下降才确认 bear 信号
    # 目前要求连续 2 根（prev2 → prev → current 单调方向一致）
    # 如需更宽松改为 1 根，删掉 prev2_regime 判断即可（见 signal_engine_a.py）
    "regime_high_confidence_threshold":  4,  # regime 绝对值 >= 此值时，置信度标为 "high"
                                             # 否则为 "low"（影响前端 ★ 标注）

    # ── 信号冷却 ──────────────────────────────────────────────────────────────
    "cooldown_bars":    5,  # 同一 pending 触发信号后，至少再过 N 根才能再次触发
                            # 防止同一个交叉连续反复提醒

    # ── 趋势回踩检测 ──────────────────────────────────────────────────────────
    "pullback_enabled":      True,  # 是否开启回踩信号（cross_confirm 后继续监控）
    "min_gap_peak":          0.5,   # fast-slow gap 峰值至少达到此值才开始监控回踩
    "pullback_gap_pct":      0.20,  # gap 从峰值回落到 20% 以下时，标记进入回踩
    "pullback_reopen_pct":   0.40,  # 回踩后 gap 重新扩大到峰值的 40% 以上，触发回踩信号
}


# ══════════════════════════════════════════════════════════════════════════════
# 策略 B 参数（事件驱动：缩量整理 / 突破 / 回踩 / 衰竭）
# 对应 app/strategies/signal_engine_b.py → SignalEngineB
# ══════════════════════════════════════════════════════════════════════════════

STRATEGY_B_PARAMS = {
    # ── 缩量整理识别 ──────────────────────────────────────────────────────────
    "contraction_lookback":   8,    # 回看 N 根识别缩量整理
    "vol_low_ratio":          0.6,  # 成交量低于均量的 70% 才算缩量根
    "vol_low_pct":            0.4,  # 缩量根数占比 >= 40% 才认为进入整理
    "max_range_pct":          0.5,  # 整理区间最大振幅（相对价格的百分比），超过则不算整理
                                    # 例：0.5 表示振幅不超过价格的 0.5%
    "atr_contraction_ratio":  1,  # 当前 ATR / ATR_MA <= 此值，才认为波动收缩
    "contraction_score_min":  3,    # 三项缩量条件（成交量/价格区间/ATR）至少满足几项才算整理

    # ── 突破确认 ──────────────────────────────────────────────────────────────
    "vol_breakout_ratio":     2.5,  # 突破时成交量需达到均量的 N 倍（放量确认突破）

    # ── 整理记忆 ──────────────────────────────────────────────────────────────
    "contraction_memory":     8,    # 整理确认后，后续 N 根内突破仍有效
                                    # 超过此根数则整理状态失效

    # ── 趋势回踩 ──────────────────────────────────────────────────────────────
    "trend_invalidate_pct":   0.005,  # 回踩时价格反向超过此比例则趋势失效（0.5%）
    "key_level_tolerance":    0.001,  # 关键位附近的容忍距离（±0.1%）

    # ── GEX 辅助过滤 ──────────────────────────────────────────────────────────
    "gex_proximity_pct":      0.003,  # 距 GEX 关键位 0.3% 以内才触发 GEX 相关信号

    # ── 信号冷却 ──────────────────────────────────────────────────────────────
    "cooldown_bars":          8,    # 同类信号触发后至少间隔 N 根才能再次触发
}


# ══════════════════════════════════════════════════════════════════════════════
# 策略 C2 参数（EMA + MACD + RSI 三指标共振 V2.1）
# 对应 app/strategies/signal_engine_c2.py → SignalEngineC2
#
# [TODO: 进阶架构规划 - 按股性分组]
# 以后实盘若需精细化，建议不要按单个 Ticker 拆分参数（极易过拟合），而是按“股性”分 2 组：
# 1. 宽基/蓝筹组（如 SPY, MSFT）：使用当前普适的 default 配置
# 2. 高弹/妖股组（如 COIN, MSTR, TSLA）：未来可单建一个 STRATEGY_C2_HIGH_VOL_PARAMS，
#    采用极致防波动的过滤要求 (如 min_confluence=6, cooldown_bars=40)。
# 届时在 watcher_manager.py 中根据标的列表动态分发配置字典即可。当前暂保持统一。
# ══════════════════════════════════════════════════════════════════════════════

STRATEGY_C2_PARAMS = {
    # ── Core（与 C V1，默认配置 一致）────────────────────────────────────────────────
    "ema_fast_len":    9,
    "ema_slow_len":   20,
    "macd_fast":      12,
    "macd_slow":      26,
    "macd_signal_len": 9,
    "rsi_len":        14,
    "rsi_ema_len":    14,
    "rsi_level":      50.0,

    # "ema_fast_len":    5,
    # "ema_slow_len":   13,
    # "macd_fast":      6,
    # "macd_slow":      13,
    # "macd_signal_len": 5,
    # "rsi_len":        10,
    # "rsi_ema_len":    6,
    # "rsi_level":      50.0,

    # ── V2.1 过滤器（对应 Pine F1–F4）────────────────────────────────────
    "min_confluence":     0,    # F1: 0 = 禁用，6 = 仅完美共振
    "cooldown_bars":     0,    # F2: 同方向信号间隔 ≥ 30 根 1m bar
    "price_cooldown_atr": 0,  # F3: 价格须距上次信号 ≥ 0.5 × ATR，0 = 禁用
    "skip_open_minutes":  0,   # F4a: 跳过开盘后前 15 分钟
    "skip_close_minutes":  10,   # F4b: 跳过收盘前 N 分钟，0 = 禁用
    "enforce_rth":      True,   # F4: 强制 RTH（09:30-16:00 ET）硬过滤，24/7 标的可设 False
}


# ══════════════════════════════════════════════════════════════════════════════
# 策略 C3 参数（EMA + MACD + RSI 原始触发 + 5min 噪音否决器）
# 对应 app/strategies/signal_engine_c3.py → SignalEngineC3
# ══════════════════════════════════════════════════════════════════════════════

STRATEGY_C3_PARAMS = {
    # ── Core ────────────────────────────────────────────────
    # "ema_fast_len":    5,
    # "ema_slow_len":   13,
    # "macd_fast":      6,
    # "macd_slow":      13,
    # "macd_signal_len": 5,
    # "rsi_len":        10,
    # "rsi_ema_len":    6,
    # "rsi_level":      50.0,
    #-------慢节奏-----
    "ema_fast_len":    9,
    "ema_slow_len":   20,
    "macd_fast":      12,
    "macd_slow":      26,
    "macd_signal_len": 9,
    "rsi_len":        14,
    "rsi_ema_len":    14,
    "rsi_level":      50.0,


    # ── 时间过滤 ────────────────────────────────────────────
    "skip_open_minutes":  5,
    "skip_close_minutes": 0,
    "enforce_rth":        True,

    # ── 5min 噪音否决器 ──────────────────────────────────────
    "enable_5m_noise_veto": True,
    "chop_ema_spread_atr_threshold": 0.25,
    "chop_macd_hist_atr_threshold":  0.05,
    "breakout_vol_ratio_threshold":  1.2,
    "min_5m_bars":                   30,
}


# ══════════════════════════════════════════════════════════════════════════════
# Market Regime 参数
# 对应 app/services/market_regime.py
# ══════════════════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════════════════
# 策略 D 参数（V4 交叉后价格破位验证）
# 对应 app/strategies/signal_engine_d.py → SignalEngineD
# ══════════════════════════════════════════════════════════════════════════════

STRATEGY_D_PARAMS = {
    # ── 破位验证窗口 ──────────────────────────────────────────────────────────
    "pending_window":   3,    # 交叉后监控的最大根数
                              # 金叉：随后 n 根内有 Close > 交叉根 High → bull 信号
                              # 死叉：随后 n 根内有 Close < 交叉根 Low  → bear 信号
                              # 超时未触发则丢弃（假交叉噪音）

    # ── 极端波动屏蔽 ──────────────────────────────────────────────────────────
    "atr_ratio_limit":  999,  # ATR(14) / ATR_MA(20) 超过此值时屏蔽所有信号
}


# ══════════════════════════════════════════════════════════════════════════════
# 策略 E 参数（V4 交叉宽松 Close 锚定 + 允许回踩）
# 对应 app/strategies/signal_engine_e.py → SignalEngineE
# ══════════════════════════════════════════════════════════════════════════════

STRATEGY_E_PARAMS = {
    # ── 破位验证窗口 ──────────────────────────────────────────────────────────
    "pending_window":     5,      # 交叉后监控的最大根数（宽松模式下 3-4）
                                  # 锚定价为交叉根 Close（非 High/Low），门槛更低

    # ── 极端波动屏蔽 ──────────────────────────────────────────────────────────
    "atr_ratio_limit":    999,    # ATR(14) / ATR_MA(20) 超过此值时屏蔽（实际关闭）

    # ── 回踩容忍 ──────────────────────────────────────────────────────────────
    "pullback_tolerance": 0.001,  # 窗口内 close 允许低于 anchor 0.1%（超过则丢弃 pending）
}


# ══════════════════════════════════════════════════════════════════════════════
# 策略 F 参数（V4 gap 连续扩大动量验证）
# 对应 app/strategies/signal_engine_f.py → SignalEngineF
# ══════════════════════════════════════════════════════════════════════════════

STRATEGY_F_PARAMS = {
    # ── 连续扩大所需根数 ───────────────────────────────────────────────────────
    "pending_window":   3,      # 任意连续 N 根 gap 扩大即触发（2-3）

    # ── 最大观察窗口 ──────────────────────────────────────────────────────────
    "max_window":        10,      # 超过此根数仍未触发则丢弃 pending

    # ── 极端波动屏蔽 ──────────────────────────────────────────────────────────
    "atr_ratio_limit":   999,

    # ── gap 最小扩大量 ────────────────────────────────────────────────────────
    "gap_min_expansion": 0.0,    # 每根 gap 至少扩大此值（0 = 只要正方向扩大即可）
}


# ══════════════════════════════════════════════════════════════════════════════
# 策略 G 参数（EMA快速穿越 + VWAP + SMA趋势过滤）
# 对应 app/strategies/signal_engine_g.py → SignalEngineG
# ══════════════════════════════════════════════════════════════════════════════

STRATEGY_G_PARAMS = {
    # ── 入场 EMA ─────────────────────────────────────────────────────────────
    "ema_entry_short":  17,    # 入场短期 EMA 周期（快线，用于捕捉信号）
    "ema_entry_long":   31,    # 入场长期 EMA 周期（慢线，穿越触发信号）

    # ── 趋势过滤 ──────────────────────────────────────────────────────────────
    "sma_period":       63,   # 长期 SMA 周期，价格必须在此线上方（bull）/ 下方（bear）

    # ── 出场 EMA ─────────────────────────────────────────────────────────────
    "ema_exit_small":   8,    # 出场短期 EMA（快线，用于持仓退出信号）
    "ema_exit_long":    9,    # 出场长期 EMA（慢线，穿越触发退出信号）

    # ── 信号冷却 ──────────────────────────────────────────────────────────────
    "cooldown_bars":    3,    # 同类信号触发后至少间隔 N 根才能再次触发
}

# ══════════════════════════════════════════════════════════════════════════════
# 策略 H 参数（EMA9/21 裸交叉 + spread@K 确认闸 —— 2026-07-11 定稿链路）
# 对应 app/strategies/signal_engine_h.py → SignalEngineH
#
# 逻辑（混淆矩阵验证定稿，见 step7_trendscore_filter/筛选逻辑/2_/6_ 文档）：
#   1. EMA(ema_fast) 上/下穿 EMA(ema_slow) 产生裸交叉（交叉根须在 RTH 内）；
#   2. 等 confirm_bars 根后，在确认根收盘时检查
#        spread = 方向 × (EMA快 − EMA慢) / ATR(atr_period)@交叉根 >= spread_threshold
#      未拉开（含被反向叉回）→ 静默丢弃；拉开 → 发信号；
#   3. 同向再响升级：距上一次本引擎信号 <= recross_escalate_min 分钟且同向
#      → confidence='high'（响铃+通知，实测为全场质量最高子集）；
#      首响 confidence='low'（只有徽章+边框闪烁，无声）。
# 旧版斜率/量能双闸已被证伪为随机砍量（误杀96%好点零精度增益），2026-07-10 废除。
# 档位（gear）按 ticker 从 user_watchlist.gear 取，映射见 STRATEGY_H_GEARS；
# 表里没配 gear 的 ticker 用本 dict 的 confirm_bars/spread_threshold 默认值。
# ══════════════════════════════════════════════════════════════════════════════

STRATEGY_H_PARAMS = {
    # ── 双线 ─────────────────────────────────────────────────────────────────
    "ema_fast":             9,     # 快线 EMA 周期
    "ema_slow":             21,    # 慢线 EMA 周期
    "atr_period":           14,    # ATR 归一长度（在交叉根取值）

    # ── spread@K 确认闸（默认=标准档） ─────────────────────────────────────────
    "confirm_bars":         3,     # 交叉后等 K 根再确认（延迟 K 分钟）
    "spread_threshold":     0.15,  # 确认根收盘 (fast-slow)/ATR@交叉根 方向对齐阈值

    # ── 同向再响升级 ──────────────────────────────────────────────────────────
    # 分桶实测(4标的,2026-01~06):质量增益集中在<=30min(Def2 94~98% vs 基线90.6%),
    # 30-45min 增益归零(90.0%=基线),60min 后跌破基线 → 窗口定 30(约1.1次/天/标的)
    "recross_escalate_min": 30,    # 上一信号同向且间隔<=N分钟 → 二次确认(high,有声)
    "rth_only":             True,  # 只对 RTH(09:30-16:00 ET) 内的交叉发信号
}

# gear 档位映射（user_watchlist.gear 的取值 → 确认参数覆盖）。
# 实测（2026-01~06，QQQ/IWM/MU/CRCL + 180标的泛化）：
#   spread3_0.15 标准档 ~12/天/票 误杀 2~3%  精度 90~91%
#   spread3_0.22 中间档 ~9.7/天   误杀 7~10% 精度 93~95%
#   spread3_0.30 狠档   ~8/天     误杀 18~22% 精度 95~97%
#   spread5_0.30 严格档 ~9/天     误杀 11~16% 精度 96~98%（低振幅高流动性票/指数用）
#
# instant_0.15 即时档（2026-09-05 加，现役 NVDA/QQQ/TSLA 三只在用）：
#   confirm_bars=0 → **不等确认**，交叉当根就发无声提醒（约 19 次/天/票），一个不漏；
#   阈值在本档下不是过滤条件而是**升级条件** —— 拉开到 0.15 时补发响铃（约 8 次/天/票）。
#   背景：名单从 13 只砍到 3 只后信号量从每天约 110 次降到 57 次，用户要求不再等确认。
#   代价：「同向再响升级」在本档下天然失效（裸交叉里金叉死叉必然交替，实测 0 次触发），
#   响铃改由「拉开」触发。实现见 SignalEngineH._instant。
STRATEGY_H_GEARS = {
    "instant_0.15": {"confirm_bars": 0, "spread_threshold": 0.15},
    "spread3_0.15": {"confirm_bars": 3, "spread_threshold": 0.15},
    "spread3_0.22": {"confirm_bars": 3, "spread_threshold": 0.22},
    "spread3_0.30": {"confirm_bars": 3, "spread_threshold": 0.30},
    "spread5_0.30": {"confirm_bars": 5, "spread_threshold": 0.30},
}

# 参考实测速率（同上，仅供前端估算展示用，不参与任何逻辑判断）。
# confirm_bars=3 家族三点用于全局滑杆插值预估"生效阈值→留量/天"；
# spread5_0.30（confirm_bars=5）是独立档位，只有一个参考点，不参与插值。
STRATEGY_H_GEAR_STATS = {
    "confirm_bars_3": [
        {"spread_threshold": 0.15, "rate_per_day": 12.0, "precision_pct": 90.5},
        {"spread_threshold": 0.22, "rate_per_day": 9.7,  "precision_pct": 94.0},
        {"spread_threshold": 0.30, "rate_per_day": 8.0,  "precision_pct": 96.0},
    ],
    "spread5_0.30": {"rate_per_day": 9.0, "precision_pct": 97.0},
}


# ══════════════════════════════════════════════════════════════════════════════
# UW Market Tide 展示徽章配置（纯展示，不参与任何策略计算）
# ══════════════════════════════════════════════════════════════════════════════
# 徽章直接显示最新一根的 diff = net_call_premium - net_put_premium（开盘即出），
# diff 带滞回带穿越 0 轴时报「Market Tide 交叉」（UW 官方策略定义）。
# 见 docs/superpowers/specs/2026-07-22-market-tide-cross-alert-design.md。
MARKET_TIDE_CONFIG = {
    # Toggling off only disables the display badge and does not impact any trade execution.
    "enabled": True,

    # diff 穿 0 的 ±滞回带（美元），防抖用。按**当日累计值**口径标定。
    # **2026-08-20 由 5M 重标为 1.0M**：切 0DTE 后量级小了一个数量级（日间摆幅
    # 从 ±100M~750M 降到 ±20M），原来的 5M 死区占掉两三成摆幅、几乎不再触发。
    # 六个交易日真实序列扫描：5M→六天共 4 次（两天整天不报）、2M→9 次、
    # **1.0M→15 次（约 2.5 次/天，每天都有信号且无空白日）**、0.75M→21 次、0.5M→29 次。
    "cross_hysteresis_usd": 1_000_000,
}



# 注：SOS（Swing Option Screener，2026-08-07 从 D:\Dev_project\SOS v3.0 移植进来）
# 的全部 SOS_* 运行参数已于 2026-08-15 随该模块整体移出本项目，备份在 D:\SOS_BAK
# （含还原说明）。`.env` 里的 SOS_* 变量已无人读取。
