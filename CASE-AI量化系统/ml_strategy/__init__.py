# -*- coding: utf-8 -*-
# Author：@跟陈博士学AI
"""ml_strategy 模块: ML 概率因子策略 (基于 11 章 feature_engine + ml_engine 简化整合)

唯一对外入口: ml_strategy.ml_prob_runner.run_ml_prob(df, **params)
被三处共用: parameter_tuning.walk_forward / lib.backtest_engine / live_trading.live_loop
"""

from ml_strategy.ml_prob_runner import run_ml_prob, clear_cache  # noqa: F401
