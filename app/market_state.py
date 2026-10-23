"""② 市场状态层 —— 从分钟线自算 VWAP / σ / ATR（纯函数）。

validator 的市场输入（TickerSignal 的 price/vwap/atr/vwap_sigma）由本层产出；
不依赖盯盘程序存活。既用于阶段1 live，也用于阶段0 历史回放
（对每笔历史单的 entry_time，取其之前的分钟线重算入场时市场状态）。

**2026-09-01 起这些量都不再进闸门**：震荡（效率比）、追高（VWAP ±kσ）、方向闸
（EMA 共振）、动能闸（5min MACD 柱）四条规则同日删除。剩下的输出只服务两处 ——
卡片上那行「距 VWAP ±x σ」，以及拒单留痕里的市场快照。

金额/价格用 Decimal。ATR 周期作为参数，用历史快照里已存的 ticker.atr 标定过，
与现系统口径一致。
"""
from __future__ import annotations

from decimal import Decimal

from app.core.market_types import Bar


def session_vwap(bars: list[Bar]) -> Decimal:
    """成交量加权均价：Σ(典型价×量)/Σ量，典型价=(H+L+C)/3。

    传入的 bars 应为「同一交易时段、开盘至今」的序列。
    """
    if not bars:
        raise ValueError("session_vwap 需要至少一根 bar")
    three = Decimal(3)
    pv = Decimal(0)
    vol = Decimal(0)
    for b in bars:
        tp = (b.high + b.low + b.close) / three
        pv += tp * b.volume
        vol += b.volume
    if vol == 0:
        raise ValueError("总成交量为 0，无法计算 VWAP")
    return pv / vol


def vwap_sigma(bars: list[Bar]) -> Decimal:
    """典型价对 VWAP 的成交量加权标准差（band 宽度）。

    σ = sqrt( Σ Vᵢ·(tpᵢ − VWAP)² / Σ Vᵢ ),  tpᵢ = (H+L+C)/3
    与 session_vwap 同一段 bars、同一异常约定。追高闸 band 判定用。
    """
    if not bars:
        raise ValueError("vwap_sigma 需要至少一根 bar")
    vw = session_vwap(bars)          # 复用（内部已校验总量>0）
    three = Decimal(3)
    wsq = Decimal(0)
    vol = Decimal(0)
    for b in bars:
        tp = (b.high + b.low + b.close) / three
        wsq += (tp - vw) * (tp - vw) * b.volume
        vol += b.volume
    return (wsq / vol).sqrt()


def sigma_with_atr_floor(
    sigma: Decimal | None,
    atr_value: Decimal | None,
    coefficient: Decimal,
) -> Decimal | None:
    """σ 的 ATR 地板：max(σ, c×ATR)。

    σ_session 的样本随交易日从 0 根 bar 长起，开盘早段是高方差的垃圾估计，
    而它出现在追高闸的**分母**上（|price−vwap|/σ），分母偏小就把倍数推爆。
    ATR 取自跨交易日的滚动窗口，开盘第一分钟就有值，量纲与 σ 一致，可作下界。
    地板只在 σ 尚未长起来时生效，长起来后 max 自动失效 —— 无切换断崖。

    σ 为 None 表示「本时段没有 VWAP 锚点」（盘前、RTH 未开始、跨日失效）。
    此时即便 ATR 有值也必须返回 None：造一个没有 VWAP 支撑的分母出来，
    等于让规则 7 拿今日价去比一个不存在的锚点。返回 None 走下游 fail-open。
    """
    if sigma is None:
        return None
    if atr_value is None:
        return sigma
    return max(sigma, atr_value * coefficient)


def true_ranges(bars: list[Bar]) -> list[Decimal]:
    """逐根真实波幅 TR。第一根用 H−L；其后取 max(H−L, |H−prevC|, |L−prevC|)。"""
    out: list[Decimal] = []
    prev_close: Decimal | None = None
    for b in bars:
        if prev_close is None:
            out.append(b.high - b.low)
        else:
            out.append(max(b.high - b.low, abs(b.high - prev_close), abs(b.low - prev_close)))
        prev_close = b.close
    return out


def atr(bars: list[Bar], period: int) -> Decimal:
    """ATR = 最近 period 根 TR 的简单均值（SMA-of-TR）。"""
    if period <= 0:
        raise ValueError("period 必须为正")
    trs = true_ranges(bars)
    if len(trs) < period:
        raise ValueError(f"bar 数不足：需 {period} 根，实得 {len(trs)}")
    window = trs[-period:]
    return sum(window, Decimal(0)) / Decimal(period)


# 2026-09-01 删除（随震荡闸 / 方向闸 / 动能闸三条规则一并下线，见
# app/validator/core.py 文件头）：
#   PIVOT_ER_WINDOW / PIVOT_ER_MIN_LEG / pivot_efficiency_ratio  拐点效率比（规则 5）
#   efficiency_ratio                                              旧口径效率比
#   atr_distance                                                  距 VWAP 的 ATR 倍数
#                                                                 （更早那版追高闸的判据）
#   macd_hist_closed_and_peak / macd_hist_shrinking               5min MACD 动能（stall）
#   ema_direction                                                 EMA9/21 方向（规则 6，
#                                                                 含 5min 死区与收缩判据）
#
# 本模块现在只剩「卡片要显示、拒单要留痕」的那几个量：session VWAP、σ、
# σ 的 ATR 地板、TR / ATR。别把上面那些捡回来 —— 捡回来就会有人拿它当第二套判据。
