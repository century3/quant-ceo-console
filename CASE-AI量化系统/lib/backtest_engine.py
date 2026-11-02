# -*- coding: utf-8 -*-
# Author：@跟陈博士学AI
# 25-AI量化系统 回测引擎 -- Backtrader 撮合 + strategy_registry evaluator
"""
backtest_engine -- 历史日 K 上跑已注册策略, 算信号 + 撮合 + 指标

从 24 讲课件打印 CASE-AI量化系统/lib/backtest_engine.py copy 后改:
    缠论在 evaluator 里调用第 13 课 ChanAnalyzer, 不在回测前预计算
    第 11 讲机器学习在开跑前整段算一次涨概率, 避免每根 K 重新训练

策略 (code, market, capital) -> {side, strategy, reason},
模拟盘 / 实盘 / 回测共用同一 evaluator。

回测引擎是第 5 讲 Backtrader + 1b 撮合。
"""

from __future__ import annotations
import os
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import backtrader as bt
import pandas as pd

from lib.backtest_data import load_daily_kline, get_stock_name
from lib.backtest_metrics import compute_metrics
from lib.bt_ashare import AShareFillMixin, apply_friction
from lib.strategy_registry import get_strategy, list_strategies, prepare_backtest_market


class BtMarketAdapter:
    """策略调用 get_recent_kline / get_latest_tick, 只能看到当天及以前"""

    def __init__(self, code: str, daily_df: pd.DataFrame):
        self.code = code
        self._df = daily_df.copy()
        self._df.sort_index(inplace=True)
        self._strategy = None

    def bind(self, strategy) -> None:
        self._strategy = strategy

    def _asof(self) -> pd.Timestamp:
        d = self._strategy.data.datetime.date(0)
        return pd.Timestamp(d)

    def get_recent_kline(self, stock_code: str,
                         period: str = "1d",
                         count: int = 50):
        if stock_code != self.code:
            return None
        if period not in ("1d", "1day", "day", "daily"):
            return None
        hist = self._df.loc[self._df.index <= self._asof()]
        if hist.empty:
            return None
        return hist.tail(count)

    def get_latest_tick(self, stock_code: str) -> dict:
        hist = self._df.loc[self._df.index <= self._asof()]
        if hist.empty:
            return {}
        row = hist.iloc[-1]
        return {
            "lastPrice": float(row["close"]),
            "open":      float(row["open"]),
            "high":      float(row["high"]),
            "low":       float(row["low"]),
            "volume":    float(row["volume"]),
        }


