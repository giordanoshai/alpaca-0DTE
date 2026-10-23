import asyncio
import json
import logging
from datetime import datetime
from typing import Optional, Any
from supabase import create_client, Client
from app.config import SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY

from app.core.sb_client import get_supabase_client

from app.core.dailylogger import get_logger

logger = get_logger("tt.signal_store")

signal_queue = asyncio.Queue()


def put_signal_nowait(
    ticker: str,
    signal_type: str,
    timeframe: str,
    price: float,
    score: Optional[float] = None,
    indicators: Optional[dict[str, Any]] = None,
    regime: Optional[str] = None,
    validator_result: Optional[dict[str, Any]] = None,
):
    """
    非阻塞地将信号放入队列中供后台异步落库使用。
    """
    signal_data = {
        "ticker": ticker,
        "signal_type": signal_type,
        "timeframe": timeframe,
        "score": score,
        "price": price,
        "indicators": indicators,
        "regime": regime,
        "validator_result": validator_result,
        "created_at": datetime.utcnow().isoformat() + "Z"
    }
    try:
        signal_queue.put_nowait(signal_data)
    except Exception as e:
        logger.error(f"Failed to put signal into queue: {e}")


async def db_worker_loop():
    """后台工作协程：不断从 signal_queue 取出信号并落库到 Supabase"""
    logger.info("db_worker_loop started for Supabase async logging.")
    client = get_supabase_client()
    
    while True:
        try:
            # 批量获取（如果有），或者阻塞等待至少一个
            signal_data = await signal_queue.get()
            batch = [signal_data]
            
            # 尝试取出当前队列中所有累积的元素，但不阻塞
            while not signal_queue.empty():
                try:
                    batch.append(signal_queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            
            # 尝试发送到 Supabase
            try:
                # 阻塞的网络调用，使用 to_thread 避免卡主循环
                # supabase-py python client 目前不支持纯 async
                def _insert():
                    return client.table("signals").insert(batch).execute()
                
                await asyncio.to_thread(_insert)
                
                for _ in range(len(batch)):
                    signal_queue.task_done()
                    
            except Exception as e:
                logger.error(f"Supabase DB insert failed: {e}. Retrying in 5 seconds.")
                # 若失败，重新把数据插回队列（或者延迟后再放回去）
                await asyncio.sleep(5)
                for item in batch:
                    await signal_queue.put(item)
                for _ in range(len(batch)):
                    signal_queue.task_done() # Mark as done for the failed gets to maintain queue integrity

        except asyncio.CancelledError:
            logger.info("db_worker_loop cancelled.")
            break
        except Exception as e:
            logger.error(f"Unexpected error in db_worker_loop: {e}")
            await asyncio.sleep(1.0)


def save_signal(
    ticker: str,
    triggered_at: datetime,
    signal_type: str,
    source: str = 'V7',
    fast: Optional[float] = None,
    slow: Optional[float] = None,
    regime_text: Optional[str] = None,
    regime_score: Optional[int] = None,
    regime_label: Optional[str] = None,
) -> int:
    indicators = {'source': source}
    if fast is not None:
        indicators['fast'] = round(fast, 4)
    if slow is not None:
        indicators['slow'] = round(slow, 4)

    validator_result = {}
    if regime_text:
        validator_result['regime_text'] = regime_text

    put_signal_nowait(
        ticker=ticker,
        signal_type=signal_type,
        timeframe='1m',
        price=0.0,
        score=float(regime_score) if regime_score is not None else None,
        indicators=indicators,
        regime=regime_label,
        validator_result=validator_result,
    )
    return 0


def get_signals(limit: int = 200) -> list[dict]:
    client = get_supabase_client()
    try:
        res = client.table('signals').select('*').order('created_at', desc=True).limit(limit).execute()
        out = []
        for row in res.data:
            indicators = row.get('indicators') or {}
            val_res = row.get('validator_result') or {}
            out.append({
                'id': row.get('id'),
                'ticker': row.get('ticker'),
                'triggered_at': row.get('created_at'),
                'signal_type': row.get('signal_type'),
                'source': indicators.get('source', 'V7'),
                'fast': indicators.get('fast'),
                'slow': indicators.get('slow'),
                'regime_text': val_res.get('regime_text'),
                'regime_score': row.get('score'),
                'regime_label': row.get('regime'),
            })
        return out
    except Exception as e:
        logger.error(f"Error getting signals from Supabase: {e}")
        return []


def delete_signal(signal_id: str):
    client = get_supabase_client()
    try:
        client.table('signals').delete().eq('id', str(signal_id)).execute()
    except Exception as e:
        logger.error(f"Error deleting signal {signal_id}: {e}")


def clear_signals() -> int:
    client = get_supabase_client()
    try:
        res = client.table('signals').delete().neq('id', '00000000-0000-0000-0000-000000000000').execute()
        return len(res.data)
    except Exception as e:
        logger.error(f"Error clearing signals: {e}")
        return 0


def save_position_alert(*args, **kwargs) -> int:
    return 0
