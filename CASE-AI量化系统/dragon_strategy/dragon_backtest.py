# -*- coding: utf-8 -*-
# Author：@跟陈博士学AI
# CASE-C 龙头战法历史回测（基于 wucai_trade MySQL 库）
"""
DragonBacktest -- 用 wucai_trade 已落库的日 K 数据，对 dragon_picker 做 T+1 回测

口径设计（A 股 T+1，不能日内平仓）：
    每个交易日 T:
      1. 读全市场 T 与 T-1 的日 K, 拼出 dragon_picker 需要的字段:
           day_change_pct = close_T / close_(T-1) - 1
           volume_ratio   = volume_T / mean(volume_(T-5..T-1))
           price          = close_T
           float_market_cap = float_shares * close_T (来自 trade_stock_status)
      2. 跑 filter_dragon_candidates(...) + calc_dragon_score
      3. 按分数取 Top K, 模拟次日开盘买入(T+1.open), T+H 收盘卖出(T+H.close)
      4. 单笔收益 = close_(T+H) / open_(T+1) - 1
    汇总:
      - 总样本数 / 胜率 / 平均收益 / 中位收益
      - 等权累计净值曲线（按 T+1 入场日聚合）-> 年化、最大回撤、Sharpe
      - 按板块（sector_2 或 sector_1）汇总
      - 按持有天数 H 网格扫描

为什么这么写:
    - Ross Cameron 当日清仓在 A 股不成立, 改成 T+1 持有 H 日（H ∈ {1, 3, 5}）
    - 流通市值用 close * float_shares 近似（status 表里 float_shares 单位为股）
    - 完全离线、读 MySQL，无需 xtdata; 课堂演示与学员复现都简单

用法（在 CASE 根目录, 已配置 .env 中 WUCAI_SQL_*）:
    python dragon_strategy/dragon_backtest.py --start 2025-01-01 --end 2025-06-30
    python dragon_strategy/dragon_backtest.py --start 2025-01-01 --end 2025-06-30 --top 5 --hold 1,3,5
"""
from __future__ import annotations

import argparse
import math
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS_DIR))

from db_config import execute_query
from dragon_picker import (
    calc_dragon_score,
    filter_dragon_candidates,
    SECTOR_MIN_CHANGE_PCT,
    SECTOR_MIN_RISE_RATIO,
)


# ============================================================
# 数据读取
# ============================================================

def load_trade_dates(start: str, end: str) -> List[date]:
    """读区间内所有交易日（按 trade_stock_daily 出现的日期为准, 避免节假日）"""
    rows = execute_query(
        """
        SELECT DISTINCT trade_date
        FROM trade_stock_daily
        WHERE trade_date BETWEEN %s AND %s
        ORDER BY trade_date
        """,
        (start, end),
    )
    return [r["trade_date"] for r in rows]


def load_status_meta() -> pd.DataFrame:
    """股票元信息: 名字 / 板块 / 流通股本 / 上市日期 (后者 v2 用于次新股过滤)"""
    rows = execute_query(
        """
        SELECT stock_code, stock_name, sector_1, sector_2, float_shares, list_date
        FROM trade_stock_status
        """
    )
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows).set_index("stock_code")
    df["float_shares"] = pd.to_numeric(df["float_shares"], errors="coerce")
    df["list_date"] = pd.to_datetime(df["list_date"], errors="coerce")
    return df


def load_sector_panel(start: str, end: str, sector_level: int = 2) -> pd.DataFrame:
    """
    v2 新增: 取区间内 trade_sector_daily 全量, 一次拉到内存
    返回长表: sector_name / trade_date / change_pct / rise_count / stock_count / rise_ratio
    """
    rows = execute_query(
        """
        SELECT sector_name, trade_date, change_pct, rise_count, stock_count
        FROM trade_sector_daily
        WHERE trade_date BETWEEN %s AND %s AND sector_level = %s
        """,
        (start, end, sector_level),
    )
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    # 板块涨幅: 表里存的是百分数 (例 2.34 表示 +2.34%), 统一成小数 0.0234
    df["change_pct"] = pd.to_numeric(df["change_pct"], errors="coerce") / 100.0
    df["rise_count"] = pd.to_numeric(df["rise_count"], errors="coerce")
    df["stock_count"] = pd.to_numeric(df["stock_count"], errors="coerce")
    df["rise_ratio"] = (df["rise_count"] / df["stock_count"]).clip(0, 1).fillna(0)
    return df.set_index(["sector_name", "trade_date"])


