# -*- coding: utf-8 -*-
# Author：@跟陈博士学AI
"""把第 13 课缠论买卖点接到策略注册表。

分析器用课程里的 ChanAnalyzer，不另写笔和中枢。
进出场跟课程回测一致：买点买入；跌破结构、收盘涨幅达到 15%，或三卖，离场。
买点标在分型那一天，但要等下一根日 K 走出来才知道。策略在这根确认 K 线收盘发单，再下一根开盘成交。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pandas as pd

_CHAN_ANALYZER = None
_BARS = 500
_MIN_BARS = 80
_TAKE_PROFIT = 0.15

# 买点 -> 信号列取值。止损列：一买、二买用 chan_zd，三买用 chan_zg。
_BUY_SPEC = {
    "chan_buy1": (1, "chan_zd", "一买"),
    "chan_buy2": (2, "chan_zd", "二买"),
    "chan_buy3": (3, "chan_zg", "三买"),
}


def _analyzer_cls():
    global _CHAN_ANALYZER
    if _CHAN_ANALYZER is not None:
        return _CHAN_ANALYZER
    path = (Path(__file__).resolve().parents[3]
            / "13-缠论精华量化" / "CASE-缠论精华量化" / "chan_analyzer.py")
    spec = importlib.util.spec_from_file_location("chan_course_analyzer", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _CHAN_ANALYZER = mod.ChanAnalyzer
    return _CHAN_ANALYZER


def _detect_second_buys(analyzer):
    """中枢后下跌笔结束，反弹再回调且不破该低点，记为二买。

    从 5-缠论二买策略回测.py 原样复制。分析器已经给出二买时直接用那一批。
    """
    detected = [s for s in analyzer.signals if s['type'] == 'second_buy']
    if detected:
        return detected

    signals = []
    used_dates = set()
    for zs in analyzer.zhongshu_list:
        post_bis = [b for b in analyzer.bi_list if b['start_index'] >= zs['end_index']]
        down = next((b for b in post_bis if b['direction'] == 'down'), None)
        if down is None:
            continue

        anchor_price = down['end_price']
        saw_up = False
        for bi in post_bis:
            if bi['start_date'] <= down['end_date']:
                continue
            if bi['direction'] == 'up':
                saw_up = True
            elif bi['direction'] == 'down' and saw_up:
                sig_date = bi.get('end_raw_date', bi['end_date'])
                if bi['end_price'] > anchor_price and sig_date not in used_dates:
                    signals.append({
                        'date': sig_date,
                        'type': 'second_buy',
                        'price': bi['end_price'],
                        'first_buy_price': anchor_price,
                    })
                    used_dates.add(sig_date)
                break

    signals.sort(key=lambda s: s['date'])
    return signals


def _prepare_frame(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out.index = pd.to_datetime(out.index).normalize()
    out = out[~out.index.duplicated(keep="last")].sort_index()
    for col in ("open", "high", "low", "close", "volume"):
        out[col] = pd.to_numeric(out[col], errors="coerce")
    return out.dropna(subset=["open", "high", "low", "close"])


def _signal_frame(df: pd.DataFrame, strategy_name: str) -> pd.DataFrame:
    analyzer = _analyzer_cls()(df)
    analyzer.analyze()
    signal_df = analyzer.get_signal_df()
    if strategy_name != "chan_buy2":
        return signal_df
    for sig in _detect_second_buys(analyzer):
        date = sig["date"]
        if date in signal_df.index and signal_df.loc[date, "chan_signal"] == 0:
            signal_df.loc[date, "chan_signal"] = 2
            signal_df.loc[date, "chan_zd"] = sig["first_buy_price"]
    return signal_df


def _stop_from_row(row, stop_col: str, close: float) -> float:
    anchor = row.get(stop_col)
    try:
        anchor = float(anchor)
    except (TypeError, ValueError):
        anchor = 0.0
    if anchor > 0:
        return anchor
    return close * 0.93


def _last_action(signal_df: pd.DataFrame, buy_code: int, stop_col: str) -> tuple:
    """沿信号列走持仓。买点和三卖都用前一根 K 线，因为当天分析还看不到刚走完的笔。"""
    in_pos = False
    entry = None
    stop = None
    armed = None
    action = "hold"
    reason = "无买卖点"
    prev_sig = 0

    for i, (_, row) in enumerate(signal_df.iterrows()):
        action = "hold"
        reason = "无买卖点"
        close = float(row["close"])
        if armed is not None and not in_pos:
            in_pos = True
            entry = float(row["open"])
            stop = armed
            armed = None
        if in_pos:
            if stop and close < stop:
                action = "sell"
                reason = f"收盘 {close:.2f} 跌破结构 {stop:.2f}"
                in_pos = False
                entry = None
                stop = None
            elif entry and entry > 0 and close / entry - 1 >= _TAKE_PROFIT:
                action = "sell"
                reason = f"收盘较成本上涨 {(close / entry - 1):.1%}，止盈"
                in_pos = False
                entry = None
                stop = None
            elif prev_sig == -3:
                action = "sell"
                reason = "三卖已确认"
                in_pos = False
                entry = None
                stop = None
        if action != "sell" and not in_pos and armed is None and prev_sig == buy_code:
            action = "buy"
            armed = _stop_from_row(signal_df.iloc[i - 1], stop_col, float(signal_df.iloc[i - 1]["close"]))
            reason = f"买点确认，结构止损 {armed:.2f}"
        prev_sig = int(row["chan_signal"])
    return action, reason


def evaluate_chan(strategy_name: str, code: str, market) -> dict:
    buy_code, stop_col, title = _BUY_SPEC[strategy_name]
    try:
        df = market.get_recent_kline(code, period="1d", count=_BARS)
    except Exception:
        df = None
    if df is None or len(df) < _MIN_BARS:
        return {"side": "hold", "strategy": strategy_name, "reason": "日 K 不足，缠论无法成笔"}
    frame = _prepare_frame(df)
    if len(frame) < _MIN_BARS:
        return {"side": "hold", "strategy": strategy_name, "reason": "日 K 不足，缠论无法成笔"}
    signal_df = _signal_frame(frame, strategy_name)
    side, reason = _last_action(signal_df, buy_code, stop_col)
    if side == "hold":
        return {"side": "hold", "strategy": strategy_name, "reason": reason}
    return {"side": side, "strategy": strategy_name, "reason": f"{title} {reason}"}
