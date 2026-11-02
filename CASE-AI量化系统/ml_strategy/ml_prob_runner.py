# -*- coding: utf-8 -*-
# Author：@跟陈博士学AI
"""ML 概率因子统一入口 -- WF / 回测引擎 / 实盘 live_loop 三处共用

唯一函数 run_ml_prob(df, **params) 接受日 K 线 DataFrame, 返回:
    {
        'prob_series': pd.Series,        # 全周期每日 ML 涨概率 (索引同 df.index)
        'signals':     List[dict],        # [{idx, date, side, prob}, ...]
        'trades':      List[dict],        # [{entry_idx, exit_idx, return_pct}, ...] 给 WF 用
    }

为什么三处共用一个函数:
    - WF 检测说"严重过拟合", 实盘信号必须用同一个模型同一份特征算出来, 否则会出现
      "WF 上 OOS Sharpe -0.3, 实盘信号又是另一回事" 的诡异情况.
    - 单点入口便于做缓存 (回测 200 天评估 + 实盘 1 分钟一轮, 避免重复训练).

缓存策略:
    key = (code, last_date_str, params_hash)
    - code 为 None 时禁用缓存 (WF 跑训练/评估两段不同 df, 不能复用)
    - 同一交易日内, 实盘多次评估同一只票直接命中
    - clear_cache() 进程启动时调一次清空
"""

from __future__ import annotations
import hashlib
import json
import math
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from ml_strategy.feature_engine import (
    calc_features,
    preprocess_features,
    get_all_feature_cols,
)
from ml_strategy.ml_engine import make_labels, rolling_train_predict


# ============================================================
# 缓存 (内存级, 进程内有效)
# ============================================================

_PROB_CACHE: Dict[Tuple[str, str, str], pd.Series] = {}


def clear_cache() -> int:
    """清空缓存, 返回清空前的条目数"""
    n = len(_PROB_CACHE)
    _PROB_CACHE.clear()
    return n


def _make_cache_key(code: str, df: pd.DataFrame, params: Dict[str, Any]) -> Tuple[str, str, str]:
    """缓存 key: (code, last_date, params_hash)
    last_date 用 df 最后一根 K 的索引串, 任何新数据进来都会 miss 缓存.
    params_hash 用稳定排序后的 json 摘要, 保证同参数同 hash.
    """
    last_date = str(df.index[-1])[:19]    # 取到秒级即可, 日 K 实际只到日
    p_str = json.dumps(params, sort_keys=True, default=str)
    p_hash = hashlib.md5(p_str.encode("utf-8")).hexdigest()[:12]
    return (str(code), last_date, p_hash)


# ============================================================
# 核心: 全周期 prob 序列计算
# ============================================================

def _compute_prob_series(
    df: pd.DataFrame,
    train_days: int,
    retrain_interval: int,
    horizon: int,
    model_type: str,
    verbose: bool,
) -> pd.Series:
    """对 df 整段计算每日 ML 涨概率序列, 索引对齐 df.index.

    无样本的位置 (训练窗口前 + label 缺失尾部) 填 0.5 (中性, 既不买也不卖).
    """
    feat_df = calc_features(df.copy())
    feat_df['label'] = make_labels(feat_df, horizon=horizon, method='binary')

    feature_cols = [c for c in get_all_feature_cols() if c in feat_df.columns]

    # 对特征做时序 MAD + Z-score (整段一次, 不滚动 -- 单股技术因子量级稳定, 滚动 z-score 边际收益小, 性能差距大)
    feat_df = preprocess_features(feat_df, feature_cols)

    pred_df = rolling_train_predict(
        feat_df,
        feature_cols=feature_cols,
        label_col='label',
        model_type=model_type,
        train_days=train_days,
        retrain_interval=retrain_interval,
        params=None,
        verbose=verbose,
    )

    prob_series = pd.Series(0.5, index=df.index, dtype=float)
    if not pred_df.empty:
        for _, row in pred_df.iterrows():
            i = int(row['row_idx'])
            if 0 <= i < len(prob_series):
                prob_series.iloc[i] = float(row['y_prob'])
    return prob_series


# ============================================================
# 信号 + 交易构造 (供 WF / 回测 / 实盘共用)
# ============================================================