def load_daily_panel(start: str, end: str) -> pd.DataFrame:
    """
    取区间内所有日 K, 一次拉到内存。
    返回长表: stock_code / trade_date / open / high / low / close / volume / amount
    """
    rows = execute_query(
        """
        SELECT stock_code, trade_date,
               open_price  AS open, high_price AS high,
               low_price   AS low,  close_price AS close,
               volume, amount
        FROM trade_stock_daily
        WHERE trade_date BETWEEN %s AND %s
        """,
        (start, end),
    )
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    for c in ("open", "high", "low", "close", "amount"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0)
    return df


# ============================================================
# 单日候选 + 信号
# ============================================================

def build_today_candidates(
    panel: pd.DataFrame,
    meta: pd.DataFrame,
    sector_panel: pd.DataFrame,
    t: pd.Timestamp,
    min_change: float = 0.05,
    max_change: float = 0.095,
    max_price: float = 30.0,
    min_vol_ratio: float = 2.0,
    mcap_range: Tuple[float, float] = (30e8, 500e8),
    min_listed_days: int = 60,
    require_sector_resonance: bool = True,
    top_k: int = 5,
) -> List[Dict[str, Any]]:
    """
    给一个交易日 T, 拼候选股 dict 列表 (含 v2 板块共振字段 + 上市天数),
    然后调 dragon_picker.filter_dragon_candidates 做统一筛选 + 打分.

    v2 改动:
        - join trade_sector_daily 给每只股注入 sector_change_pct + sector_rise_ratio
        - 用 list_date 计算 listed_days 排除次新股
        - 不再在这里写硬编码过滤, 全部交给 filter_dragon_candidates
    """
    today = panel[panel["trade_date"] == t]
    if today.empty:
        return []

    # 取 T 前 6 个交易日窗口算 5 日均量与 T-1 close
    window = panel[(panel["trade_date"] < t)].copy()
    if window.empty:
        return []
    last_dates = sorted(window["trade_date"].unique())[-6:]
    window = window[window["trade_date"].isin(last_dates)]

    # 5 日均量 (T-5..T-1)
    vol5 = window.groupby("stock_code")["volume"].mean()
    # 前一日 close
    prev_close = (
        window.sort_values("trade_date").groupby("stock_code")["close"].last()
    )

    raw: List[Dict[str, Any]] = []
    for row in today.itertuples():
        code = row.stock_code
        close_t = float(row.close or 0)
        vol_t = float(row.volume or 0)
        if close_t <= 0:
            continue
        pc = float(prev_close.get(code, 0) or 0)
        if pc <= 0:
            continue
        chg = close_t / pc - 1.0
        avg_vol = float(vol5.get(code, 0) or 0)
        vr = vol_t / avg_vol if avg_vol > 0 else 0.0

        if code not in meta.index:
            continue
        meta_row = meta.loc[code]
        name = str(meta_row.get("stock_name") or "")
        sector_2 = str(meta_row.get("sector_2") or "")
        sector_1 = str(meta_row.get("sector_1") or "")
        fshare = float(meta_row.get("float_shares") or 0)
        mcap = fshare * close_t

        # v2: 上市天数 (按自然日近似, filter 用 60 个交易日 ≈ 90 自然日, 这里给保守 90)
        list_date = meta_row.get("list_date")
        if pd.notna(list_date):
            listed_days = int((t - list_date).days)
        else:
            listed_days = None

        # v2: 板块共振字段 (sector_2 当日表现)
        s_chg, s_rise = None, None
        if sector_2 and not sector_panel.empty:
            try:
                sec_row = sector_panel.loc[(sector_2, t)]
                s_chg = float(sec_row["change_pct"]) if pd.notna(sec_row["change_pct"]) else None
                s_rise = float(sec_row["rise_ratio"]) if pd.notna(sec_row["rise_ratio"]) else None
            except KeyError:
                s_chg, s_rise = None, None

        raw.append({
            "code":              code,
            "name":              name,
            "sector_1":          sector_1,
            "sector_2":          sector_2,
            "day_change_pct":    chg,
            "price":             close_t,
            "volume_ratio":      vr,
            "float_market_cap":  mcap,
            "listed_days":       listed_days,
            "sector_change_pct": s_chg,
            "sector_rise_ratio": s_rise,
        })

    if not raw:
        return []

    # 全部硬规则 + 打分都交给 dragon_picker, 保证 picker 与 backtest 口径一致
    cands = filter_dragon_candidates(
        raw,
        min_change=min_change,
        max_price=max_price,
        mcap_range=mcap_range,
        min_volume_ratio=min_vol_ratio,
        require_sector_resonance=require_sector_resonance,
        max_change=max_change,
        min_listed_days=min_listed_days,
    )
    return cands[:top_k]


