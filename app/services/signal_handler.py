# -*- coding: utf-8 -*-
"""信号处理服务"""
from datetime import datetime
import pytz

from app.db.signal_store import save_signal
from app.watcher.market_regime import calculate_current_regime
from app.strategies.base import StrategyResult, SignalEvent
from app.services.trading_config import get_config


async def handle_signal_event(manager, ticker: str, event: SignalEvent):
    """处理 Signal Engine A/B 产出的信号事件"""
    cfg = get_config()
    strategy_cfg = cfg.get('signal_strategy', {})
    alert_key = f"{event.strategy.lower()}_alerts_enabled"
    if not strategy_cfg.get(alert_key, True):
        return  # 该策略提醒已关闭，完全屏蔽

    message = {
        'type': 'signal_event',
        'ticker': ticker,
        'data': {
            'strategy': event.strategy,
            'signal_type': event.signal_type,
            'direction': event.direction,
            'confidence': event.confidence,
            'label': event.label,
            'metadata': event.metadata,
            'timestamp': datetime.now(pytz.UTC).isoformat(),
        }
    }
    await manager.broadcast(message)
    print(f"[Strategy{event.strategy}] {ticker} {event.label} ({event.direction}/{event.confidence})")

    stored_type = f"{event.strategy}_{event.signal_type}"

    save_signal(
        ticker=ticker,
        triggered_at=datetime.now(pytz.timezone('America/New_York')),
        signal_type=stored_type,
        source=event.strategy,
        fast=event.metadata.get('v4_fast'),
        slow=event.metadata.get('v4_slow'),
        regime_score=event.metadata.get('regime_score'),
        regime_label=event.confidence,
    )


async def handle_signal(manager, watcher_manager, ticker: str, result: StrategyResult):
    """处理 V7 策略信号"""
    cfg = get_config()
    if not cfg.get('signal_strategy', {}).get('v7_alerts_enabled', True):
        return  # V7 提醒已关闭，完全屏蔽

    signals = result.signals
    message = {
        'type': 'signal',
        'ticker': ticker,
        'data': {
            'fast': result.fast,
            'slow': result.slow,
            'signals': {
                'golden_cross': signals.golden_cross,
                'death_cross': signals.death_cross,
                'bull_pattern': signals.bull_pattern,
                'bear_pattern': signals.bear_pattern,
            },
            'risk': {
                'in_opening': result.in_opening,
                'extreme_volatility': result.extreme_volatility,
                'in_saturation': result.in_saturation,
            },
            'atr_quality': getattr(result, 'atr_quality', None),
            'time_factor': getattr(result, 'time_factor', None),
            'timestamp': datetime.now(pytz.UTC).isoformat(),
        }
    }
    await manager.broadcast(message)

    fired = {
        'golden_cross': signals.golden_cross,
        'death_cross': signals.death_cross,
        'bull_pattern': signals.bull_pattern,
        'bear_pattern': signals.bear_pattern,
    }
    if any(fired.values()):
        regime = None
        if watcher_manager and ticker in watcher_manager._watchers:
            df_5m = watcher_manager._watchers[ticker].data_manager.get_df_5m(tail=100)
            if not df_5m.empty:
                regime = calculate_current_regime(df_5m)

        triggered_at = datetime.now(pytz.timezone('America/New_York'))
        for signal_type, did_fire in fired.items():
            if did_fire:
                save_signal(
                    ticker=ticker,
                    triggered_at=triggered_at,
                    signal_type=signal_type,
                    source='V7',
                    fast=result.fast,
                    slow=result.slow,
                    regime_text=regime['text'] if regime else None,
                    regime_score=(regime['bull_score_5m'] - regime['bear_score_5m']) if regime else None,
                    regime_label=regime['label'] if regime else None,
                )


async def handle_state_update(
    manager, watcher_manager, ticker: str, state: dict
):
    """处理状态更新"""
    if 'current_bar' in state and state['current_bar'] and 'datetime' in state['current_bar']:
        state['current_bar'].pop('datetime', None)

    message = {
        'type': 'update',
        'ticker': ticker,
        'data': state,
    }
    await manager.broadcast(message)
