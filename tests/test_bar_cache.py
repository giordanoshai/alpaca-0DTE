"""回测 K 线缓存 app/backtest/bar_cache.py。

用临时目录 + TT_BAR_CACHE 造数据，不依赖任何真实数据盘或网络。
"""
import pandas as pd
import pytest

from app.backtest import bar_cache


def _write(tmp_path, name: str, index, **cols):
    n = len(index)
    df = pd.DataFrame({
        "open": cols.get("open", [1.0] * n),
        "high": cols.get("high", [2.0] * n),
        "low": cols.get("low", [0.5] * n),
        "close": cols.get("close", [1.5] * n),
        "volume": cols.get("volume", [100.0] * n),
    }, index=index)
    df.to_parquet(tmp_path / name)
    return df


def _idx(start: str, n: int, tz="America/New_York"):
    return pd.date_range(start, periods=n, freq="1min", tz=tz)


@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.setenv("TT_BAR_CACHE", str(tmp_path))
    # 屏蔽自有目录与同级项目数据盘，避免测试读到真实数据
    monkeypatch.setattr(bar_cache, "CACHE_DIR", tmp_path / "_none")
    monkeypatch.setattr(bar_cache, "cache_roots", lambda: [tmp_path])
    return tmp_path


def test_按标的发现文件(cache):
    _write(cache, "QQQ_1m_2026-01-01_2026-01-02.parquet", _idx("2026-01-01 09:30", 10))
    _write(cache, "SPY_1m_2026-01-01_2026-01-02.parquet", _idx("2026-01-01 09:30", 10))
    assert bar_cache.available() == {"QQQ": 1, "SPY": 1}


def test_同一标的多文件拼接且去重(cache):
    _write(cache, "QQQ_1m_a.parquet", _idx("2026-01-01 09:30", 10))
    _write(cache, "QQQ_1m_b.parquet", _idx("2026-01-01 09:35", 10))   # 与前者重叠 5 根
    df = bar_cache.load("QQQ")
    assert len(df) == 15
    assert df.index.is_monotonic_increasing
    assert not df.index.has_duplicates


def test_缺标的时报错并列出现有的(cache):
    _write(cache, "QQQ_1m_a.parquet", _idx("2026-01-01 09:30", 10))
    with pytest.raises(FileNotFoundError, match="QQQ"):
        bar_cache.load("NVDA")


def test_end按自然日闭区间(cache):
    # 调用方想的是「到这天为止」, 传 01-02 必须含 01-02 全天
    _write(cache, "QQQ_1m_a.parquet", _idx("2026-01-01 09:30", 60 * 24 * 2))
    df = bar_cache.load("QQQ", "2026-01-01", "2026-01-02")
    assert str(df.index.max().date()) == "2026-01-02"


def test_naive索引按ET解释(cache):
    _write(cache, "QQQ_1m_a.parquet", _idx("2026-01-01 09:30", 10, tz=None))
    assert str(bar_cache.load("QQQ").index.tz) == "America/New_York"


def test_非ET时区索引被转成ET(cache):
    _write(cache, "QQQ_1m_a.parquet", _idx("2026-01-01 14:30", 10, tz="UTC"))
    df = bar_cache.load("QQQ")
    assert str(df.index.tz) == "America/New_York"
    assert df.index[0].hour == 9 and df.index[0].minute == 30


def test_缺列直接报错而不是静默补零(cache):
    pd.DataFrame({"open": [1.0], "close": [1.0]},
                 index=_idx("2026-01-01 09:30", 1)).to_parquet(cache / "QQQ_1m_a.parquet")
    with pytest.raises(ValueError, match="缺列"):
        bar_cache.load("QQQ")


def test_rth只留盘中(cache):
    _write(cache, "QQQ_1m_a.parquet", _idx("2026-01-01 04:00", 60 * 16))
    df = bar_cache.load("QQQ")
    r = bar_cache.rth(df)
    assert len(r) == 390
    assert r.index[0].strftime("%H:%M") == "09:30"
    assert r.index[-1].strftime("%H:%M") == "15:59"