# ============================================================
# 模拟交易（T+1 持有 H 日）
# ============================================================

def simulate_trades(
    panel: pd.DataFrame,
    picks_per_day: Dict[pd.Timestamp, List[Dict[str, Any]]],
    trade_dates: List[pd.Timestamp],
    hold_days: int,
) -> pd.DataFrame:
    """
    对每个 (T, code), 在 T+1.open 买入, T+hold.close 卖出。
    返回 DataFrame, 一行一笔交易。
    """
    date_index = {d: i for i, d in enumerate(trade_dates)}

    # 个股日 K 索引: (code, date) -> open/close
    p = panel.set_index(["stock_code", "trade_date"])[["open", "close"]]
    rows = []
    for t, picks in picks_per_day.items():
        i = date_index.get(t)
        if i is None or i + hold_days >= len(trade_dates):
            continue
        t_buy = trade_dates[i + 1]
        t_sell = trade_dates[i + hold_days]
        for c in picks:
            code = c["code"]
            try:
                buy_open = float(p.loc[(code, t_buy), "open"])
                sell_close = float(p.loc[(code, t_sell), "close"])
            except KeyError:
                continue
            if buy_open <= 0:
                continue
            ret = sell_close / buy_open - 1.0
            rows.append({
                "signal_date":   t,
                "buy_date":      t_buy,
                "sell_date":     t_sell,
                "code":          code,
                "name":          c.get("name", ""),
                "sector_2":      c.get("sector_2", ""),
                "sector_1":      c.get("sector_1", ""),
                "buy_open":      round(buy_open, 4),
                "sell_close":    round(sell_close, 4),
                "ret":           round(ret, 4),
                "score":         round(c.get("dragon_score", 0), 3),
                "day_change":    round(c.get("day_change_pct", 0), 4),
                "vol_ratio":     round(c.get("volume_ratio", 0), 2),
                # v2 新增字段, 复盘时看共振有没有起到作用
                "sector_chg":    round(c.get("sector_change_pct") or 0, 4),
                "sector_rise":   round(c.get("sector_rise_ratio") or 0, 3),
            })
    return pd.DataFrame(rows)


# ============================================================
# 汇总
# ============================================================

def summarize(trades: pd.DataFrame, label: str = "") -> Dict[str, Any]:
    if trades.empty:
        return {"label": label, "n": 0}
    n = len(trades)
    win = (trades["ret"] > 0).sum()
    return {
        "label":    label,
        "n":        int(n),
        "win_rate": round(win / n, 4),
        "avg_ret":  round(trades["ret"].mean(), 4),
        "median":   round(trades["ret"].median(), 4),
        "best":     round(trades["ret"].max(), 4),
        "worst":    round(trades["ret"].min(), 4),
    }


def equity_curve(trades: pd.DataFrame) -> pd.DataFrame:
    """按 buy_date 等权聚合: 当天若有 K 笔, 取均值；按交易日累乘成净值"""
    if trades.empty:
        return pd.DataFrame(columns=["buy_date", "daily_ret", "nav"])
    daily = trades.groupby("buy_date")["ret"].mean().sort_index()
    nav = (1.0 + daily).cumprod()
    return pd.DataFrame({"buy_date": daily.index, "daily_ret": daily.values, "nav": nav.values})


