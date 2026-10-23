"""
策略模块

注：`four_in_one_v7.py`（FourInOneStrategy）与 `four_in_one_v4.py`（IndicatorConfig）
已于 2026-08-15 作为死代码移除——全项目无任何调用方，v7 仅被本文件 import 而没有
任何代码 import `app.strategies` 包本身，v4 连本文件都不进。备份在回收站，
git 历史 c7ce8de 可取回。对应的 `config.V7_PARAMS` / `V4_PARAMS` 同步删除。

现存实际在用的是 base.py 的信号引擎基类，消费方为 `app/watcher/watcher_manager.py`
（SignalEngineA/B/C2/C3/D/E/F/G/H）与 `app/services/signal_handler.py`。
"""

from .base import BaseStrategy, StrategyResult, StrategySignal

__all__ = ['BaseStrategy', 'StrategyResult', 'StrategySignal']
