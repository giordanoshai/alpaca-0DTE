"""
watchlist 期权流动性体检（option liquidity probe）
──────────────────────────────────────────────────────────────────────────────
目的：用「买卖价差大 + 成交量低」这把尺子给 user_watchlist 每个非指数 ticker 的期权
可交易性打分排名，一次性体检出像 NBIS 那样 bid/ask 差别大、成交稀薄的坏票，供人工删票。

口径（2026-07-14 定稿，价差取最巿一边）：
  - 对每票取「最近一个非 0DTE 到期」的 ATM call 与 ATM put（按 |delta|≈0.5 选，delta 缺时按
    行权价最接近现价选）。
  - rel_spread = (ask-bid)/mid，call/put 各算一份，**worst = max(两者)** 作为价差判定。
  - top_depth = 报价档口最小挂单量（bid_size/ask_size 取小），薄档口 = 稀疏。
  - atm_vol / trade_count：ATM call+put 最近一个交易日的成交量与成交笔数（走日线 get_option_bars，
    因 snapshot 的 daily_bar/OI 官方常返回 None）。

判定门（集中在 ProbeConfig，可调）：
  - 价差：<8% OK / 8~15% 观察 / ≥15% 宽(红)
  - 流动性：atm_vol < 100 或 trade_count < 20 → 薄(红)
  - 两项都红 → 建议移除（NBIS 类）

只读，不写库；删票仍走人工确认（与 pool_screener 纪律一致）。

CLI:
  .venv/Scripts/python.exe -m app.services.option_liquidity_probe            # 全 watchlist
  .venv/Scripts/python.exe -m app.services.option_liquidity_probe --exclude-gear   # 只查非肉票
  .venv/Scripts/python.exe -m app.services.option_liquidity_probe --tickers NBIS AAOI HIMS
"""
from __future__ import annotations

import os
import sys
import time
import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import pandas as pd
import pytz
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import StockLatestTradeRequest, OptionBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

from app.ohlcv_provider.alpaca_prv import (
    AlpacaProvider,
    ALPACA_PAPER_API_KEY,
    ALPACA_PAPER_SECRET_KEY,
)

NY = pytz.timezone("America/New_York")
# 指数 context 永不参与（无个股期权 / 永远保留）
INDEX_CONTEXT = {"SPY", "QQQ", "IWM", "^VIX", "VIX"}


@dataclass
class ProbeConfig:
    """判定门槛全部集中在这里。

    判定口径（2026-07-14 用户定稿，二维规则，成交量为主轴）：
      - **成交量高 → OK，即便价差宽**（真值锚 MU：worst 14% 但 atm_vol 4141/597 笔，能进能出，
        用户明确"可接受"；低价票 OPEN 的 %-价差虚高也靠高量豁免，与价位混淆问题一并化解）。
      - **成交量低 + 价差宽 → 建议移除**（真值锚 NBIS：worst 9.5%、atm_vol 仅 533、开盘只成交几单，
        ATM 期权无法 0DTE 快进快出）。
      - 成交量低但价差尚可 → 观察（能算得清成本，但 fill/离场有风险）。
    阈值标定：vol_high 取 1500（稳在 NBIS 533 与 MU 4141 之间；12 只真·0DTE 好票 atm_vol 均 ≥1945）；
    spread_wide 取 8%（NBIS 9.5% 判宽）。两者是用户风险参数，可调。
    """
    exp_window_days: int = 16      # 期权链只裁近月，减小 payload + 选近月 ATM
    atm_delta: float = 0.50        # ATM 目标 |delta|
    vol_high: int = 1500           # atm_vol(call+put 最近日) ≥ 此值 = 量足，价差宽也 OK（MU 锚）
    spread_wide: float = 0.08      # 低量前提下 worst-side rel_spread ≥ 此值 = 宽(红)（NBIS 锚）
    vol_min: int = 100             # 兜底：atm_vol 缺失时用 tc 参考
    tc_min: int = 20               # tc 兜底门（atm_vol 缺失时用）
    request_sleep: float = 0.25    # 每票 chain 调用间隔，防 429


# ─────────────────────────────────────────────────────────────────────────────
# 数据加载
# ─────────────────────────────────────────────────────────────────────────────
def load_watchlist() -> list[dict]:
    """从 DB 读 user_watchlist，返回 [{ticker, gear}]，排除指数 context。"""
    from app.core.sb_client import get_supabase_client
    from app.config import USER_ID
    client = get_supabase_client()
    rows = client.table("user_watchlist").select("ticker,gear").eq("user_id", USER_ID).execute().data or []
    out = []
    for r in rows:
        t = (r.get("ticker") or "").upper()
        if not t or t in INDEX_CONTEXT:
            continue
        out.append({"ticker": t, "gear": r.get("gear")})
    return out