def perf_metrics(curve: pd.DataFrame) -> Dict[str, Any]:
    if curve.empty:
        return {}
    r = curve["daily_ret"].values
    nav = curve["nav"].values
    days = len(curve)
    ann_factor = 252 / max(days, 1)
    cum = float(nav[-1] - 1)
    ann = (1 + cum) ** ann_factor - 1
    sharpe = (r.mean() / (r.std() + 1e-9)) * math.sqrt(252) if days > 1 else 0
    # 最大回撤
    peak = np.maximum.accumulate(nav)
    dd = nav / peak - 1
    mdd = float(dd.min()) if days > 0 else 0.0
    return {
        "trade_days": days,
        "cum_return": round(cum, 4),
        "annualized": round(float(ann), 4),
        "sharpe":     round(float(sharpe), 3),
        "max_drawdown": round(mdd, 4),
    }


def by_sector(trades: pd.DataFrame, level: int = 2, top_n: int = 10) -> pd.DataFrame:
    if trades.empty:
        return pd.DataFrame()
    col = "sector_2" if level == 2 else "sector_1"
    g = trades.groupby(col).agg(
        n=("ret", "size"),
        win_rate=("ret", lambda x: float((x > 0).mean())),
        avg_ret=("ret", "mean"),
    ).sort_values("n", ascending=False).head(top_n)
    g["win_rate"] = g["win_rate"].round(4)
    g["avg_ret"] = g["avg_ret"].round(4)
    return g.reset_index()


# ============================================================
# 入口
# ============================================================

