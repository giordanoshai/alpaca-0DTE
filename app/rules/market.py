"""市场与合约的纯计算规则。"""
from __future__ import annotations

from decimal import Decimal


def spread_pct(bid: Decimal, ask: Decimal) -> Decimal:
    mid = (bid + ask) / 2
    return (ask - bid) / mid if mid > 0 else Decimal("1")


def format_data_age_zh(age_s: float | None) -> str:
    """行情数据落后多久，说人话。空值给空串，由调用方决定省略哪一段。

    文案只有一份：卡片（feed_snapshot）与下单拦截提示（market_context）都从这里
    取，不各写各的。两分钟以内报秒 —— 那还在「等下一根 K 线」的量级；再往上报
    分钟，用户据此判断是该等还是该重启。
    """
    if not age_s or age_s <= 0:
        return ""
    if age_s < 120:
        return f"{int(age_s)} 秒"
    return f"{int(age_s // 60)} 分钟"


# 2026-09-01：`is_chop`（震荡判据）、`ema_consensus_direction`（1min/5min 均线共振
# 方向）、`TREND_LABEL_ZH`（上涨/下跌/震荡文案）三者随规则 5、规则 6 与票池卡片上
# 的方向标签一并删除。它们没有别的消费者 —— 别为了「以后可能用得上」把它们捡回来，
# 捡回来就会有人拿它当第二套判据。
