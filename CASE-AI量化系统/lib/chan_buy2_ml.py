# -*- coding: utf-8 -*-
# Author：@跟陈博士学AI
"""第 13 课机器学习增强二买。

检测、特征和 LightGBM 参数从 8-ML增强二买策略-COHR.py / 8-ML增强二买策略.py 复制。
标签仍是信号之后 20 根日 K 的最大涨幅是否超过 5%，概率达到 0.5 才入场。
训练只用结果已经走完的二买：结果日落在决策日之前。美股用课件里的全部美股，A 股用课件里的六只。
买点仍等下一根日 K 确认后发单。离场是参照低点、三卖、中枢下沿，以及从持仓后最高收盘回撤 15%。
"""
from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

from lib.chan_strategy import _analyzer_cls, _prepare_frame

_NAME = "chan_buy2_ml"
_BARS = 500
_MIN_BARS = 80
_THRESHOLD = 0.5
_TRAIL = 0.15
_LABEL_BARS = 20
_LABEL_GAIN = 0.05
_FEATURE_COLS = [
    "zs_height_ratio", "zs_width", "bi_in_zs", "bi_slope", "pullback_depth",
    "atr_ratio", "adx", "vol_ratio", "rsi", "macd_hist", "momentum_10d",
]

# 与 8-ML增强二买策略-COHR.py 的 TRAIN_STOCKS 一致
_TRAIN_US = [
    "IXIC.US", "COHR.US", "RKLB.US", "MSFT.US", "COPX.US", "GOOG.US", "IPSC.US",
    "NVDA.US", "AAPL.US", "TSLA.US", "JPM.US", "BAC.US", "GS.US", "WFC.US",
    "BRK-B.US", "PGR.US", "MET.US", "AIG.US", "PLD.US", "AMT.US", "SPG.US", "O.US",
    "KO.US", "PEP.US", "MCD.US", "GIS.US", "DE.US", "ADM.US", "CTVA.US", "MOS.US",
    "BA.US", "LMT.US", "RTX.US", "NOC.US", "LLY.US", "JNJ.US", "PFE.US", "MRK.US",
    "UNH.US", "LIN.US", "DOW.US", "DD.US", "ECL.US", "CAT.US", "HON.US", "GE.US",
    "UNP.US", "ETN.US", "MAR.US", "HLT.US", "SBUX.US", "YUM.US", "UPS.US", "FDX.US",
    "EXPD.US", "XOM.US", "CVX.US", "COP.US", "SLB.US", "DHI.US", "LEN.US", "URI.US",
    "MLM.US", "WM.US", "RSG.US", "AWK.US", "FRO.US", "MATX.US", "ZIM.US", "DAC.US",
    "T.US", "VZ.US", "TMUS.US", "ODFL.US", "KNX.US", "JBHT.US", "GM.US", "F.US",
    "STLA.US", "APTV.US", "DELL.US", "IBM.US", "ORCL.US", "AMD.US", "EMR.US",
    "ROK.US", "PWR.US", "HUBB.US", "FCX.US", "NEM.US", "SHW.US", "ALB.US",
    "WMT.US", "COST.US", "HD.US", "AMZN.US", "FLEX.US", "JBL.US", "TXN.US",
    "AMAT.US", "DIS.US", "NFLX.US", "CMCSA.US", "FOX.US", "BG.US", "GWW.US",
    "SYY.US", "FAST.US", "GD.US", "LHX.US", "HII.US", "LDOS.US", "BTU.US",
    "HCC.US", "AMR.US", "CNR.US", "NUE.US", "STLD.US", "CLF.US", "RS.US",
]
# 与 8-ML增强二买策略.py 的 TRAIN_STOCKS 一致
_TRAIN_CN = [
    "688126.SH", "300750.SZ", "000001.SZ", "688981.SH", "601318.SH", "159941.SZ",
]
_TRAIN_START = {"us": "2021-09-22", "cn": "2022-01-01"}

# (市场, 截止日) -> 模型；None 表示样本不够，避免每次重训
_MODEL_CACHE: dict = {}


def detect_second_buys(analyzer):
    """放宽后的二买：分析器二买优先，中枢后不破参照低点，新低后重置再确认。"""
    signals = []
    used_dates = set()
    for sig in analyzer.signals:
        if sig["type"] != "second_buy":
            continue
        signals.append(sig)
        used_dates.add(sig["date"])

    for zs in analyzer.zhongshu_list:
        post_bis = [b for b in analyzer.bi_list if b["start_index"] >= zs["end_index"]]
        if not post_bis:
            continue
        _append_hold_above(
            signals, used_dates, post_bis,
            anchor_price=zs["ZD"], anchor_index=post_bis[0]["start_index"],
            require_prior_up=post_bis[0]["direction"] == "up",
        )
        _scan_reset_anchor(signals, used_dates, post_bis)

    signals.sort(key=lambda s: s["date"])
    return signals


