"""WebSocket 连接管理"""
import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.core.dailylogger import get_logger
from app.services.signal_handler import handle_signal_event
from app.watcher.watcher_manager import GLOBAL_WATCHER


logger = get_logger("tt.watcher")

_ET = ZoneInfo("America/New_York")


async def _send_tide_snapshot(websocket: WebSocket) -> None:
    """连线首帧补一份 Market Tide 快照。

    徽章平时靠 `market_tide_refresh_loop` 推送，而非盘中它几乎不推（值不变）。
    浏览器若在盘前刷新，不补这一帧就只能看到 `--`，累加器里回填好的昨收白拿了。
    """
    try:
        from app.services.market_tide_service import (
            current_accumulator, tide_snapshot,
        )
        acc = current_accumulator()
        if acc is None:
            return
        await websocket.send_json({
            "type": "market_tide",
            "data": tide_snapshot(acc, datetime.now(_ET), with_series=True),
        })
    except Exception as exc:  # noqa: BLE001 —— 首帧失败不能连累连接本身
        logger.warning(json.dumps({
            "event": "watcher_ws_tide_snapshot_error",
            "error": str(exc),
        }, ensure_ascii=False))


async def _send_news_snapshot(websocket: WebSocket) -> None:
    """连线首帧补一份快讯队列。

    最高档消息一天只有十几条，全靠 `news_feed_refresh_loop` 推的话，浏览器中途刷新
    后横条会一直空到下一条重要消息 —— 可能是几小时以后。队列为空时不推：空数组会
    把前端正在滚的内容清掉。
    """
    try:
        from app.services.news_feed_service import GLOBAL_NEWS_FEED
        # **补的是「此刻该显示的」，不是整个队列。** 队列里可能还留着上午的条目
        # （它们担着去重的职责，不能就地裁掉），整个推给刚连上的人就成了「刷新一下、
        # 几小时前的消息又回来了」——用户 2026-09-10 报的正是这个。
        items = GLOBAL_NEWS_FEED.visible(datetime.now(_ET))
        if not items:
            return
        await websocket.send_json({"type": "news", "data": items})
    except Exception as exc:  # noqa: BLE001 —— 同上，首帧失败不能连累连接
        logger.warning(json.dumps({
            "event": "watcher_ws_news_snapshot_error",
            "error": str(exc),
        }, ensure_ascii=False))


class ConnectionManager:
    """WebSocket 连接管理器"""
    def __init__(self):
        self._connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        """接受连接"""
        await websocket.accept()
        self._connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        """断开连接"""
        if websocket in self._connections:
            self._connections.remove(websocket)

    async def broadcast(self, message: dict):
        """广播消息给所有连接"""
        failed = []
        for connection in tuple(self._connections):
            try:
                await connection.send_json(message)
            except Exception as exc:  # noqa: BLE001 — 单个浏览器失败不能中断行情循环
                failed.append(connection)
                logger.warning(json.dumps({
                    "event": "watcher_ws_broadcast_error",
                    "error": str(exc),
                }, ensure_ascii=False))
        for connection in failed:
            self.disconnect(connection)

router = APIRouter()
manager = ConnectionManager()
# Export manager so other modules can use it
GLOBAL_WS_MANAGER = manager


def _state_payload(state: dict) -> dict:
    """构造传输副本，避免为去掉 datetime 修改 Watcher 内部状态。"""
    payload = dict(state)
    current_bar = payload.get("current_bar")
    if current_bar:
        payload["current_bar"] = {
            key: value for key, value in current_bar.items() if key != "datetime"
        }
    session_date = payload.get("session_date")
    if isinstance(session_date, date):
        payload["session_date"] = session_date.isoformat()
    return payload


async def _broadcast_update(ticker: str, state: dict) -> None:
    await manager.broadcast({
        "type": "update",
        "ticker": ticker,
        "data": _state_payload(state),
    })


async def _broadcast_signal_event(ticker: str, event) -> None:
    GLOBAL_WATCHER.record_signal(ticker)
    await handle_signal_event(manager, ticker, event)


GLOBAL_WATCHER.set_callbacks(
    on_update=_broadcast_update,
    on_signal_event=_broadcast_signal_event,
)

@router.websocket("/watcher/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)

    await _send_tide_snapshot(websocket)
    await _send_news_snapshot(websocket)

    if GLOBAL_WATCHER:
        try:
            states = GLOBAL_WATCHER.get_all_states()
        except Exception:
            states = {}
        for ticker, state in states.items():
            try:
                await websocket.send_json({
                    'type': 'update',
                    'ticker': ticker,
                    'data': _state_payload(state),
                })
            except Exception as exc:  # noqa: BLE001 — 保留连接并记录首帧失败来源
                logger.warning(json.dumps({
                    "event": "watcher_ws_initial_state_error",
                    "ticker": ticker,
                    "error": str(exc),
                }, ensure_ascii=False))
                break

    try:
        while True:
            data = await websocket.receive_text()
            try:
                msg = json.loads(data)
                if msg.get('type') == 'ping':
                    await websocket.send_json({'type': 'pong'})
                elif msg.get('type') == 'subscribe':
                    ticker = msg.get('ticker', '').upper()
                    if ticker and GLOBAL_WATCHER:
                        await GLOBAL_WATCHER.add_ticker(ticker)
                elif msg.get('type') == 'unsubscribe':
                    ticker = msg.get('ticker', '').upper()
                    if ticker and GLOBAL_WATCHER:
                        await GLOBAL_WATCHER.remove_ticker(ticker)
            except json.JSONDecodeError:
                pass
    except WebSocketDisconnect:
        manager.disconnect(websocket)