def _prob_to_signals_and_trades(
    df: pd.DataFrame,
    prob_series: pd.Series,
    buy_th: float,
    sell_th: float,
) -> Tuple[List[dict], List[dict]]:
    """根据 prob_series + 阈值构造信号与交易列表

    规则 (与实盘一致):
        - 不在仓 + prob > buy_th  -> 入场 (entry_idx = i)
        - 在仓   + prob < sell_th -> 出场 (exit_idx = i)
        - 末根仍持仓 -> 用最后一根 close 强平 (供 WF 算累计收益, 不留浮盈悬空)

    return_pct 单位 % (与 walk_forward.py 其他策略一致).
    """
    close = df['close'].astype(float).values
    n = len(close)
    probs = prob_series.values

    signals: List[dict] = []
    trades: List[dict] = []

    in_pos = False
    entry_price = 0.0
    entry_idx = -1

    for i in range(n):
        p = float(probs[i])
        if math.isnan(p):
            continue

        if not in_pos and p > buy_th:
            in_pos = True
            entry_price = float(close[i])
            entry_idx = i
            signals.append({
                'idx': i, 'date': str(df.index[i])[:10],
                'side': 'buy', 'prob': p,
            })
        elif in_pos and p < sell_th:
            ret = (close[i] - entry_price) / entry_price * 100.0
            trades.append({
                'entry_idx': entry_idx, 'exit_idx': i, 'return_pct': float(ret),
            })
            signals.append({
                'idx': i, 'date': str(df.index[i])[:10],
                'side': 'sell', 'prob': p,
            })
            in_pos = False

    if in_pos and entry_idx >= 0 and entry_idx < n - 1:
        last_i = n - 1
        ret = (close[last_i] - entry_price) / entry_price * 100.0
        trades.append({
            'entry_idx': entry_idx, 'exit_idx': last_i, 'return_pct': float(ret),
        })

    return signals, trades


# ============================================================
# 唯一对外入口
# ============================================================

def run_ml_prob(
    df: pd.DataFrame,
    *,
    train_days: int = 120,
    retrain_interval: int = 20,
    buy_th: float = 0.6,
    sell_th: float = 0.4,
    horizon: int = 1,
    model_type: str = 'xgboost',
    code: Optional[str] = None,
    verbose: bool = False,
) -> Dict[str, Any]:
    """ML 概率因子统一入口 -- 接日 K, 返回 prob_series + signals + trades

    参数:
        df: 含 OHLCV 的 DataFrame, 索引升序日期, 至少 train_days + 30 行
        train_days: 滚动训练窗口大小 (默认 120 个交易日, ~6 个月)
        retrain_interval: 重训间隔 (默认 20, 即每月重训一次)
        buy_th: 买入阈值 (prob > buy_th 入场)
        sell_th: 卖出阈值 (prob < sell_th 出场)
        horizon: 预测 N 日后涨跌 (默认 1)
        model_type: 'xgboost' (默认) / 'lightgbm'
        code: 缓存 key (实盘/回测引擎传, WF 不传 -- WF 训练/评估窗口不同, 不能复用)
        verbose: 是否打印训练进度

    返回:
        {
            'prob_series': pd.Series (索引 = df.index, 值 = 0~1, 缺失补 0.5),
            'signals':     List[{'idx', 'date', 'side', 'prob'}],
            'trades':      List[{'entry_idx', 'exit_idx', 'return_pct'}],  # WF 用
            'cached':      bool (是否命中缓存),
            'meta':        {'n_train_predict': int, 'last_prob': float, ...},
        }
    """
    cache_params = {
        'train_days':       train_days,
        'retrain_interval': retrain_interval,
        'horizon':          horizon,
        'model_type':       model_type,
    }
    cache_hit = False

    if code is not None:
        key = _make_cache_key(code, df, cache_params)
        if key in _PROB_CACHE:
            prob_series = _PROB_CACHE[key]
            cache_hit = True
        else:
            prob_series = _compute_prob_series(
                df, train_days, retrain_interval, horizon, model_type, verbose,
            )
            _PROB_CACHE[key] = prob_series
    else:
        prob_series = _compute_prob_series(
            df, train_days, retrain_interval, horizon, model_type, verbose,
        )

    signals, trades = _prob_to_signals_and_trades(df, prob_series, buy_th, sell_th)

    last_prob = float(prob_series.iloc[-1]) if len(prob_series) else 0.5
    n_pred = int((prob_series != 0.5).sum())

    return {
        'prob_series': prob_series,
        'signals':     signals,
        'trades':      trades,
        'cached':      cache_hit,
        'meta': {
            'n_train_predict': n_pred,
            'last_prob':       last_prob,
            'n_signals':       len(signals),
            'n_trades':        len(trades),
            'model_type':      model_type,
            'train_days':      train_days,
            'retrain_interval': retrain_interval,
            'buy_th':          buy_th,
            'sell_th':         sell_th,
        },
    }