def _append_hold_above(signals, used_dates, post_bis, anchor_price, anchor_index, require_prior_up):
    saw_up = not require_prior_up
    for bi in post_bis:
        if bi["end_index"] <= anchor_index:
            continue
        if bi["direction"] == "up":
            saw_up = True
            continue
        if not saw_up:
            continue
        sig_date = bi.get("end_raw_date", bi["end_date"])
        if bi["end_price"] > anchor_price and sig_date not in used_dates:
            signals.append({
                "date": sig_date,
                "type": "second_buy",
                "price": bi["end_price"],
                "first_buy_price": anchor_price,
            })
            used_dates.add(sig_date)
        return


def _scan_reset_anchor(signals, used_dates, post_bis):
    anchor_price = None
    anchor_index = -1
    saw_up = False
    for bi in post_bis:
        if bi["direction"] == "down" and (anchor_price is None or bi["end_price"] < anchor_price):
            anchor_price = bi["end_price"]
            anchor_index = bi["end_index"]
            saw_up = False
            continue
        if anchor_price is None or bi["end_index"] <= anchor_index:
            continue
        if bi["direction"] == "up":
            saw_up = True
            continue
        if saw_up and bi["end_price"] > anchor_price:
            sig_date = bi.get("end_raw_date", bi["end_date"])
            if sig_date not in used_dates:
                signals.append({
                    "date": sig_date,
                    "type": "second_buy",
                    "price": bi["end_price"],
                    "first_buy_price": anchor_price,
                })
                used_dates.add(sig_date)
            return


def overlay_second_buys(df, signals):
    """只在空信号位写入二买，并把参照低点放到 chan_zd。"""
    out = df.copy()
    for sig in signals:
        date = sig["date"]
        if date not in out.index or out.loc[date, "chan_signal"] != 0:
            continue
        out.loc[date, "chan_signal"] = 2
        anchor = sig.get("first_buy_price", 0)
        if anchor:
            out.loc[date, "chan_zd"] = anchor
    return out


def latest_zhongshu_zd(analyzer, index):
    zd = pd.Series(0.0, index=index)
    for zs in analyzer.zhongshu_list:
        zd.loc[zd.index >= zs["end_date"]] = zs["ZD"]
    return zd


def _features_at(analyzer, frame: pd.DataFrame, sig: dict) -> Optional[dict]:
    """在一个二买上取结构特征和技术指标。样本不够或指标还没出来则返回 None。"""
    import talib

    sig_date = sig["date"]
    if sig_date not in frame.index:
        return None
    idx = frame.index.get_loc(sig_date)
    if isinstance(idx, slice) or not np.isscalar(idx):
        return None
    idx = int(idx)
    if idx < 30:
        return None

    high = frame["high"].values.astype(np.float64)
    low = frame["low"].values.astype(np.float64)
    close = frame["close"].values.astype(np.float64)
    volume = frame["volume"].values.astype(np.float64)
    atr = talib.ATR(high, low, close, timeperiod=14)
    adx = talib.ADX(high, low, close, timeperiod=14)
    rsi = talib.RSI(close, timeperiod=14)
    vol_ma = talib.SMA(volume, timeperiod=20)
    _, _, macd_hist = talib.MACD(close, fastperiod=12, slowperiod=26, signalperiod=9)

    price = close[idx]
    if price <= 0 or np.isnan(atr[idx]) or atr[idx] <= 0:
        return None
    if np.isnan(adx[idx]) or np.isnan(rsi[idx]):
        return None

    zs_height_ratio = 0.0
    zs_width = 0
    bi_in_zs = 0
    for zs in reversed(analyzer.zhongshu_list):
        if zs["end_date"] <= sig_date:
            zs_height_ratio = (zs["ZG"] - zs["ZD"]) / price
            zs_start_idx = frame.index.get_loc(zs["start_date"])
            zs_end_idx = frame.index.get_loc(zs["end_date"])
            if np.isscalar(zs_start_idx) and np.isscalar(zs_end_idx):
                zs_width = int(zs_end_idx) - int(zs_start_idx)
            bi_in_zs = zs.get("bi_count", 3)
            break

    bi_slope = 0.0
    anchor = sig.get("first_buy_price") or price
    pullback_depth = (price - anchor) / price if price else 0.0
    for bi in reversed(analyzer.bi_list):
        if bi["end_date"] <= sig_date and bi["direction"] == "down":
            bi_len = max((bi["end_date"] - bi["start_date"]).days, 1)
            if bi["start_price"]:
                bi_slope = (bi["end_price"] - bi["start_price"]) / bi["start_price"] / bi_len * 100
            break

    momentum_10d = close[idx] / close[idx - 10] - 1 if idx >= 10 else 0.0
    vol_ratio_val = volume[idx] / vol_ma[idx] if not np.isnan(vol_ma[idx]) and vol_ma[idx] > 0 else 1.0
    macd_val = macd_hist[idx] if not np.isnan(macd_hist[idx]) else 0.0
    return {
        "zs_height_ratio": zs_height_ratio,
        "zs_width": zs_width,
        "bi_in_zs": bi_in_zs,
        "bi_slope": bi_slope,
        "pullback_depth": pullback_depth,
        "atr_ratio": atr[idx] / price,
        "adx": adx[idx],
        "vol_ratio": vol_ratio_val,
        "rsi": rsi[idx],
        "macd_hist": macd_val / price * 100,
        "momentum_10d": momentum_10d,
    }


