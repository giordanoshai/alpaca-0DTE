import asyncio
import logging
from fastapi import APIRouter

from app.trading_state import risk_snapshot as build_risk_snapshot

from app.core.dailylogger import get_logger

logger = get_logger("tt.gate")
router = APIRouter(prefix="/api", tags=["market"])


@router.get("/tickers")
async def list_tickers() -> dict:
    """从 user_watchlist 读取 gear 不为空的 ticker 列表；排除 `^` 前缀指数（如 ^VIX，
    4in1 用来判断行情趋势的展示标的，不是可交易期权标的）。"""
    from app.db import connect
    def run() -> list[dict]:
        with connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT ticker, gear FROM user_watchlist WHERE gear IS NOT NULL AND ticker NOT LIKE '^%'")
                rows = cur.fetchall()
                return [{"sym": r[0].upper(), "gear": r[1]} for r in rows]
    try:
        data = await asyncio.to_thread(run)
        return {"tickers": data}
    except Exception as e:
        logger.warning(f"获取 ticker 列表失败: {e}")
        return {"tickers": []}


@router.get("/risk")
async def risk_snapshot() -> dict:
    """⑨ 风控快照：真实已实现盈亏/连亏状态、交易次数与当前生效熔断线。"""
    return build_risk_snapshot()
