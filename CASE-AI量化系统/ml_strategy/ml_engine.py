# -*- coding: utf-8 -*-
# Author：@跟陈博士学AI
"""ML 训练 + 滚动预测引擎

来源: 11-机器学习因子挖掘/CASE-机器学习因子挖掘/ml_engine.py 原版直接 copy 后裁剪.
裁剪原则:
    - 保留: 二分类标签 (make_labels) + XGBoost/LightGBM 训练 (train_xgboost/train_lightgbm)
            + 滚动训练预测 (rolling_train_predict)
    - 删除: RandomForest / Purged K-Fold / IC 因子评估 / 多模型集成 / Stacking
            (本项目只用 ML 概率信号驱动单只股票交易, 不做集成与因子评估)
"""
import numpy as np
import pandas as pd
import warnings
warnings.filterwarnings('ignore')


# ============================================================
# 标签构建
# ============================================================

def make_labels(df, horizon=1, method='binary'):
    """构建预测标签

    参数:
        df: DataFrame, 需含 close 列
        horizon: 预测时间窗口 (天数)
        method: 'binary' (涨=1 / 跌=0)
    """
    future_ret = df['close'].shift(-horizon) / df['close'] - 1
    if method == 'binary':
        return (future_ret > 0).astype(int)
    raise ValueError(f"不支持的标签方法: {method}")


# ============================================================
# 模型训练接口 (XGBoost / LightGBM)
# ============================================================

def train_xgboost(X_train, y_train, params=None):
    """训练 XGBoost 二分类器"""
    import xgboost as xgb

    default_params = {
        'n_estimators': 200,
        'max_depth': 5,
        'learning_rate': 0.05,
        'subsample': 0.8,
        'colsample_bytree': 0.8,
        'min_child_weight': 10,
        'reg_alpha': 0.1,
        'reg_lambda': 1.0,
        'random_state': 42,
        'use_label_encoder': False,
        'eval_metric': 'logloss',
        'verbosity': 0,
    }
    if params:
        default_params.update(params)

    model = xgb.XGBClassifier(**default_params)
    model.fit(X_train, y_train)
    return model


def train_lightgbm(X_train, y_train, params=None):
    """训练 LightGBM 二分类器"""
    import lightgbm as lgb

    default_params = {
        'n_estimators': 200,
        'max_depth': 5,
        'learning_rate': 0.05,
        'num_leaves': 31,
        'subsample': 0.8,
        'colsample_bytree': 0.8,
        'min_child_samples': 20,
        'reg_alpha': 0.1,
        'reg_lambda': 1.0,
        'random_state': 42,
        'verbose': -1,
    }
    if params:
        default_params.update(params)

    model = lgb.LGBMClassifier(**default_params)
    model.fit(X_train, y_train)
    return model


TRAIN_FUNCS = {
    'xgboost':  train_xgboost,
    'lightgbm': train_lightgbm,
}


# ============================================================
# 滚动训练预测 (Walk-Forward 思想的核心)
# ============================================================

def rolling_train_predict(df, feature_cols, label_col='label',
                          model_type='xgboost', train_days=120,
                          retrain_interval=20, params=None,
                          verbose=False):
    """滚动窗口训练与预测

    流程: 过去 train_days 天训练 -> 预测下一天 -> 滑动 1 天
          每隔 retrain_interval 天 (即模型上次训练后又过了 N 天) 重训一次

    参数:
        df: DataFrame, 含特征列与标签列, 索引升序
        feature_cols: 特征列名列表
        label_col: 标签列名
        model_type: 'xgboost' / 'lightgbm'
        train_days: 训练窗口大小
        retrain_interval: 重训间隔天数
        params: 模型超参 (覆盖默认)
        verbose: 是否打印进度

    返回:
        DataFrame, 列: date / row_idx / y_true / y_pred / y_prob
                  其中 row_idx 是相对原 df 的整数 iloc 位置 (用来回灌到全周期 prob_series)
    """
    train_func = TRAIN_FUNCS.get(model_type)
    if train_func is None:
        raise ValueError(f"不支持的模型类型: {model_type}")

    # 用 reset_index(drop=False) 保留原索引列, 同时引入 'orig_idx' 标记原 iloc 位置
    df_full = df.copy()
    df_full['__orig_idx__'] = np.arange(len(df_full))
    df_clean = df_full.dropna(subset=feature_cols + [label_col]).reset_index(drop=False)

    if 'trade_date' in df_clean.columns:
        date_col = 'trade_date'
    elif df_clean.index.name and 'date' in (df_clean.index.name or '').lower():
        df_clean = df_clean.reset_index()
        date_col = df_clean.columns[0]
    else:
        # 第一列就是 reset 后保留的 (原索引)
        date_col = df_clean.columns[0]

    results = []
    model = None
    last_train_idx = -retrain_interval
    total = max(0, len(df_clean) - train_days)

    for i in range(train_days, len(df_clean)):
        if model is None or (i - last_train_idx) >= retrain_interval:
            train_start = max(0, i - train_days)
            train_data = df_clean.iloc[train_start:i]

            X_train = train_data[feature_cols].values
            y_train = train_data[label_col].values

            if len(np.unique(y_train)) < 2:
                continue

            model = train_func(X_train, y_train, params)
            last_train_idx = i

        row = df_clean.iloc[i]
        X_test = row[feature_cols].values.reshape(1, -1)

        y_pred = int(model.predict(X_test)[0])
        y_prob = float(model.predict_proba(X_test)[0, 1])

        results.append({
            'date':     row[date_col],
            'row_idx':  int(row['__orig_idx__']),
            'y_true':   int(row[label_col]),
            'y_pred':   y_pred,
            'y_prob':   y_prob,
        })

        if verbose and len(results) and len(results) % 100 == 0:
            print(f"  [{model_type}] 已预测 {len(results)}/{total} 天")

    return pd.DataFrame(results)