def _label_ready(frame: pd.DataFrame, idx: int, cutoff: pd.Timestamp) -> Optional[int]:
    """20 根之后的结果日必须早于截止日，标签才能进训练集。"""
    outcome_idx = idx + _LABEL_BARS
    if outcome_idx >= len(frame):
        return None
    if frame.index[outcome_idx] >= cutoff:
        return None
    price = float(frame["close"].iloc[idx])
    if price <= 0:
        return None
    future_max = float(frame["close"].iloc[idx + 1:idx + _LABEL_BARS + 1].max())
    return 1 if future_max / price - 1 > _LABEL_GAIN else 0


def _analyze(frame: pd.DataFrame):
    analyzer = _analyzer_cls()(frame)
    analyzer.analyze()
    signal_df = overlay_second_buys(analyzer.get_signal_df(), detect_second_buys(analyzer))
    return analyzer, signal_df


def _load_frame(code: str, start: str, end: str) -> Optional[pd.DataFrame]:
    from lib.backtest_data import load_daily_kline
    try:
        raw = load_daily_kline(code, start_date=start, end_date=end)
    except Exception as exc:
        print(f"[chan_buy2_ml] {code} 日 K 读取失败: {exc}", flush=True)
        return None
    if raw is None or raw.empty:
        return None
    frame = _prepare_frame(raw)
    if len(frame) < _MIN_BARS:
        return None
    return frame


def _collect(codes: list, start: str, cutoff: pd.Timestamp):
    rows = []
    labels = []
    end = cutoff.strftime("%Y-%m-%d")
    for code in codes:
        frame = _load_frame(code, start, end)
        if frame is None:
            continue
        try:
            analyzer, _ = _analyze(frame)
            signals = detect_second_buys(analyzer)
        except Exception as exc:
            print(f"[chan_buy2_ml] {code} 二买分析失败: {exc}", flush=True)
            continue
        kept = 0
        for sig in signals:
            if sig["date"] not in frame.index:
                continue
            idx = frame.index.get_loc(sig["date"])
            if not np.isscalar(idx):
                continue
            label = _label_ready(frame, int(idx), cutoff)
            feat = _features_at(analyzer, frame, sig)
            if label is None or feat is None:
                continue
            rows.append(feat)
            labels.append(label)
            kept += 1
        print(f"[chan_buy2_ml] {code} 可训练二买 {kept}", flush=True)
    if not rows:
        return pd.DataFrame(columns=_FEATURE_COLS), np.array([], dtype=int)
    return pd.DataFrame(rows, columns=_FEATURE_COLS), np.array(labels, dtype=int)


def _fit(features: pd.DataFrame, labels: np.ndarray):
    if len(features) < 3 or len(np.unique(labels)) < 2:
        return None
    import lightgbm as lgb
    model = lgb.LGBMClassifier(
        n_estimators=60, max_depth=3, learning_rate=0.1,
        min_child_samples=2, reg_alpha=0.1, reg_lambda=1.0,
        is_unbalance=True, verbose=-1, random_state=42,
    )
    model.fit(features[_FEATURE_COLS], labels)
    return model


