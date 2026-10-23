"""策略 H 即时档（confirm_bars=0）：交叉当根就提醒，拉开后再升级一次。

2026-09-05 换 3 只标的后取消「等待确认」：spread 阈值从**过滤条件**改为**升级条件**。
- 交叉当根 → 立即发 low（徽章闪烁，无声），一个交叉都不漏
- 之后拉开到阈值 → 再发一次 high（响铃+推送），每个交叉只升级一次
- 反向交叉 → 待升级状态作废

老档位（confirm_bars>=1）的「等 K 根确认 + 同向再响升级」行为必须保持不变，
本文件末尾有回归保护。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.strategies.signal_engine_h import SignalEngineH


def _bars(closes: list[float], start: str = "2026-09-04 09:30",
          rng: float = 0.02) -> pd.DataFrame:
    """把收盘价序列造成 1min K 线（naive 时间戳即视为 ET）。

    rng 控制每根的振幅（high/low 相对 close 的距离），进而决定 ATR ——
    spread = sep / ATR@交叉根，所以想造「没拉开」的场景必须把 rng 调大，
    否则横盘时 ATR 趋近 0 会让 spread 虚高。
    """
    idx = pd.date_range(start=start, periods=len(closes), freq="1min")
    c = np.asarray(closes, dtype=float)
    return pd.DataFrame(
        {"open": c, "high": c + rng, "low": c - rng, "close": c, "volume": 1000.0},
        index=idx,
    )


def _flat_then(base: float, n_flat: int, tail: list[float]) -> list[float]:
    """前 n_flat 根横盘（让两条 EMA 收敛贴合），再接一段自定义走势。"""
    return [base] * n_flat + tail


def _feed(engine: SignalEngineH, df: pd.DataFrame, start_at: int):
    """从第 start_at 根起逐根喂给引擎，返回 [(时间, 事件), ...]（只记非 None）。"""
    out = []
    for i in range(start_at, len(df) + 1):
        ev = engine.process(df.iloc[:i], pd.DataFrame(), float(df["close"].iloc[i - 1]), 0.0)
        if ev is not None:
            out.append((df.index[i - 1], ev))
    return out


INSTANT = {"confirm_bars": 0, "spread_threshold": 0.15, "gear": "instant_0.15"}


class TestInstantGear:
    def test_金叉当根就发无声信号(self):
        # 横盘 70 根后连续上涨 → 必然出金叉
        df = _bars(_flat_then(100.0, 70, [100 + 0.05 * i for i in range(1, 16)]))
        evs = _feed(SignalEngineH(INSTANT), df, 66)
        assert evs, "拉升段应当至少产生一个金叉信号"
        first = evs[0][1]
        assert first.direction == "bull"
        assert first.signal_type == "h_spread_bull"
        assert first.confidence == "low", "交叉当根只给无声徽章"
        assert first.metadata["confirm_bars"] == 0

    def test_拉开之后升级为响铃且只升级一次(self):
        df = _bars(_flat_then(100.0, 70, [100 + 0.05 * i for i in range(1, 26)]))
        evs = _feed(SignalEngineH(INSTANT), df, 66)
        types = [e.signal_type for _, e in evs]
        assert "h_spread_bull" in types, "应先有无声的交叉提醒"
        assert "h_spread_bull2" in types, "拉开后应升级出响铃信号"
        assert types.index("h_spread_bull") < types.index("h_spread_bull2"), "先提醒后升级"
        assert types.count("h_spread_bull2") == 1, "同一个交叉只升级一次"
        up = [e for _, e in evs if e.signal_type == "h_spread_bull2"][0]
        assert up.confidence == "high"
        assert up.metadata["spread_now"] >= INSTANT["spread_threshold"]

    def test_没拉开就一直不响(self):
        # 大 ATR（每根振幅 0.5 → ATR≈1.0）下只微涨 0.1：spread≈0.1 < 0.15，够不着升级线
        df = _bars(_flat_then(100.0, 70, [100.1] * 20), rng=0.5)
        evs = _feed(SignalEngineH(INSTANT), df, 66)
        types = [e.signal_type for _, e in evs]
        assert types.count("h_spread_bull2") == 0, "没拉开不许响铃"

    def test_死叉同样当根就发(self):
        df = _bars(_flat_then(100.0, 70, [100 - 0.05 * i for i in range(1, 26)]))
        evs = _feed(SignalEngineH(INSTANT), df, 66)
        assert evs
        assert evs[0][1].direction == "bear"
        assert evs[0][1].signal_type == "h_spread_bear"
        assert evs[0][1].confidence == "low"
        assert "h_spread_bear2" in [e.signal_type for _, e in evs], "跌透了应升级"

    def test_反向交叉让未升级的待办作废(self):
        # 金叉 → 没拉开就被叉回去 → 死叉；不应补发金叉的升级
        up = [100 + 0.05 * i for i in range(1, 8)]          # 造金叉
        down = [up[-1] - 0.12 * i for i in range(1, 20)]    # 迅速反向
        df = _bars(_flat_then(100.0, 70, up + down))
        evs = _feed(SignalEngineH(INSTANT), df, 66)
        order = [e.signal_type for _, e in evs]
        assert "h_spread_bull" in order and "h_spread_bear" in order
        i_bear = order.index("h_spread_bear")
        assert "h_spread_bull2" not in order[i_bear:], "死叉之后不该再补发金叉的升级"

    def test_盘前盘后不发信号(self):
        df = _bars(_flat_then(100.0, 70, [100 + 0.05 * i for i in range(1, 16)]),
                   start="2026-09-04 04:00")
        assert _feed(SignalEngineH(INSTANT), df, 66) == []

    def test_同一根bar重复调用只评估一次(self):
        df = _bars(_flat_then(100.0, 70, [100 + 0.05 * i for i in range(1, 16)]))
        eng = SignalEngineH(INSTANT)
        first = None
        for i in range(66, len(df) + 1):
            ev = eng.process(df.iloc[:i], pd.DataFrame(), 100.0, 0.0)
            if ev is not None and first is None:
                first = (i, ev)
                # 同一根再喂一次，必须返回 None
                assert eng.process(df.iloc[:i], pd.DataFrame(), 100.0, 0.0) is None
        assert first is not None


class TestLegacyGearUnchanged:
    """老档位（等 K 根确认）行为必须原样保留 —— 换档回滚时的护栏。"""

    LEGACY = {"confirm_bars": 3, "spread_threshold": 0.15, "gear": "spread3_0.15"}

    def test_老档位仍然等K根才发(self):
        df = _bars(_flat_then(100.0, 70, [100 + 0.05 * i for i in range(1, 16)]))
        evs = _feed(SignalEngineH(self.LEGACY), df, 66)
        assert evs, "老档位在拉升段仍应发信号"
        ev = evs[0][1]
        assert ev.metadata["confirm_bars"] == 3
        # 老档位下 spread 是过滤条件：发出来的必然已经达标
        assert ev.metadata["spread_now"] >= self.LEGACY["spread_threshold"]

    def test_老档位没拉开就完全不发(self):
        df = _bars(_flat_then(100.0, 70, [100.1] * 20), rng=0.5)
        assert _feed(SignalEngineH(self.LEGACY), df, 66) == []

    def test_confirm_bars负数被夹到0(self):
        eng = SignalEngineH({"confirm_bars": -3, "spread_threshold": 0.15})
        assert eng._confirm_bars == 0
