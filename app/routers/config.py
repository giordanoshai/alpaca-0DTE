import asyncio
import json
import logging
import pathlib
from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict

import app.templates as templates_service
from app.services.data_source_registry import current_source
from app.integrations.alpaca.data import AlpacaData
from app.option_stream import (
    OPTION_STREAM,
    OptionSubscriptionBudgetError,
    atm_chain_symbols,
)
from app.templates import (
    TemplateLockedError,
    TemplatePersistenceError,
    delete_template,
    get_store,
    load_template,
    save_template,
)

from app.core.dailylogger import get_logger

logger = get_logger("tt.gate")
router = APIRouter(prefix="/api", tags=["config"])
_REPLAY_DIR = pathlib.Path(__file__).resolve().parent.parent.parent / "data" / "replay"


@router.get("/templates")
def list_templates() -> dict:
    """列出数据库逐行配置模板 + 当前加载/待加载。"""
    s = get_store()
    templates = []
    for row in s.records():
        templates.append({
            "id": row["id"], "name": row["name"], "reason": row["reason"],
            "status": row["status"], "created_at": row["created_at"], "updated_at": row["updated_at"],
            **{k: v for k, v in row["params"].items() if k != "name"},
        })
    return {
        "active": s.active_name(),
        "active_id": s.active_id(),
        "pending": s.pending_name(),
        "pending_id": s.pending_id(),
        "market_locked": templates_service.is_market_hours(),
        "templates": templates,
    }


class TemplateParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    changes: dict[str, str]
    reason: str


@router.post("/templates/params")
def update_template_params(body: TemplateParams) -> dict:
    """设置页保存：新增一行独立配置模板，不自动加载。"""
    if not body.name.strip():
        raise HTTPException(status_code=422, detail="配置名称必填")
    if not body.reason.strip():
        raise HTTPException(status_code=422, detail="修改理由必填")
    if not body.changes:
        raise HTTPException(status_code=422, detail="无参数改动")
    try:
        result = save_template(body.name, body.changes, reason=body.reason.strip())
    except KeyError as e:
        raise HTTPException(status_code=422, detail=f"不可修改的参数键: {e.args[0]!r}")
    except TemplatePersistenceError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:  # noqa: BLE001 — Decimal/int 转换失败等
        raise HTTPException(status_code=422, detail=f"参数值非法: {e}")
    logger.info(json.dumps({"event": "template_row_saved", "name": body.name.strip(),
                            "reason": body.reason.strip(), "changes": body.changes,
                            **{k: v for k, v in result.items() if k != "template"}},
                           ensure_ascii=False))
    return result


@router.post("/templates/{template_id}/load")
def load_template_ep(template_id: int) -> dict:
    """加载数据库中的一行配置并立即生效。"""
    try:
        result = load_template(template_id)
    except TemplateLockedError as e:
        raise HTTPException(status_code=423, detail=str(e))
    except KeyError:
        raise HTTPException(status_code=404, detail=f"配置不存在: {template_id}")
    except TemplatePersistenceError as e:
        raise HTTPException(status_code=503, detail=str(e))
    logger.info(json.dumps({"event": "template_row_loaded", "id": template_id,
                            **{k: v for k, v in result.items() if k != "template"}}, ensure_ascii=False))
    return result


@router.delete("/templates/{template_id}")
def delete_template_ep(template_id: int) -> dict:
    """删除一个非当前、非待加载配置。"""
    try:
        result = delete_template(template_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"配置不存在: {template_id}")
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except TemplatePersistenceError as e:
        raise HTTPException(status_code=503, detail=str(e))
    logger.info(json.dumps({"event": "template_row_deleted", **result}, ensure_ascii=False))
    return result


class RecordQuotes(BaseModel):
    model_config = ConfigDict(extra="forbid")
    on: bool


@router.get("/record-quotes")
async def record_quotes_status() -> dict:
    return {"recording": OPTION_STREAM.is_recording, "count": OPTION_STREAM.record_count}


@router.post("/record-quotes")
async def record_quotes_toggle(body: RecordQuotes) -> dict:
    """录制 QQQ/IWM/SPY 今日 0DTE 期权 bid/ask，供次日回放调试。

    2026-07-17 事故复盘：最初做法是独立脚本自己开一条 Alpaca 期权 WS 连接，跟
    watchdog 盯盘用的那条（app/option_stream.py，实时止损/触墙止盈）抢账号唯一
    的并发连接名额，把盯盘打挂了。现在改为复用 OPTION_STREAM 已经开着的那条连接
    （见 option_stream.py::start_recording），不会再抢。
    """
    if not body.on:
        OPTION_STREAM.stop_recording()
        logger.info(json.dumps({"event": "record_quotes_stop", "count": OPTION_STREAM.record_count}, ensure_ascii=False))
        return {"recording": False, "count": OPTION_STREAM.record_count}

    if OPTION_STREAM.is_recording:
        return {"recording": True, "count": OPTION_STREAM.record_count, "note": "already recording"}

    source = current_source()
    alpaca = getattr(source, "alpaca", None) or AlpacaData()

    def run() -> set[str]:
        return atm_chain_symbols(alpaca)

    symbols = await asyncio.to_thread(run)
    if not symbols:
        raise HTTPException(status_code=400, detail="取不到今日期权链候选（是否未开盘/无分钟线数据）")

    _REPLAY_DIR.mkdir(parents=True, exist_ok=True)
    path = _REPLAY_DIR / f"option_quotes_{datetime.now().strftime('%Y%m%d')}.jsonl"
    try:
        OPTION_STREAM.start_recording(symbols, path)
    except OptionSubscriptionBudgetError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    logger.info(json.dumps({"event": "record_quotes_start", "symbols": len(symbols), "path": str(path)}, ensure_ascii=False))
    return {"recording": True, "symbols": len(symbols), "path": str(path)}