class RegistryBridge(AShareFillMixin, bt.Strategy):
    params = (
        ('evaluator', None),
        ('strategy_name', ''),
        ('stock_code', ''),
        ('capital', 1_000_000.0),
        ('start_date', ''),
        ('end_date', ''),
        ('position_pct', 95),
        ('market', None),
        ('limit_pct', 0.10),
    )

    def __init__(self):
        self.pending = None
        self.buy_bar = None
        self._cost_basis = 0.0
        self._fill_reason = ""
        self.signals: List[Dict[str, Any]] = []
        self.trades: List[Dict[str, Any]] = []
        self.navs: List[Dict[str, Any]] = []
        self.p.market.bind(self)

    def _date_str(self) -> str:
        return self.data.datetime.date(0).strftime("%Y-%m-%d")

    def _in_range(self) -> bool:
        d = self._date_str()
        return self.p.start_date <= d <= self.p.end_date

    def _lot(self) -> int:
        return 1 if str(self.p.stock_code).upper().endswith(".US") else 100

    def _buy_size(self) -> int:
        price = float(self.data.open[0])
        lot = self._lot()
        if price <= 0:
            return 0
        budget = float(self.broker.getcash()) * (self.p.position_pct / 100.0)
        return (int(budget // price) // lot) * lot

    def notify_order(self, order):
        if order.status in (order.Rejected, order.Canceled, order.Margin):
            self._fill_reason = ""
            return
        if order.status != order.Completed:
            return
        date_str = self._date_str()
        size = abs(int(order.executed.size))
        price = float(order.executed.price)
        reason = self._fill_reason
        self._fill_reason = ""
        if order.isbuy():
            self.buy_bar = len(self)
            self._cost_basis = size * price + float(order.executed.comm)
            self.trades.append({
                "date": date_str, "side": "buy", "price": round(price, 4),
                "size": size, "pnl": 0.0, "reason": reason,
            })
            return
        revenue = size * price
        pnl = revenue - float(order.executed.comm) - self._cost_basis
        self.trades.append({
            "date": date_str, "side": "sell", "price": round(price, 4),
            "size": size, "pnl": round(pnl, 2), "reason": reason,
        })
        self._cost_basis = 0.0

    def next_open(self):
        """昨日收盘信号, 今日开盘撮合"""
        if not self._in_range() or self.pending is None or len(self) < 2:
            return
        side = self.pending["side"]
        reason = self.pending.get("reason", "")
        us = str(self.p.stock_code).upper().endswith(".US")
        if side == "buy" and not self.position:
            qty = self._buy_size()
            if qty >= self._lot() and self._can_buy():
                self._fill_reason = reason
                self.buy(size=qty)
        elif side == "sell" and self.position:
            blocked_t1 = (not us) and self.buy_bar is not None and len(self) <= self.buy_bar
            if (not blocked_t1) and self._can_sell():
                self._fill_reason = reason
                self.close()
        self.pending = None

    def next(self):
        """收盘跑 evaluator, 只记账"""
        if not self._in_range():
            return
        try:
            sig = self.p.evaluator(self.p.stock_code, self.p.market, self.p.capital)
        except Exception as e:
            sig = {"side": "hold", "strategy": self.p.strategy_name,
                   "reason": f"策略异常 {type(e).__name__}: {e}"}
        side = sig.get("side", "hold")
        if side in ("buy", "sell"):
            self.signals.append({
                "date": self._date_str(),
                "side": side,
                "reason": sig.get("reason", ""),
            })
            self.pending = {"side": side, "reason": sig.get("reason", "")}
        self.navs.append({
            "date": self._date_str(),
            "nav":  round(float(self.broker.getvalue()), 2),
        })


def run_backtest(stock_code: str,
                 strategy_name: str,
                 start_date: str,
                 end_date: str,
                 initial_cash: Optional[float] = None,
                 commission: Optional[float] = None,
                 position_pct: Optional[int] = None,
                 warmup_bars: int = 60) -> Dict[str, Any]:
    """单股 + 单策略回测, 返回字段与页面 / 模拟盘历史回放一致"""
    initial_cash = float(initial_cash if initial_cash is not None
                         else os.environ.get("BACKTEST_INITIAL_CASH", 1_000_000))
    commission = float(commission if commission is not None
                       else os.environ.get("BACKTEST_COMMISSION", 0.00015))
    position_pct = int(position_pct if position_pct is not None
                       else os.environ.get("BACKTEST_POSITION_PCT", 95))

    meta = get_strategy(strategy_name)
    if meta is None:
        return {"ok": False, "message": f"未知策略: {strategy_name}"}
    # 缠论要看到中枢形成前的笔，预热短了会把买点算没
    if strategy_name.startswith("chan_"):
        warmup_bars = max(int(warmup_bars), 500)
    # 机器学习要 120 日训练窗加上动量因子的回看
    elif strategy_name in ("ml_xgb", "ml_lgb"):
        warmup_bars = max(int(warmup_bars), 250)

    try:
        sd = datetime.strptime(start_date, "%Y-%m-%d") if start_date else None
        ed = datetime.strptime(end_date, "%Y-%m-%d") if end_date else None
        warmup_start = (sd - timedelta(days=int(warmup_bars * 1.7))).strftime("%Y-%m-%d") if sd else None
        full_df = load_daily_kline(stock_code,
                                    start_date=warmup_start or start_date,
                                    end_date=end_date)
    except Exception as e:
        return {"ok": False, "message": f"加载 {stock_code} 失败: {e}"}

    if full_df is None or len(full_df) < 30:
        return {"ok": False, "message": f"{stock_code} 数据不足 (<30 根)"}

    if sd is not None:
        bt_mask = full_df.index >= pd.Timestamp(sd)
    else:
        bt_mask = pd.Series(True, index=full_df.index)
    if ed is not None:
        bt_mask &= full_df.index <= pd.Timestamp(ed)
    bt_indices = [i for i, in_range in enumerate(bt_mask) if in_range]
    if not bt_indices:
        return {"ok": False, "message": f"撮合区间 {start_date}~{end_date} 内无数据"}

    feed_df = full_df.copy()
    feed_df["openinterest"] = 0.0

    is_us = stock_code.upper().endswith(".US")
    market = BtMarketAdapter(stock_code, full_df)
    prepare_backtest_market(market, strategy_name, full_df, stock_code, cutoff=start_date)
    cerebro = bt.Cerebro(stdstats=False, cheat_on_open=True)
    cerebro.addstrategy(
        RegistryBridge,
        evaluator=meta.evaluator,
        strategy_name=strategy_name,
        stock_code=stock_code,
        capital=initial_cash,
        start_date=start_date,
        end_date=end_date,
        position_pct=position_pct,
        market=market,
        limit_pct=0.0 if is_us else 0.10,
    )
    cerebro.adddata(bt.feeds.PandasData(dataname=feed_df))
    cerebro.broker.setcash(initial_cash)
    apply_friction(cerebro, slip_perc=0.001, commission=commission, us=is_us)

    result = cerebro.run()
    strat = result[0]
    trades = strat.trades
    navs = strat.navs
    signals = strat.signals

    bt_first_idx = bt_indices[0]
    bt_last_idx = bt_indices[-1]
    last_bar = full_df.iloc[bt_last_idx]
    if not navs:
        navs = [{"date": last_bar.name.strftime("%Y-%m-%d"),
                 "nav": round(float(cerebro.broker.getvalue()), 2)}]
    metrics = compute_metrics(initial_cash, trades, navs)

    bh_open = float(full_df.iloc[bt_first_idx]["open"])
    bh_close = float(full_df.iloc[bt_last_idx]["close"])
    bh_total = (bh_close - bh_open) / bh_open if bh_open > 0 else 0.0
    bh_years = max(metrics["years"], 1e-6)
    bh_annual = (1 + bh_total) ** (1 / bh_years) - 1 if bh_total > -1 else bh_total

    bt_slice = full_df.iloc[bt_first_idx:bt_last_idx + 1]
    kline_out = [
        {
            "date":   d.strftime("%Y-%m-%d"),
            "open":   round(float(r["open"]), 4),
            "high":   round(float(r["high"]), 4),
            "low":    round(float(r["low"]), 4),
            "close":  round(float(r["close"]), 4),
            "volume": int(r["volume"]) if not pd.isna(r["volume"]) else 0,
        }
        for d, r in bt_slice.iterrows()
    ]

    return {
        "ok":         True,
        "stock_code": stock_code,
        "stock_name": get_stock_name(stock_code),
        "strategy":   strategy_name,
        "strategy_label": meta.label,
        "params": {
            "start_date":  start_date,
            "end_date":    end_date,
            "initial_cash": initial_cash,
            "commission":  commission,
            "position_pct": position_pct,
            "engine":      "backtrader",
        },
        "kline":      kline_out,
        "signals":    signals,
        "trades":     trades,
        "navs":       navs,
        "metrics":    metrics,
        "buy_hold": {
            "total_return":  round(bh_total,  6),
            "annual_return": round(bh_annual, 6),
        },
    }


def score_strategies(stock_code: str,
                     start_date: str,
                     end_date: str,
                     candidates: Optional[List[str]] = None,
                     initial_cash: Optional[float] = None,
                     commission: Optional[float] = None,
                     position_pct: Optional[int] = None) -> Dict[str, Any]:
    """对一只股票跑多个策略, 输出横向对比"""
    if not candidates:
        candidates = [s["name"] for s in list_strategies()]

    summaries: List[Dict[str, Any]] = []
    failed: List[Dict[str, str]] = []
    nav_series: Dict[str, List[Dict[str, Any]]] = {}
    buy_hold_series: List[Dict[str, Any]] = []
    bh_filled = False
    for name in candidates:
        try:
            res = run_backtest(stock_code, name, start_date, end_date,
                               initial_cash=initial_cash,
                               commission=commission,
                               position_pct=position_pct)
        except Exception as e:
            failed.append({"strategy": name, "error": f"{type(e).__name__}: {e}"})
            continue
        if not res.get("ok"):
            failed.append({"strategy": name, "error": res.get("message", "?")})
            continue
        m = res["metrics"]
        summaries.append({
            "strategy":      name,
            "label":         res.get("strategy_label", name),
            "total_return":  m["total_return"],
            "annual_return": m["annual_return"],
            "max_drawdown": m["max_drawdown"],
            "sharpe":       m["sharpe_ratio"],
            "calmar":       m["calmar_ratio"],
            "win_rate":     m["win_rate"],
            "profit_factor":      m.get("profit_factor", 0),
            "profit_loss_ratio":  m.get("profit_loss_ratio", 0),
            "trades":             m["total_trades"],
            "won":                m.get("won_trades", 0),
            "lost":               m.get("lost_trades", 0),
            "final_value":        m.get("final_value", 0),
        })
        navs = res.get("navs") or []
        initial = m.get("initial_cash", 1) or 1
        nav_series[name] = [
            {"date": p["date"], "nav": round(p["nav"] / initial, 6)}
            for p in navs
        ]
        if not bh_filled:
            kline = res.get("kline") or []
            if kline:
                base_open = kline[0].get("open") or 1
                close_map = {b["date"]: b.get("close") for b in kline}
                buy_hold_series = [
                    {"date": p["date"],
                     "nav":  round((close_map.get(p["date"]) or base_open) / base_open, 6)}
                    for p in navs
                ]
                bh_filled = True

    ranked = sorted(summaries,
                    key=lambda s: (s["annual_return"], s["sharpe"]),
                    reverse=True)
    best = ranked[0] if ranked and ranked[0].get("trades", 0) > 0 else None

    return {
        "ok":         True,
        "stock_code": stock_code,
        "stock_name": get_stock_name(stock_code),
        "params": {
            "start_date":   start_date,
            "end_date":     end_date,
            "initial_cash": initial_cash,
            "commission":   commission,
            "position_pct": position_pct,
            "engine":       "backtrader",
        },
        "ranked":     ranked,
        "summaries":  summaries,
        "nav_series": nav_series,
        "buy_hold":   buy_hold_series,
        "failed":     failed,
        "best":       best,
    }
