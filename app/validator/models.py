"""Validator 的输入/输出数据模型（纯数据，无逻辑）。

金额一律 Decimal（禁止 float 算钱）；比率类（delta、ATR 倍数）
可用 Decimal 以便与阈值精确比较。方向：'long'=做多 CALL，'short'=做空 PUT。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from app.validator.constants import (
    DAILY_PROFIT_TARGET,
    ENTRY_CUTOFF_MINS,
    MAX_DAILY_LOSS,
    MAX_POSITION_SIZE,
    MAX_SPREAD_PCT,
    MAX_TRADES_PER_DAY,
    MAX_WEEKLY_LOSS,
    MIN_DELTA,
    OPENING_BLACKOUT_MIN,
    POSITION_TOLERANCE,
)

Direction = Literal["long", "short"]


@dataclass(frozen=True)
class TradeRequest:
    """一次下单意图。

    ⑥（2026-07-16 用户决策）起放开数量输入：用户在买入处自选张数，
    validator 不再算张数，改为守上限（数量×单手成本 ≤ 最大仓位×(1+容差)）。
    默认 1 张（不传即最小单位）。
    """

    ticker: str
    direction: Direction
    qty: int = 1


@dataclass(frozen=True)
class TickerSignal:
    """② 市场状态层给出的、该 ticker 的实时快照（validator 的市场输入）。

    **2026-09-01 起没有任何规则读这个结构。** 震荡（效率比）、追高（VWAP ±kσ）、
    方向闸（1min/5min 均线共振）、动能闸（5min MACD 柱）四条闸同日删除，随之删掉
    的字段是 `er` / `ema_dir_1m` / `ema_dir_5m` / `macd5_hist_now` /
    `macd5_hist_peak` / `macd5_hist_shrinking`。

    剩下这四个字段**只有两个用途**，都不是闸门：
      · 卡片上那行「距 VWAP ±x σ」（feed_snapshot）
      · 拒单留痕 ttt_intercept_log 的市场快照（trade_recorder.market_snapshot）
    留着是因为「被拦当时行情什么样」复盘时还要看，不是因为哪条规则要用。
    """

    price: Decimal         # 正股现价
    vwap: Decimal
    atr: Decimal           # 复盘用；也是 vwap_sigma 开盘地板的原料
    # 典型价对 VWAP 的加权标准差；0=未知。卡片按 (price−vwap)/σ 显示偏离倍数。
    vwap_sigma: Decimal = Decimal("0")


@dataclass(frozen=True)
class Contract:
    """③ 合约选择器为该 ticker+方向选出的唯一合约。

    delta 可为 None：行情档不提供 greeks 时（Alpaca 免费 indicative 档实测全缺）
    这个值根本拿不到。**不许在这里编一个默认值** —— 编出来的数会让规则 9 的
    delta 门槛恒成立，把一道空转的闸伪装成「通过」。规则 9 见到 None 会
    fail-open 放行并标记「未评估」，与同文件 macd5_hist_shrinking 同一约定。
    """

    delta: Decimal | None
    spread_pct: Decimal    # 分数形式，如 0.021 表示 2.1%
    mid: Decimal           # 权利金 mid（每股）
    ask: Decimal | None = None  # 新开仓可成交买价；旧调用方缺失时兼容回退 mid


@dataclass(frozen=True)
class AccountState:
    """⑦ 风控状态机 + 账户层输入。pnl 为签名值：亏损为负。"""

    daily_pnl: Decimal
    weekly_pnl: Decimal
    consecutive_losses: int
    cooldown_remaining_s: int      # 冷却剩余秒数，0 表示未在冷却
    minutes_since_open: Decimal    # 距开盘分钟数（用于 blackout）
    buying_power: Decimal
    trades_today: int = 0          # ⑨ 今日已交易次数（达上限熔断）


@dataclass(frozen=True)
class RuleConfig:
    """一份完整规则配置（= 一个可切换的规则模板；受盘中冻结约束）。

    validator 的所有可调阈值都从这里取（不再硬编码常量），使配置模板能整套切换。
    默认值取自 constants.py（铁律默认），模板可覆盖。
    """

    whitelist: tuple[str, ...]
    max_daily_loss: Decimal = MAX_DAILY_LOSS            # 日亏熔断
    max_weekly_loss: Decimal = MAX_WEEKLY_LOSS          # 周亏熔断
    max_position_size: Decimal = MAX_POSITION_SIZE      # 单笔最大仓位
    opening_blackout_min: Decimal = Decimal(OPENING_BLACKOUT_MIN)  # 开盘禁入分钟
    min_delta: Decimal = MIN_DELTA                      # 合约最低 delta
    max_spread_pct: Decimal = MAX_SPREAD_PCT            # 合约最大 spread
    position_tolerance: Decimal = POSITION_TOLERANCE    # ⑥ 手数守卫容差（默认 20%）
    max_trades_per_day: int = MAX_TRADES_PER_DAY        # ⑨ 今日交易次数上限
    daily_profit_target: Decimal = DAILY_PROFIT_TARGET  # ⑨ 日止盈锁利线
    entry_cutoff_mins: Decimal | None = ENTRY_CUTOFF_MINS              # 午后禁入分钟线（None=停用）
    # 2026-09-01 删除：`chase_sigma_k`（追高 σ 倍数）、`er_threshold`（震荡效率比
    # 门槛）、`stall_exhaust_lo/hi`（MACD 动能旧区间，2026-08-27 起本就不参与判断）。
    # 四条闸删了，这些阈值没有任何读取方。旧模板 JSON 里仍带着这些键，
    # templates._coerce_params 会直接忽略（见那边的白名单）。


@dataclass(frozen=True)
class ValidationInput:
    request: TradeRequest
    signal: TickerSignal
    account: AccountState
    config: RuleConfig
    contract: Contract | None = None   # ③ 选不出合格合约时为 None
    # 同标的**已占用**的仓位金额（入场成本口径，含 pending/open/closing）。规则 8 用它把
    # "先买 $500 再买 $500" 这种分次建仓算进同一个上限里——只看本单会让上限形同虚设。
    # 由 order_gate 从 REGISTRY 算好传入（validator 保持纯函数、不认识持仓登记）。
    existing_exposure: Decimal = Decimal(0)


@dataclass(frozen=True)
class ValidationResult:
    """(allowed, reason) + 程序算出的下单张数 + 命中的规则名。

    skipped 列出**因缺输入而没能评估**的判据名（如 greeks 缺失时的 "delta"）。
    allowed=True 只说明「没有任何一条判据把它拦下」，不等于每条判据都真跑过；
    这两件事必须分得开，否则日志里一片「通过」，没人看得出闸在空转。
    """

    allowed: bool
    reason: str = ""
    rule: str = ""
    qty: int = 0
    skipped: tuple[str, ...] = ()