def test_体检给出日数与完整度(cache):
    _write(cache, "QQQ_1m_a.parquet", _idx("2026-01-01 09:30", 390))
    q = bar_cache.quality(bar_cache.load("QQQ"))
    assert q["days"] == 1 and q["minutes"] == 390 and q["complete_ratio"] == 1.0
    assert q["suspect_split"] == []


def test_体检标出疑似拆股日(cache):
    # 两天, 第二天收盘只有第一天的 1/10 —— 典型未复权拆股
    idx = _idx("2026-01-01 09:30", 390).append(_idx("2026-01-02 09:30", 390))
    _write(cache, "QQQ_1m_a.parquet", idx, close=[100.0] * 390 + [10.0] * 390)
    q = bar_cache.quality(bar_cache.load("QQQ"))
    assert q["suspect_split"] == ["2026-01-02"]


# --- fetch 的行情源（2026-08-22） ---------------------------------------
#
# 起因：`scripts/gate_at.py` 要能回答「昨天某时刻会不会拦」，但免费档拉最近的
# SIP 数据会被 Alpaca 拒（"subscription does not permit querying recent SIP
# data"），实测同一区间改用 IEX 就能拉到。所以 fetch 必须能指定行情源。
# 默认仍不传 —— 保持历史区间走服务端默认（SIP），已落盘的缓存口径不变。


class _FakeBar:
    def __init__(self, ts):
        self.timestamp = ts
        self.open = self.high = self.low = self.close = 1.0
        self.volume = 100.0


class _FakeStock:
    """只记下请求里的 feed，然后回一根 bar。"""

    def __init__(self, sink):
        self.sink = sink

    def get_stock_bars(self, req):
        self.sink["feed"] = req.feed
        ts = pd.Timestamp("2026-08-21 14:00", tz="UTC")

        class _Resp:
            data = {"IBIT": [_FakeBar(ts)]}

        return _Resp()


@pytest.fixture
def fake_alpaca(tmp_path, monkeypatch):
    from app.integrations.alpaca import client as ac
    sink: dict = {}
    monkeypatch.setattr(ac.CLIENTS, "stock", lambda: _FakeStock(sink), raising=False)
    monkeypatch.setattr(bar_cache, "CACHE_DIR", tmp_path)
    return sink


def test_fetch默认不指定行情源(fake_alpaca):
    """不传就走服务端默认。历史区间一直是这么拉的，缓存口径不能因本次改动漂移。"""
    bar_cache.fetch("IBIT", "2026-08-20", "2026-08-21")
    assert fake_alpaca["feed"] is None


def test_fetch可指定IEX(fake_alpaca):
    """免费档只有 IEX 拿得到最近几天的数据 —— 拿不到就没法回答「昨天会不会拦」。"""
    bar_cache.fetch("IBIT", "2026-08-20", "2026-08-21", feed="iex")
    assert fake_alpaca["feed"] == "iex"


# --- 别让替代行情源污染标定样本（2026-08-22） ---------------------------
#
# `fetch(..., feed="iex")` 落盘的文件名带 `_iex` 后缀。IEX 只占全市场成交的百分之几，
# 量与 VWAP 都跟默认(SIP)的那批对不上。若 `load()` 把两种文件一锅端，标定脚本的
# 样本量会随「谁昨天顺手拉过一次」而变 —— 实测混进 1,863 根就让全量决策点从
# 2,304,568 变成 2,306,431，而报告上的数字看起来照样一本正经。
# 所以默认只读默认源；要用替代源的（如 gate_at.py 查昨天）显式传 alt_feed=True。


def test_load默认不读替代行情源(cache):
    _write(cache, "TSLA_1m_2026-01-01_2026-01-02.parquet", _idx("2026-01-01 09:30", 3))
    _write(cache, "TSLA_1m_2026-08-20_2026-08-21_iex.parquet",
           _idx("2026-08-20 09:30", 3))
    assert len(bar_cache.load("TSLA")) == 3


def test_load显式要替代源时才带上(cache):
    _write(cache, "TSLA_1m_2026-01-01_2026-01-02.parquet", _idx("2026-01-01 09:30", 3))
    _write(cache, "TSLA_1m_2026-08-20_2026-08-21_iex.parquet",
           _idx("2026-08-20 09:30", 3))
    assert len(bar_cache.load("TSLA", alt_feed=True)) == 6