def fetch_spots(tickers: list[str], sc: StockHistoricalDataClient) -> dict[str, float]:
    """一次批量拉现价，用于 delta 缺失时按行权价选 ATM。"""
    try:
        res = sc.get_stock_latest_trade(StockLatestTradeRequest(symbol_or_symbols=tickers))
        return {sym: getattr(tr, "price", None) for sym, tr in res.items()}
    except Exception as e:
        print(f"[warn] 批量现价拉取失败，将全部按 delta 选 ATM: {e}")
        return {}


# ─────────────────────────────────────────────────────────────────────────────
# 单票选 ATM
# ─────────────────────────────────────────────────────────────────────────────
def _nearest_expiry(contracts: list[dict], today: "datetime.date") -> str | None:
    """取最近一个非 0DTE 到期日（> today）；若无则退回最小 >= today。"""
    exps = sorted({c["expiry"] for c in contracts})
    if not exps:
        return None
    future = [e for e in exps if datetime.strptime(e, "%Y-%m-%d").date() > today]
    if future:
        return future[0]
    return exps[0]


def _pick_atm(contracts: list[dict], side: str, spot: float | None, cfg: ProbeConfig) -> dict | None:
    """在给定 side(call/put) 里选 ATM：优先 |delta|≈atm_delta，delta 全缺时按 strike 最接近 spot。"""
    pool = [c for c in contracts if c["type"] == side]
    if not pool:
        return None
    with_delta = [c for c in pool if c.get("delta") is not None]
    if with_delta:
        return min(with_delta, key=lambda c: abs(abs(c["delta"]) - cfg.atm_delta))
    if spot:
        return min(pool, key=lambda c: abs(c["strike"] - spot))
    return None


# ─────────────────────────────────────────────────────────────────────────────
# ATM 成交量（日线补数，snapshot 拿不到）
# ─────────────────────────────────────────────────────────────────────────────
def fetch_atm_volumes(occs: list[str], provider: AlpacaProvider) -> dict[str, dict]:
    """批量拉选中 ATM 合约的日线，取最近一个交易日的 volume + trade_count。"""
    out: dict[str, dict] = {}
    if not occs:
        return out
    oc = provider.option_client
    start = datetime.now(timezone.utc) - timedelta(days=8)
    for i in range(0, len(occs), 50):
        chunk = occs[i:i + 50]
        try:
            df = oc.get_option_bars(
                OptionBarsRequest(
                    symbol_or_symbols=chunk,
                    timeframe=TimeFrame(1, TimeFrameUnit.Day),
                    start=start,
                )
            ).df
        except Exception as e:
            print(f"[warn] ATM 日线批量拉取失败 {chunk[:2]}...: {e}")
            continue
        if df is None or df.empty:
            continue
        df = df.reset_index()
        for sym, g in df.groupby("symbol"):
            last = g.sort_values("timestamp").iloc[-1]
            out[sym] = {
                "volume": int(last.get("volume", 0) or 0),
                "trade_count": int(last.get("trade_count", 0) or 0),
            }
    return out


# ─────────────────────────────────────────────────────────────────────────────
# 判定
# ─────────────────────────────────────────────────────────────────────────────
def verdict(worst_spread: float | None, atm_vol: int | None, tc: int | None, cfg: ProbeConfig) -> str:
    """二维判定（成交量为主轴）：
    高量 → OK（价差宽也可接受，MU 锚）；低量+宽价差 → 建议移除（NBIS 锚）；低量+价差尚可 → 观察。
    """
    if worst_spread is None and atm_vol is None and tc is None:
        return "无数据"
    # 成交量是否够高（价差宽也能进出）。atm_vol 缺失时用 tc 兜底。
    if atm_vol is not None:
        vol_ok = atm_vol >= cfg.vol_high
    elif tc is not None:
        vol_ok = tc >= cfg.tc_min
    else:
        vol_ok = False
    if vol_ok:
        return "OK"
    # 以下均为低成交量
    wide = worst_spread is not None and worst_spread >= cfg.spread_wide
    return "建议移除" if wide else "观察"


