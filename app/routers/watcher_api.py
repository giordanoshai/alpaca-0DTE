"""FastAPI 路由定义"""
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd
import pytz
from fastapi import APIRouter, Query, Request
from fastapi.responses import FileResponse, JSONResponse

from app.config import USER_ID, MARKET_STATUS_TICKERS
from app.routers.watcher_api_models import WatcherStatus
from app.core.data_manager import MIN_WARMUP_BARS
from app.core.indicators import add_all_indicators
from app.db.signal_store import save_signal, get_signals, delete_signal, clear_signals
from app.services.trading_config import get_config as get_trading_config

router = APIRouter(prefix="/api", tags=["api"])


from app.watcher.watcher_manager import GLOBAL_WATCHER

# 手动加票的默认档位：与现役 NVDA/QQQ/TSLA 一致（用户 2026-09-05 拍板）。
# 取值含义见 app/config.py STRATEGY_H_GEARS。
DEFAULT_MANUAL_GEAR = "instant_0.15"

# 手动输入框的代码形状闸：字母开头，允许 . 与 -（BRK.B / RDS-A 这类），最长 6 位。
# 只挡明显的手滑输入，真伪由后面那道 K 线预热判定。
_MANUAL_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,5}$")


def get_state(request: Request):
    return {
        'manager': None,
        'watcher_manager': GLOBAL_WATCHER,
    }


@router.get("/watchlist")
async def get_watchlist():
    """获取 watchlist"""
    try:
        from app.core.sb_client import get_supabase_client
        client = get_supabase_client()
        # 前端卡片只显示订阅中的(gear 非空);gear=NULL 为候选仓库
        response = client.table('user_watchlist').select('ticker').eq('user_id', USER_ID).not_.is_('gear', 'null').execute()
        tickers = [item['ticker'].lstrip('$') for item in response.data]
        return {"tickers": tickers}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/watchlist")
async def add_ticker(ticker: str = Query(...), request: Request = None):
    """手动加一只票进盯盘名单（突发消息临时加票的入口）。

    顺序是「先补 K 线、再写库」，反过来会同时踩三个坑（2026-09-05 修）：

    1. **预热失败就不写库**：`WatcherManager.add_ticker()` 内部会下载最近两天的分钟线、
       补断层、落本地缓存，拿不到数据时返回 False。原先接口不看这个返回值，冷门票
       照样报成功，前端摆一张永远算不出 VWAP/ATR 的空白卡片。宁可加不进去。
    2. **写库必须 upsert**：表上有 `UNIQUE (ticker)`，而库里躺着一百多只 gear 为空的
       候选票，用 insert 加它们必然撞约束报 500 —— 这正是「手动添加没反应」的主因。
    3. **必须补 gear**：`GET /api/watchlist` 与 `/api/tickers` 都只认 gear 非空的记录，
       gear 留空的话页面一刷新这票就消失了。默认给 instant_0.15，与现役三只一致。

    下单白名单取的是盯盘内存池（见 order_gate 的动态白名单），所以票一旦进池就同时
    具备下单资格，这里不需要另外开口子。
    """
    ticker = (ticker or "").strip().upper().lstrip('$')

    if ticker.startswith('^'):
        return JSONResponse(
            {"error": f"{ticker} 是纯展示指数（不是可交易期权标的），不能加进盯盘名单"},
            status_code=400)
    if not _MANUAL_TICKER_RE.match(ticker):
        return JSONResponse({"error": f"代码 {ticker or '(空)'} 不是合法的股票代码"},
                            status_code=400)

    manager = get_state(request)['watcher_manager']
    if manager is None:
        return JSONResponse({"error": "盯盘引擎未启动，稍后再试"}, status_code=503)

    # ① 先起盯盘器 —— 这一步就是 K 线补全，会同步等它跑完（单只票通常几秒）
    if not await manager.add_ticker(ticker):
        return JSONResponse(
            {"error": f"{ticker} 的 K 线数据补不齐（拿不到分钟线，或不足 {MIN_WARMUP_BARS} 根），没有加入"},
            status_code=400)

    bars = manager.bar_count(ticker)

    # ② K 线到位了才写名单
    try:
        from app.core.sb_client import get_supabase_client
        client = get_supabase_client()
        client.table('user_watchlist').upsert(
            {'user_id': USER_ID, 'ticker': ticker, 'gear': DEFAULT_MANUAL_GEAR},
            on_conflict='ticker',
        ).execute()
    except Exception as e:
        # ③ 写库失败要把票从内存池摘回去，否则会留下一只「页面上没有、后台却在盯」的
        #    幽灵票 —— 它还会占 UW price 频道预算，且因为在池里而自动获得下单资格。
        await manager.remove_ticker(ticker)
        return JSONResponse({"error": f"{ticker} K线已就绪但写入名单失败：{e}"},
                            status_code=500)

    return {"success": True, "ticker": ticker, "gear": DEFAULT_MANUAL_GEAR, "bars": bars}


