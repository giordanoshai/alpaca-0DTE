"""回测用 1min K 线本地缓存。

设计取自 tt_strategy/backtest/data 的既有约定：**一个标的一个 parquet 文件、
按 `{SYMBOL}_1m_{START}_{END}.parquet` 命名、索引是 America/New_York 时区的
DatetimeIndex、列固定 open/high/low/close/volume**。同一标的允许多个区间文件
并存, `load()` 会把它们拼起来去重。

为什么用本地 parquet 而不是每次查库
-----------------------------------
标定/回放动辄要几十只标的 × 一年分钟线（单只约 9.5 万根 RTH）。走只读库
每次几十秒且受网络与库负载影响, 结果不可复现；parquet 读一只 5MB 文件约 50ms,
且落盘后**任何人任何时候重跑都是同一份输入** —— 标定报告的可复现性靠这个。

数据落在 `data/bars/`(已在 .gitignore 内, 不进版本库)。另可用环境变量
`TT_BAR_CACHE` 追加只读搜索目录(多个用 os.pathsep 分隔), 例如指向
tt_strategy 的既有数据盘, 免去重复下载。

    from app.backtest.bar_cache import load, rth, quality
    df = load("QQQ", "2025-06-02", "2026-05-22")   # ETH 全时段
    day = rth(df)                                   # 只留 09:30-16:00 ET

补数据:
    .venv/Scripts/python.exe -m app.backtest.bar_cache fetch NVDA 2026-06-01 2026-08-08
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import pandas as pd

ET = "America/New_York"
COLUMNS = ["open", "high", "low", "close", "volume"]
RTH_START, RTH_END = pd.Timestamp("09:30").time(), pd.Timestamp("16:00").time()
EXPECTED_RTH_BARS = 390          # 09:30-16:00 = 6.5h
_NAME_RE = re.compile(r"^(?P<sym>[A-Z^.\-]+)_1m_(?P<rest>.+)\.parquet$", re.I)

_REPO = Path(__file__).resolve().parents[2]
#: 写入目录(fetch 落盘处)。读取时同样会搜索它。
CACHE_DIR = _REPO / "data" / "bars"


def cache_roots() -> list[Path]:
    """按优先级返回所有搜索目录: 自有缓存 → TT_BAR_CACHE → 同级 tt_strategy 数据盘。"""
    roots = [CACHE_DIR]
    for raw in os.environ.get("TT_BAR_CACHE", "").split(os.pathsep):
        if raw.strip():
            roots.append(Path(raw.strip()))
    # 同级项目的既有数据盘：相对仓库定位, 不写死绝对路径; 不存在就自动跳过
    roots.append(_REPO.parent / "tt_strategy" / "backtest" / "data")
    return [r for r in roots if r.is_dir()]


#: 文件名尾部带 `_<行情源>` 的是替代源(如 `_iex`)。默认源的尾部是日期, 必是数字。
_ALT_FEED_RE = re.compile(r"_[a-z]+$")


def _files_for(symbol: str, *, alt_feed: bool = False) -> list[Path]:
    """该标的的所有缓存文件。`alt_feed=False` 时剔除替代行情源的那些。

    剔除是默认行为, 因为标定/回放的样本量必须只由「缓存里有哪些交易日」决定,
    不能由「谁昨天顺手用 --fetch 拉过一次 IEX」决定 —— 后者会让同一个脚本
    在两台机器上跑出不同的总数, 而报告上的数字看起来照样一本正经。
    """
    sym = symbol.upper()
    out: list[Path] = []
    for root in cache_roots():
        for p in root.glob("*.parquet"):
            m = _NAME_RE.match(p.name)
            if not m or m.group("sym").upper() != sym:
                continue
            if not alt_feed and _ALT_FEED_RE.search(m.group("rest")):
                continue
            out.append(p)
    return out


def available() -> dict[str, int]:
    """{标的: 文件数}，用于确认缓存里有什么。"""
    out: dict[str, int] = {}
    for root in cache_roots():
        for p in root.glob("*.parquet"):
            m = _NAME_RE.match(p.name)
            if m:
                out[m.group("sym").upper()] = out.get(m.group("sym").upper(), 0) + 1
    return dict(sorted(out.items()))


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = [str(c).lower() for c in df.columns]
    missing = [c for c in COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"缓存文件缺列 {missing}; 实际列 {list(df.columns)}")
    idx = pd.DatetimeIndex(df.index)
    # 落盘约定是 ET；naive 一律按 ET 解释，带别的时区则转过来
    df.index = idx.tz_localize(ET) if idx.tz is None else idx.tz_convert(ET)
    return df[COLUMNS].astype(float).sort_index()


def load(symbol: str, start: str | None = None, end: str | None = None,
         *, alt_feed: bool = False) -> pd.DataFrame:
    """读 1min K 线，返回 ET 时区、含盘前盘后的 OHLCV。

    `end` 按**自然日闭区间**处理（传 "2026-05-22" 会含当天全天），因为调用方
    想的是「到这天为止」，而不是「到这天 00:00 为止」。

    `alt_feed=True` 才把替代行情源(如 `_iex` 后缀的文件)一起读进来。默认不读,
    理由见 `_files_for`；查最近几天(免费档只有 IEX 拿得到)的调用方才该打开它。
    """
    files = _files_for(symbol, alt_feed=alt_feed)
    if not files:
        raise FileNotFoundError(
            f"缓存里没有 {symbol.upper()} 的 1m 数据; 现有 {sorted(available())}\n"
            f"搜索目录: {[str(r) for r in cache_roots()]}")

    frames = [_normalize(pd.read_parquet(p)) for p in files]
    df = pd.concat(frames)
    df = df[~df.index.duplicated(keep="last")].sort_index()

    if start is not None:
        df = df[df.index >= pd.Timestamp(start, tz=ET)]
    if end is not None:
        df = df[df.index < pd.Timestamp(end, tz=ET) + pd.Timedelta(days=1)]
    return df


def rth(df: pd.DataFrame) -> pd.DataFrame:
    """只留常规交易时段 09:30–16:00 ET。"""
    return df[[RTH_START <= t < RTH_END for t in df.index.time]]


def quality(df: pd.DataFrame, split_gap: float = 0.25) -> dict:
    """数据体检: RTH 日数、完整度、疑似拆股日。

    `split_gap`: 日收盘环比跳空超过该比例即列为可疑。**不自动剔除** —— 低价票
    ($1 量级)隔夜真能跳 30%, 与 10:1 拆股在数值上无法区分, 只能人工看一眼。
    """
    r = rth(df)
    if r.empty:
        return {"days": 0, "minutes": 0, "complete_ratio": 0.0, "suspect_split": []}
    per_day = r.groupby(r.index.date).size()
    daily_close = r["close"].groupby(r.index.date).last()
    chg = (daily_close / daily_close.shift() - 1).abs()
    return {
        "days": int(len(per_day)),
        "minutes": int(len(r)),
        "first_day": str(per_day.index[0]),
        "last_day": str(per_day.index[-1]),
        "complete_ratio": float((per_day >= EXPECTED_RTH_BARS * 0.95).mean()),
        "suspect_split": [str(d) for d in chg[chg > split_gap].index],
    }


def fetch(symbol: str, start: str, end: str, *, include_eth: bool = True,
          feed: str | None = None) -> Path:
    """从 Alpaca 拉 1min K 线并落盘, 返回写入的文件路径。

    复用 `app.integrations.alpaca.client.CLIENTS`（密钥与代理都在那儿处理），
    **不要另起一套客户端**：tt_strategy 那份 provider 把 paper 密钥硬编码在源码里,
    不能照搬。

    `feed` 不传就走服务端默认（本账户的历史区间= SIP，已落盘的缓存都是这个口径）。
    最近几天必须传 "iex"：免费档查近期 SIP 会被拒（"subscription does not permit
    querying recent SIP data"），同一区间换 IEX 就能拉到。**两者不是同一份行情** ——
    IEX 只占全市场成交的百分之几，量与 VWAP 都会偏，别把两种 feed 的文件混在
    同一个标定里；文件名带 -iex 后缀就是为了让它一眼可辨。
    """
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    from app.integrations.alpaca.client import CLIENTS

    sym = symbol.upper()
    kw = {"feed": feed} if feed else {}
    req = StockBarsRequest(
        symbol_or_symbols=sym, timeframe=TimeFrame.Minute,
        start=pd.Timestamp(start, tz=ET).tz_convert("UTC").to_pydatetime(),
        end=(pd.Timestamp(end, tz=ET) + pd.Timedelta(days=1)).tz_convert("UTC").to_pydatetime(),
        **kw,
    )
    bars = CLIENTS.stock().get_stock_bars(req).data.get(sym, [])
    if not bars:
        raise RuntimeError(f"Alpaca 未返回 {sym} {start}~{end} 的数据")

    df = pd.DataFrame(
        [{"ts": b.timestamp, "open": b.open, "high": b.high, "low": b.low,
          "close": b.close, "volume": b.volume} for b in bars]
    ).set_index("ts")
    df.index = pd.DatetimeIndex(df.index).tz_convert(ET)
    df = df[COLUMNS].astype(float).sort_index()
    if not include_eth:
        df = rth(df)

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    # feed 进文件名: 两种 feed 的量与 VWAP 不一样, 混在一个标定里会得出假结论
    tag = f"_{feed}" if feed else ""
    out = CACHE_DIR / f"{sym}_1m_{start}_{end}{tag}.parquet"
    df.to_parquet(out)
    return out


def _main(argv: list[str]) -> int:
    import io
    import sys
    if hasattr(sys.stdout, "buffer"):   # Windows 控制台默认 GBK
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd = argv[0]
    if cmd == "list":
        print(f"搜索目录: {[str(r) for r in cache_roots()]}")
        for sym, n in available().items():
            print(f"  {sym:<8} {n} 个文件")
        return 0
    if cmd == "check":
        for sym in (argv[1:] or list(available())):
            q = quality(load(sym))
            flag = f"  ⚠ 疑似拆股 {q['suspect_split']}" if q["suspect_split"] else ""
            print(f"{sym:<8}{q['days']:>5}日 {q['minutes']:>8}分钟 "
                  f"{q['first_day']}~{q['last_day']} 完整度 {q['complete_ratio']:.0%}{flag}")
        return 0
    if cmd == "fetch":
        if len(argv) not in (4, 5):
            print("用法: fetch <SYMBOL> <START> <END> [FEED]\n"
                  "  例: fetch NVDA 2026-06-01 2026-08-08\n"
                  "  最近几天免费档拉不到 SIP, 末尾加 iex:\n"
                  "      fetch NVDA 2026-08-15 2026-08-22 iex")
            return 1
        print(f"已写入 {fetch(argv[1], argv[2], argv[3], feed=(argv[4] if len(argv) == 5 else None))}")
        return 0
    print(f"未知命令 {cmd!r}; 可用: list / check / fetch")
    return 1


if __name__ == "__main__":
    raise SystemExit(_main(__import__("sys").argv[1:]))
