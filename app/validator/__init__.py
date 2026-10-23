"""④ Pre-trade validator —— 系统灵魂，唯一下单闸门（阶段0：纯逻辑）。

对外暴露：validate() + 数据模型 + T1 铁律常量。
不接任何外部 API；所有输入由调用方组装成 ValidationInput。
"""
from app.validator.constants import (
    CONSECUTIVE_LOSS_COOLDOWN,
    MAX_DAILY_LOSS,
    MAX_LOSS_PER_TRADE,
    MAX_POSITION_SIZE,
    MAX_WEEKLY_LOSS,
    OPENING_BLACKOUT_MIN,
)
from app.validator.core import validate
from app.validator.models import (
    AccountState,
    Contract,
    RuleConfig,
    TickerSignal,
    TradeRequest,
    ValidationInput,
    ValidationResult,
)

__all__ = [
    "validate",
    "ValidationInput",
    "ValidationResult",
    "TradeRequest",
    "TickerSignal",
    "Contract",
    "AccountState",
    "RuleConfig",
    "MAX_POSITION_SIZE",
    "MAX_LOSS_PER_TRADE",
    "MAX_DAILY_LOSS",
    "MAX_WEEKLY_LOSS",
    "CONSECUTIVE_LOSS_COOLDOWN",
    "OPENING_BLACKOUT_MIN",
]
