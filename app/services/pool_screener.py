# -*- coding: utf-8 -*-
"""标的池筛选器（肉票 Screener）—— 定期体检/换血盯盘标的池。

背景（2026-07-11 定稿，详见 step7_trendscore_filter/筛选逻辑/5_盯盘标的池筛选_2026-07-11.md）：
  选票标准 = 日振幅 ≥5.5%（烈度） × 期权流动性（近月成交/持仓） × tape 干净（尺度无关口径）。
  标的性格会漂移（TSLA 这半年只剩 3.2% 振幅），池子需要定期重跑；本模块把筛选流程固化成功能。

三组指标（RTH 1m，默认回看 6 个月）：
  烈度   amp_day   日振幅中位数%；d5 = 振幅≥5% 天占比（参考列）
  tape   dvol_1m   每分钟成交额中位数($M)
         tick_bar  每根 1m bar 含多少个 tick（<5 = 楼梯状锯齿）
         gap_ratio bar 间跳价中位数 / bar 高中位数（尺度无关；⚠不要用绝对 bps 判锯齿，
                   高波动票会被误判——2026-07-11 复检教训，AAOI 曾因此被误杀）
         coverage  RTH 分钟覆盖率；frozen = h==l 冻结 bar 占比
  期权   opt_vol / opt_oi  来自期权流动性快照 CSV（近月总成交 / 总持仓）

数据源：DB stock_ohlcv_min（覆盖≥60 交易日时优先）→ Alpaca 1m（SIP，带本地 parquet 缓存）。
本模块**只读不写库**：输出报告 + 与现有 watchlist 的 diff 建议，换血由人工确认（TODO P2）。

CLI：
  .venv/Scripts/python.exe -m app.services.pool_screener \
      --csv step7_trendscore_filter/筛选逻辑/options_analysis.csv \
      --extra APP --months 6 --out .scratch/pool_screen_report.csv
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
ET = "America/New_York"
RTH_LO, RTH_HI = 570, 960
TICK = 0.01

# 大盘 context（固定保留，不参与筛选）
CONTEXT_TICKERS = ["SPY", "QQQ", "IWM", "^VIX"]


def _get_db_dsn() -> str:
    """动态获取只读数据库连接串（优先从 app.db 获取，其次读环境变量）。"""
    try:
        from app.db import database_url
        return database_url()
    except Exception:
        return os.environ.get("DATABASE_URL", "")


@dataclass
class ScreenConfig:
    """门槛全部集中在这里。默认值 = 2026-07-11 定稿（能复现当日 13+AAOI 的结果）。"""
    months: int = 6                  # 回看窗口
    amp_min: float = 5.5             # 烈度门：日振幅中位数 ≥ %
    opt_vol_min: int = 30_000        # 期权门：近月总成交 ≥ 张（与 opt_oi_min 取或）
    opt_oi_min: int = 200_000        # 期权门：近月总持仓 ≥ 张
    dvol_min: float = 1.0            # tape 门：每分钟成交额 ≥ $M
    tick_bar_min: float = 5.0        # tape 门：每 bar tick 数 ≥（<5 = 楼梯锯齿）
    gap_ratio_max: float = 0.12      # tape 门：跳价/bar高 ≤（尺度无关锯齿口径）
    coverage_min: float = 0.997      # tape 门：RTH 分钟覆盖率 ≥
    price_min: float = 15.0          # tape 门：价格 ≥ $（tick 粒度）
    min_days: int = 40               # 数据不足此交易日数则 no_data
    cache_dir: Path = field(default_factory=lambda: ROOT / ".scratch/batch_data")


# ---------------------------------------------------------------- 数据源 --- #
def _load_db(ticker: str, start: str, end: str) -> pd.DataFrame:
    dsn = _get_db_dsn()
    if not dsn:
        return pd.DataFrame()
    import psycopg2
    try:
        con = psycopg2.connect(dsn, connect_timeout=5)
    except Exception:
        return pd.DataFrame()
    try:
        df = pd.read_sql(
            """select ts, open, high, low, close, volume from stock_ohlcv_min
               where ticker=%s and timeframe='1m' and ts>=%s and ts<%s order by ts""",
            con, params=(ticker, start, end))
    except Exception:
        return pd.DataFrame()
    finally:
        con.close()
    if df.empty:
        return df
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    df = df.set_index("ts").sort_index()
    return df[~df.index.duplicated(keep="last")].astype(float)


def _load_alpaca(ticker: str, start: str, end: str, cache_dir: Path) -> pd.DataFrame:
    """Alpaca 1m（SIP 优先/IEX 兜底），parquet 缓存复用。"""
    from alpaca.data.enums import DataFeed
    from alpaca.data.historical.stock import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    from app.ohlcv_provider.alpaca_prv import (ALPACA_PAPER_API_KEY,
                                               ALPACA_PAPER_SECRET_KEY)
    cache_dir.mkdir(parents=True, exist_ok=True)
    f = cache_dir / f"{ticker}_1m_alpaca.parquet"
    s, e = pd.Timestamp(start, tz="UTC"), pd.Timestamp(end, tz="UTC")
    if f.exists():
        old = pd.read_parquet(f)
        if len(old) and old.index.min() <= s + pd.Timedelta("5d") and \
                old.index.max() >= e - pd.Timedelta("5d"):
            return old.loc[s:e]
    client = StockHistoricalDataClient(ALPACA_PAPER_API_KEY, ALPACA_PAPER_SECRET_KEY)
    for feed in (DataFeed.SIP, DataFeed.IEX):
        try:
            req = StockBarsRequest(symbol_or_symbols=ticker, timeframe=TimeFrame.Minute,
                                   start=s.to_pydatetime(), end=e.to_pydatetime(), feed=feed)
            df = client.get_stock_bars(req).df
        except Exception:
            continue
        if df is None or df.empty:
            continue
        if isinstance(df.index, pd.MultiIndex):
            df = df.droplevel(0)
        df = df[["open", "high", "low", "close", "volume"]].sort_index()
        df = df[~df.index.duplicated(keep="last")].astype(float)
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC")
        df.to_parquet(f)
        (cache_dir / f"{ticker}_1m_alpaca.feed.txt").write_text(feed.value)
        return df
    return pd.DataFrame()


def load_1m(ticker: str, start: str, end: str, cfg: ScreenConfig) -> pd.DataFrame:
    df = _load_db(ticker, start, end)
    if len(df):
        days = pd.Series(df.index.tz_convert(ET).date).nunique()
        if days >= 60:
            return df
    return _load_alpaca(ticker, start, end, cfg.cache_dir)


# ---------------------------------------------------------------- 指标 ----- #
def ticker_metrics(df: pd.DataFrame, cfg: ScreenConfig) -> dict | None:
    et = df.index.tz_convert(ET)
    etmin = et.hour * 60 + et.minute
    rth = df[(etmin >= RTH_LO) & (etmin < RTH_HI)]
    dates = pd.Series(np.array(rth.index.tz_convert(ET).date), index=rth.index)
    n_days = dates.nunique()
    if n_days < cfg.min_days:
        return None
    o, h, l, c, v = (rth[x] for x in ("open", "high", "low", "close", "volume"))
    # 烈度
    amp = []
    for d in pd.unique(dates.values):
        day = rth[dates.values == d]
        if len(day) < 200:
            continue
        amp.append(100 * (day["high"].max() - day["low"].min()) / day["close"].iloc[-1])
    amp = np.array(amp)
    # tape（尺度无关）
    prev_c = c.shift(1)
    same_day = dates.eq(dates.shift(1))
    gap = (o - prev_c).abs()[same_day]
    rng = h - l
    med_rng = float(rng.median())
    price = float(c.median())
    return {
        "days": int(n_days),
        "amp_day": round(float(np.median(amp)), 2),
        "d5%": round(100 * float((amp >= 5).mean())),
        "dvol_1m": round(float((c * v).median()) / 1e6, 2),
        "tick_bar": round(price * float((rng / c).median()) / TICK, 1),
        "gap_ratio": round(float(gap.median()) / med_rng, 3) if med_rng > 0 else np.nan,
        "coverage": round(len(rth) / (n_days * 390), 3),
        "frozen%": round(100 * float((rng == 0).mean()), 1),
        "price": round(price, 1),
    }


# ---------------------------------------------------------------- 判定 ----- #
def verdict(m: dict, opt_vol: float | None, opt_oi: float | None,
            cfg: ScreenConfig) -> tuple[str, str]:
    """返回 (判定, 失败原因)。期权数据缺失时期权门记 '未核'（不拦截，报告注明）。"""
    fails = []
    if m["amp_day"] < cfg.amp_min:
        fails.append(f"振幅{m['amp_day']}<{cfg.amp_min}")
    opt_note = ""
    if opt_vol is None and opt_oi is None:
        opt_note = "期权未核"
    elif not ((opt_vol or 0) >= cfg.opt_vol_min or (opt_oi or 0) >= cfg.opt_oi_min):
        fails.append("期权流动性不足")
    if m["dvol_1m"] < cfg.dvol_min:
        fails.append(f"成交额{m['dvol_1m']}M<{cfg.dvol_min}M")
    if m["tick_bar"] < cfg.tick_bar_min:
        fails.append(f"tick/bar{m['tick_bar']}<{cfg.tick_bar_min}")
    if not np.isnan(m["gap_ratio"]) and m["gap_ratio"] > cfg.gap_ratio_max:
        fails.append(f"跳价/bar高{m['gap_ratio']}>{cfg.gap_ratio_max}")
    if m["coverage"] < cfg.coverage_min:
        fails.append(f"覆盖率{m['coverage']}")
    if m["price"] < cfg.price_min:
        fails.append(f"价格{m['price']}<{cfg.price_min}")
    v = "PASS" if not fails else "FAIL"
    reason = ";".join(fails) or opt_note
    if v == "PASS" and opt_note:
        v = "PASS(期权未核)"
    return v, reason


# ---------------------------------------------------------------- 主流程 --- #
def run_screen(candidates: list[str],
               opt_liq: dict[str, tuple[float, float]] | None = None,
               current_pool: list[str] | None = None,
               cfg: ScreenConfig | None = None) -> pd.DataFrame:
    """candidates: 候选 ticker；opt_liq: {ticker:(opt_vol,opt_oi)}；
    current_pool: 现有肉票池（出 diff 用）。返回逐 ticker 报告 DataFrame。"""
    cfg = cfg or ScreenConfig()
    end = datetime.now(timezone.utc).date().isoformat()
    start = (datetime.now(timezone.utc) - timedelta(days=cfg.months * 31)).date().isoformat()
    opt_liq = opt_liq or {}
    rows = []
    for i, t in enumerate(sorted(set(x.upper() for x in candidates)), 1):
        try:
            df = load_1m(t, start, end, cfg)
            m = ticker_metrics(df, cfg) if len(df) else None
            if m is None:
                rows.append({"ticker": t, "判定": "no_data", "原因": "数据不足"})
                continue
            ov, oi = opt_liq.get(t, (None, None))
            v, reason = verdict(m, ov, oi, cfg)
            rows.append({"ticker": t, **m, "opt_vol": ov, "opt_oi": oi,
                         "判定": v, "原因": reason})
        except Exception as e:  # 单 ticker 失败不拖垮整批
            rows.append({"ticker": t, "判定": "error", "原因": f"{type(e).__name__}: {e}"})
        if i % 30 == 0:
            print(f"  ...{i}/{len(set(candidates))}", flush=True)
    rep = pd.DataFrame(rows)
    if current_pool:
        cur = {x.upper() for x in current_pool}
        newp = set(rep[rep["判定"].str.startswith("PASS")]["ticker"])
        rep.attrs["diff"] = {"建议加入": sorted(newp - cur), "建议移出": sorted(cur - newp),
                             "维持": sorted(cur & newp)}
    return rep


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    p = argparse.ArgumentParser(description="标的池筛选器（肉票 screener）")
    p.add_argument("--csv", default="step7_trendscore_filter/筛选逻辑/options_analysis.csv",
                   help="候选宇宙 CSV（含 ticker + 期权流动性列）")
    p.add_argument("--extra", nargs="*", default=[],
                   help="额外候选（不在 CSV 内，期权门记'未核'）")
    p.add_argument("--pool", nargs="*", default=None,
                   help="现有肉票池（出进/出 diff）；缺省用 2026-07-11 定稿 13 只")
    p.add_argument("--months", type=int, default=6)
    p.add_argument("--amp-min", type=float, default=5.5)
    p.add_argument("--out", default=".scratch/pool_screen_report.csv")
    a = p.parse_args()

    cfg = ScreenConfig(months=a.months, amp_min=a.amp_min)
    csvp = ROOT / a.csv
    opts = pd.read_csv(csvp)
    opts["ticker"] = opts["ticker"].str.upper()
    opt_liq = {r.ticker: (float(r.near_term_total_volume), float(r.near_term_total_open_interest))
               for r in opts.itertuples()}
    candidates = opts["ticker"].tolist() + [t.upper() for t in a.extra]
    pool = a.pool if a.pool is not None else \
        ["APP", "MU", "IREN", "NBIS", "CRWV", "CRCL", "ASTS", "RKLB", "SNDK",
         "IONQ", "APLD", "BMNR", "SMCI"]

    print(f"候选 {len(set(candidates))} 个 | 窗口近 {cfg.months} 个月 | 振幅门 ≥{cfg.amp_min}%")
    rep = run_screen(candidates, opt_liq, pool, cfg)
    outp = ROOT / a.out
    rep.to_csv(outp, index=False, encoding="utf-8-sig")
    ok = rep[rep["判定"].str.startswith("PASS")].sort_values("amp_day", ascending=False)
    print(f"\nPASS {len(ok)} 个：")
    cols = ["ticker", "amp_day", "d5%", "dvol_1m", "tick_bar", "gap_ratio",
            "price", "opt_vol", "opt_oi", "判定"]
    print(ok[cols].to_string(index=False))
    d = rep.attrs.get("diff")
    if d:
        print(f"\n与现有池 diff：加入 {d['建议加入']} | 移出 {d['建议移出']} | 维持 {len(d['维持'])} 只")
    print(f"\n完整报告：{outp}（判定含失败原因列）")
    print("提示：本工具只读不写库；确认换血后手动更新 user_watchlist（自动化见 TODO.md P2）。")


if __name__ == "__main__":
    main()
