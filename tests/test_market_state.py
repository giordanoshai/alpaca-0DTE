"""② 市场状态层 VWAP / σ / ATR 单元测试（手算 fixture 校验）。

2026-09-01：效率比与「距 VWAP 的 ATR 倍数」两组用例随震荡闸、追高闸一并删除。
"""
from __future__ import annotations

import math
from decimal import Decimal as D

import pytest

from app.market_state import (
    Bar,
    atr,
    session_vwap,
    sigma_with_atr_floor,
    true_ranges,
    vwap_sigma,
)


def bar(ts, o, h, l, c, v):
    return Bar(ts, D(str(o)), D(str(h)), D(str(l)), D(str(c)), D(str(v)))


# ---- VWAP ----

def test_session_vwap_volume_weighted():
    bars = [bar(1, 9, 10, 8, 9, 100),    # tp=9  ×100 = 900
            bar(2, 9, 12, 10, 11, 200),  # tp=11 ×200 = 2200
            bar(3, 11, 11, 9, 10, 100)]  # tp=10 ×100 = 1000
    # (900+2200+1000)/(400) = 10.25
    assert session_vwap(bars) == D("10.25")


def test_session_vwap_single_bar():
    bars = [bar(1, 100, 102, 98, 101, 50)]  # tp=(102+98+101)/3=100.333...
    assert abs(float(session_vwap(bars)) - (102 + 98 + 101) / 3) < 1e-9


# ---- VWAP σ (band 宽度) ----

def test_vwap_sigma_volume_weighted():
    # 与 test_session_vwap_volume_weighted 同一组 bar：VWAP=10.25
    # σ² = [100·(9−10.25)² + 200·(11−10.25)² + 100·(10−10.25)²] / 400
    #    = [156.25 + 112.5 + 6.25] / 400 = 0.6875 → σ = 0.829156…
    bars = [bar(1, 9, 10, 8, 9, 100),
            bar(2, 9, 12, 10, 11, 200),
            bar(3, 11, 11, 9, 10, 100)]
    assert math.isclose(float(vwap_sigma(bars)), math.sqrt(0.6875), rel_tol=1e-9)


def test_vwap_sigma_single_bar_is_zero():
    # 单 bar：典型价 == VWAP → 离散度为 0
    bars = [bar(1, 100, 102, 98, 101, 50)]
    assert vwap_sigma(bars) == D("0")


def test_vwap_sigma_empty_raises():
    with pytest.raises(ValueError):
        vwap_sigma([])


def test_watcher_inline_sigma_matches_vwap_sigma_reference():
    """锁 app.market_state.vwap_sigma（Decimal 参考实现）与
    app/watcher/watcher_manager.py `_recalculate_minute_state` 内联 float 公式
    的数值等价性。两处各自独立手写同一 σ 公式（成交量加权 VWAP band 宽度）；
    watcher 侧不是可调用函数，这里逐行复刻其精确浮点算术。任一侧公式被改动
    导致二者分叉，本测试即失败——这是追高闸跨数据源行为一致性的回归闸。
    """
    bars = [bar(1, 9, 10, 8, 9, 100),
            bar(2, 9, 12, 10, 11, 200),
            bar(3, 11, 11, 9, 10, 100)]

    # ---- 复刻 watcher_manager._recalculate_minute_state 的精确浮点算术 ----
    highs = [float(b.high) for b in bars]
    lows = [float(b.low) for b in bars]
    closes = [float(b.close) for b in bars]
    volumes = [float(b.volume) for b in bars]

    tp = [(h + l + c) / 3 for h, l, c in zip(highs, lows, closes)]
    tp_vol = [t * v for t, v in zip(tp, volumes)]
    vol_sum = sum(volumes)
    vwap_val = sum(tp_vol) / vol_sum

    tp2 = [(h + l + c) / 3.0 for h, l, c in zip(highs, lows, closes)]
    var = sum((t - vwap_val) ** 2 * v for t, v in zip(tp2, volumes)) / vol_sum
    sigma_val = var ** 0.5

    assert math.isclose(sigma_val, float(vwap_sigma(bars)), rel_tol=1e-9)


# ---- True Range / ATR ----

def test_true_ranges_first_uses_high_low():
    bars = [bar(1, 9, 10, 9, 9.5, 10),
            bar(2, 9.5, 11, 9.5, 10.5, 10),   # max(1.5, |11-9.5|=1.5, |9.5-9.5|=0)=1.5
            bar(3, 10.5, 10.8, 10, 10.2, 10)]  # max(0.8, |10.8-10.5|=0.3, |10-10.5|=0.5)=0.8
    assert true_ranges(bars) == [D("1"), D("1.5"), D("0.8")]


def test_atr_sma_of_true_ranges():
    bars = [bar(1, 9, 10, 9, 9.5, 10),
            bar(2, 9.5, 11, 9.5, 10.5, 10),
            bar(3, 10.5, 10.8, 10, 10.2, 10)]
    # (1 + 1.5 + 0.8) / 3 = 1.1
    assert atr(bars, period=3) == D("1.1")


def test_atr_uses_only_last_period():
    bars = [bar(1, 9, 20, 0, 10, 10),   # TR=20（应被 period=2 排除）
            bar(2, 10, 11, 9, 10.5, 10),
            bar(3, 10.5, 10.8, 10, 10.2, 10)]
    # 仅取最后 2 根 TR：bar2 TR=max(2,|11-10|=1,|9-10|=1)=2；bar3 TR=0.8 → (2+0.8)/2=1.4
    assert atr(bars, period=2) == D("1.4")


# ---- σ 的 ATR 地板 ----

def test_地板生效_sigma小于atr时抬到atr():
    assert sigma_with_atr_floor(D("0.05"), D("0.40"), D("1.0")) == D("0.40")


def test_地板失效_sigma大于atr时原样返回():
    assert sigma_with_atr_floor(D("0.80"), D("0.40"), D("1.0")) == D("0.80")


def test_系数参与运算而非写死1():
    assert sigma_with_atr_floor(D("0.05"), D("0.40"), D("0.5")) == D("0.20")


def test_sigma为None时恒返回None():
    """σ=None 表示本时段无 VWAP 锚点（盘前/跨日失效）。

    此时不得因为 ATR 有值就凭空造出一个 σ —— 那会让规则 7 拿着
    一个没有 VWAP 支撑的分母去判追高。必须保持 None，走下游 fail-open。
    """
    assert sigma_with_atr_floor(None, D("0.40"), D("1.0")) is None


def test_atr为None时不套地板():
    """ATR 缺失只发生在极早期预热（bar 不足 15 根），退回现状 σ。"""
    assert sigma_with_atr_floor(D("0.05"), None, D("1.0")) == D("0.05")


def test_atr为0时退化为sigma():
    assert sigma_with_atr_floor(D("0.05"), D("0"), D("1.0")) == D("0.05")


def test_浮点残差sigma被地板抬起():
    """单根 bar 的 σ 真值是 0，浮点下可能留下 ~1e-13 的残差。

    不套地板时 |price−vwap|/1e-13 会爆到 1e12 量级并触发「追高」——
    实测 505 组样本中开盘第 1 分钟有 7.1% 因此被拦死。
    """
    assert sigma_with_atr_floor(D("1e-13"), D("0.40"), D("1.0")) == D("0.40")