@router.delete("/watchlist/{ticker}")
async def remove_ticker(ticker: str, request: Request = None):
    """从盯盘名单移除：只把 gear 清空，让记录退回候选仓库，不删行。

    原先是 delete 整行。库里那一百多只 gear 为空的候选票，只要被加进来再移除一次，
    候选记录就永久没了 —— 而移除的本意只是「不盯了」，不是「从此不认识这只票」。
    """
    ticker = ticker.upper()

    try:
        from app.core.sb_client import get_supabase_client
        client = get_supabase_client()
        client.table('user_watchlist').update({'gear': None}) \
            .eq('user_id', USER_ID).eq('ticker', ticker).execute()

        state = get_state(request)
        if state['watcher_manager'] and ticker not in MARKET_STATUS_TICKERS:
            await state['watcher_manager'].remove_ticker(ticker)

        return {"success": True, "ticker": ticker}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/state")
async def get_all_state(request: Request = None):
    """获取所有 ticker 状态"""
    state = get_state(request)
    if state['watcher_manager']:
        return state['watcher_manager'].get_all_states()
    return {}


@router.get("/state/{ticker}")
async def get_ticker_state(ticker: str, request: Request = None):
    """获取单个 ticker 状态"""
    state = get_state(request)
    if state['watcher_manager']:
        ts = state['watcher_manager'].get_ticker_state(ticker.upper())
        if ts:
            return ts
    return JSONResponse({"error": "Ticker not found"}, status_code=404)


@router.get("/health")
async def get_health(request: Request = None):
    """盯盘健康状态（供 tt_trading 掉线拦单 + 横幅判断）

    返回:
        running: 盯盘主循环是否在运行
        market_open: 当前是否处于可交易时段（盘前 04:00-09:30 或常规 09:30-16:00 ET）
        last_update_ts: 最近一次任意标的 bar 更新的 UNIX epoch 秒（UTC-based）
        ticker_count: 当前监控的标的数量
    """
    state = get_state(request)
    wm = state['watcher_manager']

    if not wm:
        return {"running": False, "market_open": False, "last_update_ts": None, "ticker_count": 0}

    # market_open: 复用任一 watcher 的 DataManager 判断交易时段
    market_open = False
    last_update_ts = None
    for watcher in wm._watchers.values():
        if watcher.data_manager:
            session = watcher.data_manager._get_trading_session()
            market_open = session in ('pre_market', 'regular')
            break

    # last_update_ts: 所有 watcher 中最新的 bar 时间
    latest = None
    for watcher in wm._watchers.values():
        if watcher.data_manager and watcher.data_manager.last_bar_time is not None:
            if latest is None or watcher.data_manager.last_bar_time > latest:
                latest = watcher.data_manager.last_bar_time
    if latest is not None:
        last_update_ts = int(latest.timestamp())

    return {
        "running": wm._running,
        "market_open": market_open,
        "last_update_ts": last_update_ts,
        "ticker_count": len(wm._watchers),
    }


@router.get("/watcher/status")
async def get_watcher_status(request: Request = None):
    """获取盯盘状态"""
    state = get_state(request)
    if state['watcher_manager']:
        return {"running": state['watcher_manager']._running}
    return {"running": False}


