# -*- coding: utf-8 -*-
"""回归测试：注入 GLOBAL_WATCHER 的信号回调必须能正确落库并广播。"""
import asyncio
from unittest.mock import AsyncMock, patch

from app.routers import watcher_ws
from app.strategies.base import SignalEvent
from app.watcher.watcher_manager import GLOBAL_WATCHER


def test_handle_signal_event_saves_and_broadcasts():
    event = SignalEvent(
        strategy='A',
        signal_type='cross_confirm',
        direction='bull',
        confidence='high',
        label='test',
        metadata={},
    )

    async def run():
        with patch('app.services.signal_handler.get_config', return_value={'signal_strategy': {}}), \
             patch('app.services.signal_handler.save_signal') as mock_save, \
             patch.object(watcher_ws.manager, 'broadcast', new=AsyncMock()) as broadcast:
            callback = GLOBAL_WATCHER.on_signal_event
            assert callback is watcher_ws._broadcast_signal_event
            await callback('AMD', event)
            return mock_save, broadcast

    mock_save, broadcast = asyncio.run(run())

    assert mock_save.called
    kwargs = mock_save.call_args.kwargs
    assert kwargs['ticker'] == 'AMD'
    assert kwargs['signal_type'] == 'A_cross_confirm'

    assert broadcast.called
    payload = broadcast.call_args.args[0]
    assert payload['type'] == 'signal_event'
    assert payload['ticker'] == 'AMD'
