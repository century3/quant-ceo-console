# -*- coding: utf-8 -*-
# Author：@跟陈博士学AI
# CASE-C 单标的复盘 -- 真实数据教学用
"""
DragonReplayOne -- 给定一只股 + 一个时间窗口, 逐日打印"它今天有没有被龙头战法选中"
                  以及"如果选中, T+1 开盘买持 H 日真实收益是多少"

用途:
    课堂演示: 选一只历史上的明星票 (例如 2024 年的某只半导体/锂电龙头),
    放真实回测画面给学员看 -- 这只票在什么日子触发了 dragon_picker,
    随后几天真实涨跌如何, 比看抽象的胜率/MDD 直观得多

口径:
    每个交易日 T (从 start 到 end):
        - 拼出 day_change_pct / volume_ratio / float_market_cap / sector_*
        - 跑 v1 (关共振) 和 v2 (默认) 两套, 看看哪一套放行
        - 如果放行, 用 T+1.open 买入, T+H.close 卖出, 算真实收益
        - 终端按行打印, 同时写一份 CSV 到 outputs/

用法:
    python dragon_strategy\\dragon_replay_one.py --code 300750.SZ --start 2024-01-01 --end 2024-12-31
    python dragon_strategy\\dragon_replay_one.py --code 002241.SZ --start 2024-06-01 --end 2024-09-30 --hold 3
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any

import pandas as pd

THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS_DIR))

from db_config import execute_query
from dragon_picker import (
    calc_dragon_score,
    passes_sector_resonance,
    SECTOR_MIN_CHANGE_PCT,
    SECTOR_MIN_RISE_RATIO,
)


# ============================================================
# 数据读取 (单只股, 区间)
# ============================================================

def load_meta(code: str) -> Dict[str, Any]:
    rows = execute_query(
        "SELECT stock_code, stock_name, sector_1, sector_2, float_shares, list_date "
        "FROM trade_stock_status WHERE stock_code=%s",
        (code,),
    )
    if not rows:
        raise SystemExit(f"[ERROR] trade_stock_status 找不到 {code}")
    r = rows[0]
    return {
        "code":          r["stock_code"],
        "name":          r["stock_name"] or "",
        "sector_1":      r["sector_1"] or "",
        "sector_2":      r["sector_2"] or "",
        "float_shares":  float(r["float_shares"] or 0),
        "list_date":     r["list_date"],
    }


def load_one_stock_daily(code: str, start: str, end: str) -> pd.DataFrame:
    """单只股 (含前置 7 个交易日, 算 5 日均量与 T-1 close 用)"""
    rows = execute_query(
        """
        SELECT trade_date,
               open_price  AS open, high_price AS high,
               low_price   AS low,  close_price AS close,
               volume, amount
        FROM trade_stock_daily
        WHERE stock_code=%s
          AND trade_date BETWEEN DATE_SUB(%s, INTERVAL 14 DAY) AND %s
        ORDER BY trade_date
        """,
        (code, start, end),
    )
    if not rows:
        raise SystemExit(f"[ERROR] trade_stock_daily 找不到 {code} 在 {start}~{end}")
    df = pd.DataFrame(rows)
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    for c in ("open", "high", "low", "close", "amount"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0)
    return df.reset_index(drop=True)


def load_sector_series(sector_2: str, start: str, end: str) -> pd.DataFrame:
    """读这只股所在 sector_2 在区间内的日线 (含 stock_count, rise_count 算 rise_ratio)"""
    rows = execute_query(
        """
        SELECT trade_date, change_pct, rise_count, stock_count
        FROM trade_sector_daily
        WHERE sector_name=%s AND sector_level=2
          AND trade_date BETWEEN %s AND %s
        ORDER BY trade_date
        """,
        (sector_2, start, end),
    )
    if not rows:
        return pd.DataFrame(columns=["trade_date", "change_pct", "rise_ratio"])
    df = pd.DataFrame(rows)
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    df["change_pct"] = pd.to_numeric(df["change_pct"], errors="coerce") / 100.0
    df["rise_count"] = pd.to_numeric(df["rise_count"], errors="coerce")
    df["stock_count"] = pd.to_numeric(df["stock_count"], errors="coerce")
    df["rise_ratio"] = (df["rise_count"] / df["stock_count"]).clip(0, 1).fillna(0)
    return df.set_index("trade_date")


# ============================================================
# 单只股逐日复盘
# ============================================================

def run_replay(code: str, start: str, end: str, hold: int,
               max_price: float = 30.0,
               mcap_low: float = 30e8,
               mcap_high: float = 500e8) -> pd.DataFrame:
    meta = load_meta(code)
    print(f"\n标的: {meta['code']}  {meta['name']}  "
          f"sector_2=[{meta['sector_2']}]  上市日={meta['list_date']}")
    if not meta["sector_2"]:
        print("  [WARN] 该股 sector_2 为空, v2 共振过滤会全部淘汰它")
    if not meta["float_shares"]:
        print("  [WARN] 该股 float_shares 为空, 流通市值过滤会全部淘汰它")

    daily = load_one_stock_daily(code, start, end)
    sec = load_sector_series(meta["sector_2"], start, end) if meta["sector_2"] else pd.DataFrame()
    if sec.empty:
        print(f"  [WARN] sector [{meta['sector_2']}] 在 {start}~{end} 无数据")

    # 算 day_change_pct / 5 日均量 / volume_ratio / 流通市值
    daily["prev_close"] = daily["close"].shift(1)
    daily["day_change_pct"] = daily["close"] / daily["prev_close"] - 1.0
    daily["vol5"] = daily["volume"].shift(1).rolling(5).mean()
    daily["volume_ratio"] = daily["volume"] / daily["vol5"]
    daily["float_market_cap"] = daily["close"] * meta["float_shares"]

    # 按 start 截到目标区间
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)
    in_range = daily[(daily["trade_date"] >= start_ts) & (daily["trade_date"] <= end_ts)].copy()

    out_rows: List[Dict[str, Any]] = []
    list_date = pd.to_datetime(meta["list_date"]) if meta["list_date"] else None

    print(f"\n{'日期':<12} {'收':>7} {'涨幅':>7} {'量比':>5} {'市值亿':>7} "
          f"{'板块涨':>7} {'板涨家%':>7} {'v1':>3} {'v2':>3} {'分':>5} "
          f"{'未来 ' + str(hold) + 'd':>9}")
    print("-" * 95)

    for i, row in in_range.iterrows():
        t = row["trade_date"]
        chg = row["day_change_pct"]
        vr = row["volume_ratio"]
        mcap = row["float_market_cap"]
        close_t = row["close"]

        if pd.isna(chg) or pd.isna(vr) or close_t <= 0:
            continue

        # 板块共振字段
        s_chg, s_rise = None, None
        if t in sec.index:
            s_chg = float(sec.loc[t, "change_pct"])
            s_rise = float(sec.loc[t, "rise_ratio"])

        # 上市天数 (自然日)
        listed_days = int((t - list_date).days) if list_date is not None else None

        # 复用 picker 的逻辑判断 v1 / v2 是否通过 (这里手动展开, 让原因可打印)
        name = meta["name"]

        v1_pass, v1_reason = _check_v1(chg, close_t, mcap, vr, name,
                                        max_price=max_price,
                                        mcap_low=mcap_low,
                                        mcap_high=mcap_high)
        v2_pass, v2_reason = _check_v2(v1_pass, chg, listed_days, s_chg, s_rise)

        # dragon_score 用 picker 公式; 算前先填 rank_in_top=1 (单只股没有横截面)
        score_input = {
            "day_change_pct":    chg,
            "volume_ratio":      vr,
            "float_market_cap":  mcap,
            "price":             close_t,
            "rank_in_top":       1,
            "sector_change_pct": s_chg,
            "sector_rise_ratio": s_rise,
        }
        score = calc_dragon_score(score_input)

        # 真实未来 N 日收益 (用 daily 找 T+1.open 与 T+hold.close)
        idx_in_daily = daily.index[daily["trade_date"] == t]
        future_ret = None
        if len(idx_in_daily):
            j = idx_in_daily[0]
            if j + hold < len(daily):
                buy_open = daily.loc[j + 1, "open"]
                sell_close = daily.loc[j + hold, "close"]
                if buy_open and buy_open > 0:
                    future_ret = sell_close / buy_open - 1.0

        # 终端打印
        print(f"{t.strftime('%Y-%m-%d')} {close_t:>7.2f} {chg:>+7.2%} {vr:>5.1f} "
              f"{mcap/1e8:>7.0f} "
              f"{('-' if s_chg is None else f'{s_chg:+.2%}'):>7} "
              f"{('-' if s_rise is None else f'{s_rise:.0%}'):>7} "
              f"{('Y' if v1_pass else 'N'):>3} {('Y' if v2_pass else 'N'):>3} "
              f"{score:>5.2f} "
              f"{('-' if future_ret is None else f'{future_ret:+.2%}'):>9}"
              + (f"  [{v2_reason}]" if not v2_pass and v1_pass else ""))

        out_rows.append({
            "trade_date":   t.date(),
            "close":        round(close_t, 4),
            "day_chg":      round(chg, 4),
            "vol_ratio":    round(vr, 2),
            "mcap_yi":      round(mcap / 1e8, 1),
            "sector_chg":   None if s_chg is None else round(s_chg, 4),
            "sector_rise":  None if s_rise is None else round(s_rise, 3),
            "v1_pass":      int(v1_pass),
            "v2_pass":      int(v2_pass),
            "v2_reason":    v2_reason,
            "dragon_score": round(score, 3),
            "future_ret":   None if future_ret is None else round(future_ret, 4),
        })

    return pd.DataFrame(out_rows)


def _check_v1(chg: float, price: float, mcap: float, vr: float, name: str,
              max_price: float = 30.0,
              mcap_low: float = 30e8,
              mcap_high: float = 500e8) -> tuple[bool, str]:
    if chg < 0.05:
        return False, f"v1: 涨幅 {chg:+.2%} < 5%"
    if price > max_price:
        return False, f"v1: 价 {price:.2f} > {max_price:.0f}"
    if not (mcap_low <= mcap <= mcap_high):
        return False, f"v1: 市值 {mcap/1e8:.0f}亿 不在 {mcap_low/1e8:.0f}-{mcap_high/1e8:.0f}"
    if vr < 2.0:
        return False, f"v1: 量比 {vr:.1f} < 2"
    if "ST" in name.upper() or "*" in name or "退" in name:
        return False, "v1: ST/退市"
    return True, ""


def _check_v2(v1_pass: bool, chg: float, listed_days,
              s_chg, s_rise) -> tuple[bool, str]:
    if not v1_pass:
        return False, ""
    if chg > 0.095:
        return False, f"v2-6: 涨幅 {chg:+.2%} > 9.5% (近涨停)"
    if listed_days is not None and listed_days < 60:
        return False, f"v2-7: 上市 {listed_days} 天 < 60 (次新股)"
    if s_chg is None or s_rise is None:
        return False, "v2-8: 板块当日无数据"
    if s_chg < SECTOR_MIN_CHANGE_PCT:
        return False, f"v2-8: 板块涨 {s_chg:+.2%} < {SECTOR_MIN_CHANGE_PCT:.1%}"
    if s_rise < SECTOR_MIN_RISE_RATIO:
        return False, f"v2-8: 板涨家 {s_rise:.0%} < {SECTOR_MIN_RISE_RATIO:.0%}"
    return True, ""


# ============================================================
# 汇总
# ============================================================

def summarize(df: pd.DataFrame, hold: int):
    if df.empty:
        print("\n  无样本")
        return

    n = len(df)
    v1_n = int(df["v1_pass"].sum())
    v2_n = int(df["v2_pass"].sum())

    def _stats(mask, label):
        sub = df[mask & df["future_ret"].notna()]
        if sub.empty:
            print(f"    {label}: 0 笔, 无统计")
            return
        win = (sub["future_ret"] > 0).sum()
        print(f"    {label}: {len(sub):>3} 笔  胜率 {win/len(sub):.0%}  "
              f"均收 {sub['future_ret'].mean():+.2%}  "
              f"最优 {sub['future_ret'].max():+.2%}  "
              f"最差 {sub['future_ret'].min():+.2%}")

    print(f"\n{'='*95}")
    print(f"  汇总 (hold = T+1.open 买, T+{hold}.close 卖)")
    print(f"{'='*95}")
    print(f"  区间内交易日: {n} 天")
    print(f"  v1 触发: {v1_n} 天  ({v1_n/max(n,1):.0%})")
    print(f"  v2 触发: {v2_n} 天  ({v2_n/max(n,1):.0%})")
    print()
    _stats(df["v1_pass"] == 1, "v1 触发后未来收益")
    _stats(df["v2_pass"] == 1, "v2 触发后未来收益")

    # v1 触发但 v2 拦掉的, 看看真实收益
    v1_only = df[(df["v1_pass"] == 1) & (df["v2_pass"] == 0) & df["future_ret"].notna()]
    if not v1_only.empty:
        win = (v1_only["future_ret"] > 0).sum()
        print(f"\n  >>> v1 进 v2 拦的 {len(v1_only)} 笔: "
              f"胜率 {win/len(v1_only):.0%}  均收 {v1_only['future_ret'].mean():+.2%}  "
              f"(若这些都是亏的, 说明 v2 过滤有用)")


# ============================================================
# 入口
# ============================================================

def main() -> int:
    parser = argparse.ArgumentParser(
        description="单标的复盘 -- 看龙头战法在某只股某段时间的真实战绩"
    )
    parser.add_argument("--code",  required=True, help="股票代码, 如 300750.SZ")
    parser.add_argument("--start", required=True, help="起始日 YYYY-MM-DD")
    parser.add_argument("--end",   required=True, help="结束日 YYYY-MM-DD")
    parser.add_argument("--hold",  type=int, default=3, help="持有天数 H, 默认 3")
    parser.add_argument("--max-price", type=float, default=30.0,
                        help="价格上限, 默认 30 (大牛股复盘可放宽如 200)")
    parser.add_argument("--mcap-low",  type=float, default=30e8,
                        help="流通市值下限(元), 默认 30e8")
    parser.add_argument("--mcap-high", type=float, default=500e8,
                        help="流通市值上限(元), 默认 500e8 (大牛股可放到 5000e8)")
    args = parser.parse_args()

    df = run_replay(args.code, args.start, args.end, args.hold,
                    max_price=args.max_price,
                    mcap_low=args.mcap_low,
                    mcap_high=args.mcap_high)
    summarize(df, args.hold)

    out_dir = THIS_DIR.parent / "outputs"
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = out_dir / f"replay_{args.code}_{args.start}_{args.end}_H{args.hold}_{ts}.csv"
    df.to_csv(out_path, index=False, encoding="utf-8-sig")
    print(f"\n  CSV: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