@router.post("/watcher/status")
async def set_watcher_status(status: WatcherStatus, request: Request = None):
    """设置盯盘状态"""
    state = get_state(request)
    if state['watcher_manager']:
        if status.running and not state['watcher_manager']._running:
            await state['watcher_manager'].start()
        elif not status.running and state['watcher_manager']._running:
            await state['watcher_manager'].stop()
        return {"running": state['watcher_manager']._running}
    return JSONResponse({"error": "Watcher not initialized"}, status_code=500)


@router.get("/history/{ticker}")
async def get_ticker_history(
    ticker: str, interval: str = Query(default="5m"),
    limit: Optional[int] = Query(default=None), request: Request = None,
):
    """获取 ticker 历史 K线数据（用于图表）。limit 可覆盖默认回看根数
    （小卡片迷你图不需要 1500 根 1m，调小 limit 省掉大头的抓取/解析/渲染成本；
    详情弹窗大图不传 limit，走原有 1500 根）。"""
    ticker = ticker.upper()

    try:
        state = get_state(request)
        if not state['watcher_manager']:
            return JSONResponse({"error": "Watcher not initialized"}, status_code=500)

        watcher = state['watcher_manager']._watchers.get(ticker)
        if not watcher:
            return JSONResponse({"error": f"Ticker {ticker} not in watchlist"}, status_code=404)

        if not watcher.data_manager:
            return JSONResponse({"error": "Data manager not initialized"}, status_code=500)

        dm = watcher.data_manager

        if interval == '5m':
            df = dm.get_df_5m(tail=(limit or 300))
        else:
            df = dm.get_df_1m(tail=(limit or 1500))

        if df.empty:
            return JSONResponse({"error": f"No data available for {ticker}"}, status_code=404)

        df = add_all_indicators(df)

        bars = []
        for idx, row in df.iterrows():
            ts = int(idx.timestamp())
            bar_data = {
                'time': ts,
                'open': float(row['open']),
                'high': float(row['high']),
                'low': float(row['low']),
                'close': float(row['close']),
                'volume': float(row['volume']),
            }

            rsi_col = 'RSI_9'
            rsi_ma_col = f'RSI_9_MA_14'
            for col in ['MACD_12_26_9', 'MACDh_12_26_9', 'MACDs_12_26_9', rsi_col, rsi_ma_col]:
                if col in df.columns:
                    val = row.get(col)
                    if pd.notna(val):
                        if col == 'MACD_12_26_9':
                            bar_data['macd'] = float(val)
                        elif col == 'MACDh_12_26_9':
                            bar_data['macd_hist'] = float(val)
                        elif col == 'MACDs_12_26_9':
                            bar_data['macd_signal'] = float(val)
                        elif col == rsi_col:
                            bar_data['rsi'] = float(val)
                        elif col == rsi_ma_col:
                            bar_data['rsi_ma'] = float(val)

            bars.append(bar_data)

        return {
            'ticker': ticker,
            'interval': interval,
            'bars': bars,
        }

    except Exception as e:
        import traceback
        traceback.print_exc()
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/signals")
async def get_all_signals(limit: int = Query(default=200)):
    return get_signals(limit=limit)


@router.delete("/signals/{signal_id}")
async def delete_one_signal(signal_id: str):
    delete_signal(signal_id)
    return {"success": True}


@router.delete("/signals")
async def clear_all_signals():
    count = clear_signals()
    return {"deleted": count}


@router.get("/trading_config")
async def get_trading_config_endpoint():
    """获取当前交易配置"""
    return get_trading_config()


@router.get("/alerts/switches")
async def get_alert_switches():
    """获取三个策略提醒开关状态"""
    from app.services.trading_config import get_alert_switches
    return get_alert_switches()