def train_model(code: str, cutoff) -> Optional[object]:
    """按股票市场选课件里的训练集，截止日之前走完标签的二买用来拟合。"""
    cutoff = pd.Timestamp(cutoff).normalize()
    universe = "us" if str(code).upper().endswith(".US") else "cn"
    key = (universe, cutoff.strftime("%Y-%m-%d"))
    if key in _MODEL_CACHE:
        return _MODEL_CACHE[key]
    codes = _TRAIN_US if universe == "us" else _TRAIN_CN
    print(f"[chan_buy2_ml] 训练{universe} 截止 {key[1]}，股票 {len(codes)} 只", flush=True)
    features, labels = _collect(codes, _TRAIN_START[universe], cutoff)
    model = _fit(features, labels)
    ok = 0 if model is None else int(labels.sum())
    print(
        f"[chan_buy2_ml] 样本 {len(labels)}，20日涨超5% {ok}"
        + ("" if model is not None else "，样本不够未训练"),
        flush=True,
    )
    _MODEL_CACHE[key] = model
    return model


def _probs(analyzer, frame: pd.DataFrame, model) -> dict:
    if model is None:
        return {}
    out = {}
    for sig in detect_second_buys(analyzer):
        feat = _features_at(analyzer, frame, sig)
        if feat is None:
            continue
        row = pd.DataFrame([feat], columns=_FEATURE_COLS)
        prob = float(model.predict_proba(row)[0, 1])
        out[pd.Timestamp(sig["date"]).normalize()] = prob
    return out


def _stop_from_row(row, close: float) -> float:
    try:
        anchor = float(row.get("chan_zd"))
    except (TypeError, ValueError):
        anchor = 0.0
    if anchor > 0:
        return anchor
    return close * 0.93


def _last_action(signal_df: pd.DataFrame, zd: pd.Series, probs: dict) -> tuple:
    """确认 K 线收盘才允许买。离场规则与课件二买一致。"""
    in_pos = False
    stop = None
    highest = None
    armed = None
    action = "hold"
    reason = "无二买"
    prev_sig = 0
    prev_date = None

    for i, (dt, row) in enumerate(signal_df.iterrows()):
        action = "hold"
        reason = "无二买"
        close = float(row["close"])
        if armed is not None and not in_pos:
            in_pos = True
            stop = armed
            highest = close
            armed = None
        if in_pos:
            highest = max(highest or 0.0, close)
            floor = 0.0
            if len(zd):
                pos = zd.index.searchsorted(pd.Timestamp(dt).normalize(), side="right") - 1
                if pos >= 0:
                    floor = float(zd.iloc[pos])
            if stop and close < stop:
                action = "sell"
                reason = f"收盘 {close:.2f} 跌破参照低点 {stop:.2f}"
                in_pos = False
            elif prev_sig == -3:
                action = "sell"
                reason = "三卖已确认"
                in_pos = False
            elif floor > (stop or 0) and highest > floor and close < floor:
                action = "sell"
                reason = f"收盘 {close:.2f} 跌破中枢下沿 {floor:.2f}"
                in_pos = False
            elif highest and highest > (stop or 0) and close < highest * (1 - _TRAIL):
                action = "sell"
                reason = f"收盘较持仓后高点回撤超过 {_TRAIL:.0%}"
                in_pos = False
            if action == "sell":
                stop = None
                highest = None
        if action != "sell" and not in_pos and armed is None and prev_sig == 2:
            prob = float(probs.get(prev_date, 0.0))
            if prob >= _THRESHOLD:
                action = "buy"
                armed = _stop_from_row(signal_df.iloc[i - 1], float(signal_df.iloc[i - 1]["close"]))
                reason = f"二买确认，概率 {prob:.2f}，结构止损 {armed:.2f}"
            else:
                reason = f"二买确认，概率 {prob:.2f} 低于 {_THRESHOLD:.2f}"
        prev_sig = int(row["chan_signal"])
        prev_date = pd.Timestamp(dt).normalize()
    return action, reason


def evaluate(code: str, market, model=None) -> dict:
    try:
        df = market.get_recent_kline(code, period="1d", count=_BARS)
    except Exception:
        df = None
    if df is None or len(df) < _MIN_BARS:
        return {"side": "hold", "strategy": _NAME, "reason": "日 K 不足，缠论无法成笔"}
    frame = _prepare_frame(df)
    if len(frame) < _MIN_BARS:
        return {"side": "hold", "strategy": _NAME, "reason": "日 K 不足，缠论无法成笔"}
    if model is None and not getattr(market, "chan_buy2_ml_ready", False):
        model = train_model(code, frame.index[-1])
    elif model is None:
        model = getattr(market, "chan_buy2_ml_model", None)
    if model is None:
        return {"side": "hold", "strategy": _NAME, "reason": "二买训练样本不足，模型未建立"}
    analyzer, signal_df = _analyze(frame)
    probs = _probs(analyzer, frame, model)
    side, reason = _last_action(signal_df, latest_zhongshu_zd(analyzer, signal_df.index), probs)
    return {"side": side, "strategy": _NAME, "reason": reason}