def run(
    start: str,
    end: str,
    holds: List[int],
    top_k: int,
    min_change: float,
    max_change: float,
    max_price: float,
    min_vol_ratio: float,
    mcap_low: float,
    mcap_high: float,
    min_listed_days: int,
    require_sector_resonance: bool,
    sector_level: int,
    out_dir: Optional[Path] = None,
) -> None:
    print("\n" + "#" * 70)
    print(f"# CASE-C 龙头战法回测  {start} ~ {end}")
    print(f"# Top {top_k} / hold {holds} / min_change {min_change} / max_change {max_change}")
    print(f"# max_price {max_price} / vol_ratio>={min_vol_ratio} "
          f"/ mcap [{mcap_low/1e8:.0f}-{mcap_high/1e8:.0f}]亿")
    print(f"# 上市天数>={min_listed_days}  板块共振={'开' if require_sector_resonance else '关'} "
          f"(sector_chg>={SECTOR_MIN_CHANGE_PCT:.1%}, rise_ratio>={SECTOR_MIN_RISE_RATIO:.0%})")
    print("#" * 70)

    print("\n[1] 读市场元信息（股票名/板块/流通股本/上市日期）...")
    meta = load_status_meta()
    if meta.empty:
        print("  [ERROR] trade_stock_status 为空, 请先跑 21-CASE-A 同步数据")
        return
    print(f"  共 {len(meta)} 只股票")

    print(f"\n[2] 读日 K 面板 {start} ~ {end} ...")
    panel = load_daily_panel(start, end)
    if panel.empty:
        print("  [ERROR] trade_stock_daily 区间为空, 请确认 21-CASE-A 数据范围")
        return
    print(f"  共 {len(panel):,} 行")

    print(f"\n[2.1] 读板块面板 trade_sector_daily (sector_level={sector_level}) ...")
    sector_panel = load_sector_panel(start, end, sector_level=sector_level)
    if sector_panel.empty:
        print("  [ERROR] trade_sector_daily 区间为空, 请先跑 10-板块行情采集.py")
        return
    print(f"  共 {len(sector_panel):,} 行 / "
          f"{sector_panel.index.get_level_values(0).nunique()} 个板块")

    trade_dates = sorted(panel["trade_date"].unique())
    print(f"  交易日: {len(trade_dates)} 天 ({trade_dates[0].date()} -> {trade_dates[-1].date()})")

    print(f"\n[3] 逐日生成龙头候选 ...")
    picks_per_day: Dict[pd.Timestamp, List[Dict[str, Any]]] = {}
    pick_count = 0
    for i, t in enumerate(trade_dates):
        # 至少要有 6 个交易日的历史窗口
        if i < 6:
            continue
        cands = build_today_candidates(
            panel, meta, sector_panel, t,
            min_change=min_change,
            max_change=max_change,
            max_price=max_price,
            min_vol_ratio=min_vol_ratio,
            mcap_range=(mcap_low, mcap_high),
            min_listed_days=min_listed_days,
            require_sector_resonance=require_sector_resonance,
            top_k=top_k,
        )
        if cands:
            picks_per_day[t] = cands
            pick_count += len(cands)
    print(f"  生成候选 {len(picks_per_day)} 天, 共 {pick_count} 笔信号")

    if not picks_per_day:
        print("  [INFO] 该参数下无任何候选, 终止")
        return

    out_dir = out_dir or (THIS_DIR.parent / "outputs")
    out_dir.mkdir(parents=True, exist_ok=True)

    for hold in holds:
        print("\n" + "=" * 70)
        print(f"  持有 H = {hold} 日 (T+1.open 买, T+{hold}.close 卖)")
        print("=" * 70)
        trades = simulate_trades(panel, picks_per_day, trade_dates, hold)
        if trades.empty:
            print("  无成交")
            continue

        s = summarize(trades, label=f"H={hold}")
        print(f"  样本: {s['n']} 笔   胜率 {s['win_rate']:.2%}   均收 {s['avg_ret']:+.2%}   "
              f"中位 {s['median']:+.2%}   最优 {s['best']:+.2%}   最差 {s['worst']:+.2%}")

        curve = equity_curve(trades)
        m = perf_metrics(curve)
        if m:
            print(f"  净值曲线: {m['trade_days']} 个买入日   "
                  f"累计 {m['cum_return']:+.2%}   "
                  f"年化 {m['annualized']:+.2%}   "
                  f"Sharpe {m['sharpe']}   MDD {m['max_drawdown']:+.2%}")

        sec = by_sector(trades, level=sector_level, top_n=10)
        if not sec.empty:
            col = "sector_2" if sector_level == 2 else "sector_1"
            print(f"\n  按 {col} 汇总 (前 10):")
            for r in sec.itertuples():
                print(f"    {getattr(r, col):<14s}  n={r.n:>3}  "
                      f"胜率 {r.win_rate:.2%}  均收 {r.avg_ret:+.2%}")

        # 落盘
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        trades_path = out_dir / f"dragon_trades_H{hold}_{ts}.csv"
        curve_path = out_dir / f"dragon_curve_H{hold}_{ts}.csv"
        trades.to_csv(trades_path, index=False, encoding="utf-8-sig")
        curve.to_csv(curve_path, index=False, encoding="utf-8-sig")
        print(f"\n  CSV: {trades_path.name}, {curve_path.name}  -> {out_dir}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="CASE-C 龙头战法 T+1 历史回测（读 wucai_trade.*）"
    )
    parser.add_argument("--start", required=True, help="起始日 YYYY-MM-DD")
    parser.add_argument("--end",   required=True, help="结束日 YYYY-MM-DD")
    parser.add_argument("--top",   type=int, default=5, help="每日 Top K 候选, 默认 5")
    parser.add_argument("--hold",  type=str, default="1,3,5",
                        help="持有天数列表, 逗号分隔, 默认 1,3,5")
    parser.add_argument("--min-change",   type=float, default=0.05)
    parser.add_argument("--max-change",   type=float, default=0.095,
                        help="v2: 涨幅上限, 默认 9.5% (排除涨停板, 避免 T+1 高开污染)")
    parser.add_argument("--max-price",    type=float, default=30.0)
    parser.add_argument("--min-vol-ratio", type=float, default=2.0)
    parser.add_argument("--mcap-low",     type=float, default=30e8)
    parser.add_argument("--mcap-high",    type=float, default=500e8)
    parser.add_argument("--min-listed-days", type=int, default=60,
                        help="v2: 上市天数下限, 默认 60 个交易日 (排除次新股)")
    parser.add_argument("--no-sector-resonance", action="store_true",
                        help="v2: 关闭板块共振硬过滤 (用于做 v1 vs v2 对照)")
    parser.add_argument("--sector-level", type=int, choices=[1, 2], default=2)
    args = parser.parse_args()

    holds = [int(x) for x in args.hold.split(",") if x.strip()]
    run(
        start=args.start, end=args.end,
        holds=holds, top_k=args.top,
        min_change=args.min_change, max_change=args.max_change,
        max_price=args.max_price,
        min_vol_ratio=args.min_vol_ratio,
        mcap_low=args.mcap_low, mcap_high=args.mcap_high,
        min_listed_days=args.min_listed_days,
        require_sector_resonance=not args.no_sector_resonance,
        sector_level=args.sector_level,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