@router.post("/alerts/switches/{strategy}")
async def set_alert_switch(strategy: str, enabled: bool = Query(...)):
    """切换策略提醒开关（strategy: v7 | a | b | c）"""
    from app.services.trading_config import set_alert_enabled
    if strategy.lower() not in ('v7', 'a', 'b', 'c2', 'c3', 'd', 'e', 'f', 'g', 'h'):
        return JSONResponse({"error": "strategy must be v7, a, b, c2, c3, d, e, f, g, or h"}, status_code=400)
    set_alert_enabled(strategy, enabled)
    return {"strategy": strategy.lower(), "enabled": enabled}


# ── 策略参数编辑（前端弹窗）────────────────────────────────────────────────────

@router.get("/strategy_params")
async def get_strategy_params_endpoint():
    """获取所有策略当前参数 + 默认值（供弹窗渲染）"""
    from app.services.trading_config import get_strategy_params
    return get_strategy_params()


@router.post("/strategy_params/{key}")
async def update_strategy_params_endpoint(key: str, request: Request):
    """更新某策略参数并热重载运行中引擎。body = {param: value, ...}"""
    from app.services.trading_config import update_strategy_params
    try:
        patch = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid JSON body"}, status_code=400)
    result = update_strategy_params(key.upper(), patch)
    if not result.get("ok"):
        return JSONResponse(result, status_code=400)
    state = get_state(request)
    if state.get('watcher_manager'):
        result["reloaded"] = state['watcher_manager'].reload_strategy_engines()
    return result


@router.post("/strategy_params/{key}/reset")
async def reset_strategy_params_endpoint(key: str, request: Request):
    """把某策略参数恢复默认并热重载运行中引擎。"""
    from app.services.trading_config import reset_strategy_params
    result = reset_strategy_params(key.upper())
    if not result.get("ok"):
        return JSONResponse(result, status_code=400)
    state = get_state(request)
    if state.get('watcher_manager'):
        result["reloaded"] = state['watcher_manager'].reload_strategy_engines()
    return result


# ── H 策略 gear 档位 / 全局 spread 系数（TODO.md P2.5，2026-07-13）─────────────

@router.get("/h-gears")
async def get_h_gears():
    """H 策略档位定义 + 参考实测速率（供前端档位选择器/滑杆预估展示）。"""
    from app.config import STRATEGY_H_GEARS, STRATEGY_H_GEAR_STATS
    return {"gears": STRATEGY_H_GEARS, "stats": STRATEGY_H_GEAR_STATS}


@router.post("/set-gear")
async def set_gear(ticker: str = Query(...), gear: str = Query(...), request: Request = None):
    """更新单个 ticker 的 H 策略档位：写 user_watchlist.gear + 热重建该票引擎（状态机重置）。"""
    from app.config import STRATEGY_H_GEARS
    ticker = ticker.upper()
    if gear not in STRATEGY_H_GEARS:
        return JSONResponse(
            {"error": f"gear must be one of {list(STRATEGY_H_GEARS)}"}, status_code=400
        )
    try:
        from app.core.sb_client import get_supabase_client
        from app.services.trading_config import refresh_gear_map
        client = get_supabase_client()
        client.table('user_watchlist').update({'gear': gear}) \
            .eq('user_id', USER_ID).eq('ticker', ticker).execute()
        refresh_gear_map()

        state = get_state(request)
        reloaded = bool(state.get('watcher_manager')) and \
            state['watcher_manager'].reload_ticker_engine(ticker)
        return {"ticker": ticker, "gear": gear, "reloaded": reloaded}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/global-spread-factor")
async def get_global_spread_factor_endpoint():
    """获取当前全局 spread 系数（内存态，重启复位为 1.0）。"""
    from app.services.trading_config import get_global_spread_factor
    return {"factor": get_global_spread_factor()}


@router.post("/global-spread-factor")
async def set_global_spread_factor_endpoint(factor: float = Query(...), request: Request = None):
    """设置全局 spread 系数（内存态，不写库），热更新所有运行中 ticker 的 H 引擎阈值（不重置状态机）。"""
    from app.services.trading_config import set_global_spread_factor
    applied = set_global_spread_factor(factor)

    state = get_state(request)
    updated = state['watcher_manager'].apply_global_spread_factor() if state.get('watcher_manager') else 0
    return {"factor": applied, "updated_tickers": updated}


