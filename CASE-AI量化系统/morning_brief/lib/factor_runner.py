# -*- coding: utf-8 -*-
# Author：@跟陈博士学AI
# 多因子选股运行器（晨会内嵌）
from __future__ import annotations
import math
from datetime import date
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from .db_config import execute_query


def load_kline_from_db(stock_code: str, lookback_days: int = 200,
                        table: str = "trade_stock_daily") -> pd.DataFrame:
    """从日 K 表加载单股最近 N 个交易日。A 股 trade_stock_daily，美股 trade_stock_us_daily。"""
    if table not in ("trade_stock_daily", "trade_stock_us_daily"):
        raise ValueError(f"不支持的日K表: {table}")
    sql = f"""
        SELECT trade_date, open_price, high_price, low_price, close_price,
               volume, amount
        FROM {table}
        WHERE stock_code = %s
        ORDER BY trade_date DESC
        LIMIT %s
    """
    rows = execute_query(sql, (stock_code, lookback_days))
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows[::-1])
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    df.set_index("trade_date", inplace=True)
    df.rename(columns={
        "open_price": "open", "high_price": "high",
        "low_price":  "low",  "close_price": "close",
    }, inplace=True)
    for col in ["open", "high", "low", "close", "amount"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0)
    return df


def _safe_pct_change(prices: pd.Series, periods: int) -> float:
    if len(prices) <= periods:
        return np.nan
    p_now  = prices.iloc[-1]
    p_then = prices.iloc[-1 - periods]
    if p_then <= 0:
        return np.nan
    return p_now / p_then - 1.0


def calc_factors_for_one(df: pd.DataFrame, total_share: float = 0) -> Dict[str, float]:
    if df is None or len(df) < 130:
        return {}

    close  = df["close"].astype(float)
    volume = df["volume"].astype(float) if "volume" in df.columns else None
    amount = df["amount"].astype(float) if "amount" in df.columns else None

    returns = close.pct_change().dropna()
    if len(returns) < 100:
        return {}

    f = {}
    f["MOM_1M"] = _safe_pct_change(close, 21)
    f["MOM_3M"] = _safe_pct_change(close, 63)
    f["MOM_6M"] = _safe_pct_change(close, 126)

    rev_5d = _safe_pct_change(close, 5)
    f["REV_5D"] = -rev_5d if not np.isnan(rev_5d) else np.nan

    vol_20 = returns.tail(20).std() * math.sqrt(250)
    vol_60 = returns.tail(60).std() * math.sqrt(250)
    f["VOL_20"] = -vol_20 if not np.isnan(vol_20) else np.nan
    f["VOL_60"] = -vol_60 if not np.isnan(vol_60) else np.nan

    if amount is not None and len(amount) >= 20:
        liq_20 = amount.tail(20).mean()
        f["LIQ_20"] = -math.log(max(liq_20, 1.0))
    else:
        f["LIQ_20"] = np.nan

    if volume is not None and len(volume) >= 20:
        if total_share > 0:
            turn_20 = (volume.tail(20).mean() / total_share) * 100
        else:
            long_vol = volume.tail(60).mean()
            turn_20 = volume.tail(20).mean() / long_vol if long_vol > 0 else np.nan
        f["TURN_20"] = -turn_20 if not np.isnan(turn_20) else np.nan
    else:
        f["TURN_20"] = np.nan

    delta = close.diff()
    gain = delta.where(delta > 0, 0).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = 100 - 100 / (1 + rs)
    rsi_val = rsi.iloc[-1] if len(rsi) > 0 else np.nan
    f["RSI_14"] = (rsi_val - 50) if not np.isnan(rsi_val) else np.nan

    ma20 = close.rolling(20).mean().iloc[-1]
    bias_20 = (close.iloc[-1] - ma20) / ma20 if ma20 > 0 else np.nan
    f["BIAS_20"] = -bias_20 if not np.isnan(bias_20) else np.nan

    return f


def calc_factors_batch(stock_codes: List[str], lookback_days: int = 200,
                       table: str = "trade_stock_daily") -> pd.DataFrame:
    rows = {}
    for code in stock_codes:
        df = load_kline_from_db(code, lookback_days=lookback_days, table=table)
        if df.empty:
            continue
        f = calc_factors_for_one(df)
        if f:
            rows[code] = f

    df_result = pd.DataFrame.from_dict(rows, orient="index")
    print(f"  [FACTOR] {len(df_result)} 只有效, "
          f"{len(stock_codes) - len(df_result)} 只数据不足被剔除")
    return df_result


def winsorize_mad(series: pd.Series, n: float = 3.0) -> pd.Series:
    s = series.copy()
    median = s.median()
    mad = (s - median).abs().median()
    if mad == 0 or np.isnan(mad):
        return s
    upper = median + n * 1.4826 * mad
    lower = median - n * 1.4826 * mad
    return s.clip(lower=lower, upper=upper)


def zscore(series: pd.Series) -> pd.Series:
    s = series.copy()
    mean = s.mean()
    std  = s.std(ddof=1)
    if std == 0 or np.isnan(std):
        return s * 0.0
    return (s - mean) / std


def industry_neutralize(factor_series: pd.Series, industry_map: dict) -> pd.Series:
    df = pd.DataFrame({
        "factor":   factor_series,
        "industry": pd.Series(industry_map),
    })
    df = df.dropna(subset=["industry"])
    return df.groupby("industry")["factor"].transform(zscore)


def preprocess_factors(factor_df: pd.DataFrame,
                        industry_map: Optional[dict] = None,
                        winsorize_n: float = 3.0,
                        neutralize: bool = True) -> pd.DataFrame:
    result = pd.DataFrame(index=factor_df.index)
    for col in factor_df.columns:
        s = factor_df[col].dropna()
        if len(s) == 0:
            result[col] = factor_df[col]
            continue
        s_w = winsorize_mad(s, n=winsorize_n)
        s_z = zscore(s_w)
        if neutralize and industry_map:
            s_z = industry_neutralize(s_z, industry_map)
            s_z = zscore(s_z)
        result[col] = s_z
    return result


def filter_tradable(stock_codes: List[str], min_listed_days: int = 250) -> List[str]:
    if not stock_codes:
        return []

    placeholders = ",".join(["%s"] * len(stock_codes))
    rows = execute_query(
        f"SELECT stock_code, stock_name, list_date FROM trade_stock_status "
        f"WHERE stock_code IN ({placeholders})",
        stock_codes)
    info = {r["stock_code"]: r for r in rows}
    today = date.today()

    keep = []
    for code in stock_codes:
        meta = info.get(code)
        if not meta:
            continue
        name = (meta.get("stock_name") or "")
        if "ST" in name.upper() or "退" in name:
            continue
        listed = meta.get("list_date")
        if listed:
            try:
                ld = listed if isinstance(listed, date) else date.fromisoformat(str(listed))
                if (today - ld).days < min_listed_days:
                    continue
            except Exception:
                pass
        keep.append(code)

    return keep