# ─────────────────────────────────────────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────────────────────────────────────────
def run(tickers: list[dict], cfg: ProbeConfig | None = None) -> pd.DataFrame:
    cfg = cfg or ProbeConfig()
    provider = AlpacaProvider()
    sc = StockHistoricalDataClient(ALPACA_PAPER_API_KEY, ALPACA_PAPER_SECRET_KEY)

    today = datetime.now(NY).date()
    exp_gte = today
    exp_lte = today + timedelta(days=cfg.exp_window_days)

    syms = [t["ticker"] for t in tickers]
    spots = fetch_spots(syms, sc)

    pending: list[dict] = []   # 每票选中的 ATM，待补成交量
    occ_needed: list[str] = []

    for i, t in enumerate(tickers, 1):
        sym = t["ticker"]
        print(f"[{i}/{len(tickers)}] {sym} ...", flush=True)
        contracts = provider.get_option_chain_snapshot(sym, exp_gte=exp_gte, exp_lte=exp_lte)
        time.sleep(cfg.request_sleep)
        if not contracts:
            pending.append({**t, "expiry": None, "call": None, "put": None, "no_options": True})
            continue
        exp = _nearest_expiry(contracts, today)
        near = [c for c in contracts if c["expiry"] == exp]
        spot = spots.get(sym)
        call = _pick_atm(near, "call", spot, cfg)
        put = _pick_atm(near, "put", spot, cfg)
        for c in (call, put):
            if c:
                occ_needed.append(c["occ"])
        pending.append({**t, "expiry": exp, "spot": spot, "call": call, "put": put, "no_options": False})

    vols = fetch_atm_volumes(occ_needed, provider)

    rows = []
    for p in pending:
        sym, gear = p["ticker"], p["gear"]
        is_ref = bool(gear)  # gear 非空 = 已定档肉票 = 参照基准
        if p.get("no_options"):
            rows.append({
                "ticker": sym, "参照": "★" if is_ref else "", "expiry": None,
                "spread_call%": None, "spread_put%": None, "worst%": None,
                "depth": None, "atm_vol": None, "tc": None, "判定": "无期权",
            })
            continue
        call, put = p["call"], p["put"]
        sp_call = call["rel_spread"] if call else None
        sp_put = put["rel_spread"] if put else None
        worst = max([s for s in (sp_call, sp_put) if s is not None], default=None)

        depths = []
        for c in (call, put):
            if c and c.get("bid_size") is not None and c.get("ask_size") is not None:
                depths.append(min(c["bid_size"], c["ask_size"]))
        depth = min(depths) if depths else None

        cv = vols.get(call["occ"], {}) if call else {}
        pv = vols.get(put["occ"], {}) if put else {}
        atm_vol = (cv.get("volume", 0) + pv.get("volume", 0)) if (cv or pv) else None
        tc = max([x for x in (cv.get("trade_count"), pv.get("trade_count")) if x is not None], default=None)

        rows.append({
            "ticker": sym,
            "参照": "★" if is_ref else "",
            "expiry": p["expiry"],
            "spread_call%": round(sp_call * 100, 1) if sp_call is not None else None,
            "spread_put%": round(sp_put * 100, 1) if sp_put is not None else None,
            "worst%": round(worst * 100, 1) if worst is not None else None,
            "depth": int(depth) if depth is not None else None,
            "atm_vol": atm_vol,
            "tc": tc,
            "判定": verdict(worst, atm_vol, tc, cfg),
        })

    df = pd.DataFrame(rows)
    # 排序：建议移除 > 观察 > OK，其内按 worst% 降序
    order = {"建议移除": 0, "观察": 1, "OK": 2, "无数据": 3, "无期权": 4}
    df["_o"] = df["判定"].map(lambda v: order.get(v, 5))
    df = df.sort_values(["_o", "worst%"], ascending=[True, False], na_position="last").drop(columns="_o")
    return df


def main():
    ap = argparse.ArgumentParser(description="watchlist 期权流动性体检")
    ap.add_argument("--tickers", nargs="+", default=None, help="只查这些 ticker（默认读全 watchlist）")
    ap.add_argument("--exclude-gear", action="store_true", help="只查 gear=null 的非肉票")
    ap.add_argument("--exp-window", type=int, default=None, help="近月到期窗口天数（默认 16）")
    ap.add_argument("--out-dir", default="data/pool_reports", help="报告输出目录")
    args = ap.parse_args()

    cfg = ProbeConfig()
    if args.exp_window:
        cfg.exp_window_days = args.exp_window

    if args.tickers:
        wl = [{"ticker": t.upper(), "gear": None} for t in args.tickers if t.upper() not in INDEX_CONTEXT]
    else:
        wl = load_watchlist()
        if args.exclude_gear:
            wl = [t for t in wl if not t["gear"]]

    print(f"体检 {len(wl)} 个 ticker（价差取最巿一边；宽≥{cfg.spread_wide:.0%} / 薄 vol<{cfg.vol_min} 或 tc<{cfg.tc_min}）\n")
    df = run(wl, cfg)

    pd.set_option("display.max_rows", None)
    pd.set_option("display.width", 200)
    print("\n" + "=" * 90)
    print(df.to_string(index=False))
    print("=" * 90)

    n_drop = int((df["判定"] == "建议移除").sum())
    n_watch = int((df["判定"] == "观察").sum())
    print(f"\n建议移除 {n_drop} 只 / 观察 {n_watch} 只 / OK {int((df['判定']=='OK').sum())} 只")
    if n_drop:
        print("建议移除:", ", ".join(df.loc[df["判定"] == "建议移除", "ticker"].tolist()))

    os.makedirs(args.out_dir, exist_ok=True)
    out_path = os.path.join(args.out_dir, f"opt_liquidity_{datetime.now(NY):%Y-%m-%d}.csv")
    df.to_csv(out_path, index=False, encoding="utf-8-sig")
    print(f"\n报告已写入: {out_path}")


if __name__ == "__main__":
    main()
