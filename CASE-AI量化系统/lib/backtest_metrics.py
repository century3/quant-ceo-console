# -*- coding: utf-8 -*-
# Author：@跟陈博士学AI
# 25-AI量化系统 回测绩效指标计算
"""
backtest_metrics -- 不依赖 backtrader 的指标计算

参考 QuantStats/data_loader._calc_metrics 思路，改写为纯 Python：
    - 从交易序列 trades 算: 胜率 / 盈亏比 / 利润因子 / 期望值 / 最大连亏
    - 从净值序列 navs 算:  最大回撤 + 持续天数 / 年化 / 夏普 / 卡玛

用法:
    metrics = compute_metrics(
        initial_cash=1_000_000,
        trades=[{"date": ..., "side": "buy"/"sell", "price": ..., "size": ..., "pnl": ...}, ...],
        navs=[{"date": ..., "nav": float}, ...],
    )
    -> {"total_return", "annual_return", "max_drawdown", ..., "win_rate", "profit_loss_ratio", ...}
"""

from __future__ import annotations
import math
from typing import List, Dict, Any


# ============================================================
# 指标计算
# ============================================================

def compute_metrics(initial_cash: float,
                    trades: List[Dict[str, Any]],
                    navs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """计算完整绩效指标

    Args:
        initial_cash: 初始资金
        trades: 交易明细 (单笔). 必须含 'side' / 'pnl' (sell 时填实现盈亏, buy 时 0)
        navs:   每日净值 [{"date": "YYYY-MM-DD", "nav": float}]

    Returns: 指标字典 (浮点已 round 到合理精度)
    """
    if initial_cash <= 0:
        return _empty_metrics()

    nav_values = [float(x["nav"]) for x in navs] if navs else [initial_cash]
    final_value = nav_values[-1]
    total_return = (final_value - initial_cash) / initial_cash

    trading_days = len(navs)
    years = trading_days / 252.0 if trading_days > 0 else 0.0
    if years > 0 and total_return > -1:
        annual_return = (1.0 + total_return) ** (1.0 / years) - 1.0
    else:
        annual_return = total_return

    # 最大回撤 + 持续天数 (按净值序列扫一遍)
    max_dd = 0.0
    max_dd_len = 0
    if nav_values:
        peak = nav_values[0]
        dd_len = 0
        cur_max_dd_len = 0
        for v in nav_values:
            if v > peak:
                peak = v
                cur_max_dd_len = 0
            else:
                cur_max_dd_len += 1
                if peak > 0:
                    dd_pct = (peak - v) / peak
                    if dd_pct > max_dd:
                        max_dd = min(dd_pct, 1.0)
                if cur_max_dd_len > max_dd_len:
                    max_dd_len = cur_max_dd_len
            dd_len = cur_max_dd_len

    # 夏普 (日收益率序列, 按 252 年化, rf=2%)
    sharpe = 0.0
    if len(nav_values) >= 2:
        rets = []
        for i in range(1, len(nav_values)):
            prev = nav_values[i - 1]
            if prev > 0:
                rets.append(nav_values[i] / prev - 1.0)
        if rets:
            mean_r = sum(rets) / len(rets)
            var = sum((r - mean_r) ** 2 for r in rets) / len(rets)
            std = math.sqrt(var)
            rf_daily = 0.02 / 252.0
            if std > 1e-9:
                sharpe = (mean_r - rf_daily) / std * math.sqrt(252)

    calmar = (annual_return / max_dd) if max_dd > 0 else 0.0

    # 交易侧指标 -- 用"卖出 trade.pnl"作为单笔已实现盈亏
    sell_trades = [t for t in (trades or []) if t.get("side") == "sell" and "pnl" in t]
    total_trades = len(sell_trades)
    won = [t for t in sell_trades if float(t.get("pnl", 0)) > 0]
    lost = [t for t in sell_trades if float(t.get("pnl", 0)) <= 0]
    won_n = len(won)
    lost_n = len(lost)
    win_rate = (won_n / total_trades) if total_trades > 0 else 0.0
    avg_win = (sum(float(t["pnl"]) for t in won) / won_n) if won_n > 0 else 0.0
    avg_loss = (sum(float(t["pnl"]) for t in lost) / lost_n) if lost_n > 0 else 0.0
    profit_loss_ratio = abs(avg_win / avg_loss) if avg_loss != 0 else 0.0
    gross_profit = sum(float(t["pnl"]) for t in won)
    gross_loss = sum(float(t["pnl"]) for t in lost)
    profit_factor = abs(gross_profit / gross_loss) if gross_loss != 0 else 0.0

    # 最大连续亏损次数
    max_consec_loss = 0
    cur_streak = 0
    for t in sell_trades:
        if float(t.get("pnl", 0)) <= 0:
            cur_streak += 1
            if cur_streak > max_consec_loss:
                max_consec_loss = cur_streak
        else:
            cur_streak = 0

    expected_value = (win_rate * avg_win + (1.0 - win_rate) * avg_loss) if total_trades > 0 else 0.0

    return {
        "initial_cash":        round(initial_cash, 2),
        "final_value":         round(final_value, 2),
        "total_return":        round(total_return, 6),
        "annual_return":       round(annual_return, 6),
        "max_drawdown":        round(max_dd, 6),
        "max_dd_len":          int(max_dd_len),
        "sharpe_ratio":        round(sharpe, 4),
        "calmar_ratio":        round(calmar, 4),
        "total_trades":        int(total_trades),
        "won_trades":          int(won_n),
        "lost_trades":         int(lost_n),
        "win_rate":            round(win_rate, 4),
        "avg_win":             round(avg_win, 2),
        "avg_loss":            round(avg_loss, 2),
        "profit_loss_ratio":   round(profit_loss_ratio, 2),
        "profit_factor":       round(profit_factor, 2),
        "max_consecutive_losses": int(max_consec_loss),
        "expected_value":      round(expected_value, 2),
        "years":               round(years, 2),
        "trading_days":        int(trading_days),
    }


def _empty_metrics() -> Dict[str, Any]:
    """空结果 (用于异常路径)"""
    return {
        "initial_cash": 0, "final_value": 0,
        "total_return": 0, "annual_return": 0,
        "max_drawdown": 0, "max_dd_len": 0,
        "sharpe_ratio": 0, "calmar_ratio": 0,
        "total_trades": 0, "won_trades": 0, "lost_trades": 0,
        "win_rate": 0, "avg_win": 0, "avg_loss": 0,
        "profit_loss_ratio": 0, "profit_factor": 0,
        "max_consecutive_losses": 0, "expected_value": 0,
        "years": 0, "trading_days": 0,
    }


# ============================================================
# 推荐策略评分: 把多策略指标摊平后排序
# ============================================================

def rank_strategies(per_strategy_metrics: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """对多策略指标排个名, 推荐"最适合该股的策略"

    评分思路 (简单加权, 教学场景够用; 想搞复杂可换 IC/IR):
        score = 0.4 * 夏普_norm + 0.3 * 卡玛_norm + 0.3 * 胜率_norm

    Returns: 按 score 降序排好的列表
    """
    rows = []
    for name, m in (per_strategy_metrics or {}).items():
        rows.append({
            "strategy":      name,
            "total_return":  m.get("total_return", 0),
            "annual_return": m.get("annual_return", 0),
            "max_drawdown":  m.get("max_drawdown", 0),
            "sharpe":        m.get("sharpe_ratio", 0),
            "calmar":        m.get("calmar_ratio", 0),
            "win_rate":      m.get("win_rate", 0),
            "trades":        m.get("total_trades", 0),
        })
    if not rows:
        return []

    # 归一化 (min-max), 防 0
    def _norm(values):
        vmin, vmax = min(values), max(values)
        rng = vmax - vmin
        if rng < 1e-9:
            return [0.5] * len(values)
        return [(v - vmin) / rng for v in values]

    sharpes = _norm([r["sharpe"]   for r in rows])
    calmars = _norm([r["calmar"]   for r in rows])
    wins    = _norm([r["win_rate"] for r in rows])
    for r, s, c, w in zip(rows, sharpes, calmars, wins):
        # 没有交易过的策略给 0 分 (不能推荐一个完全没动手的)
        if r["trades"] == 0:
            r["score"] = 0.0
        else:
            r["score"] = round(0.4 * s + 0.3 * c + 0.3 * w, 4)

    rows.sort(key=lambda x: x["score"], reverse=True)
    return rows
