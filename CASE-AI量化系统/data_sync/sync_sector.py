# -*- coding: utf-8 -*-
# Author：@跟陈博士学AI
# 板块指数合成：copy 自 21-CASE-A sector_index_builder.py，加进度回调
from __future__ import annotations

from collections.abc import Callable
from datetime import date
from typing import Dict, List, Optional, Tuple

import numpy as np

from dragon_strategy.db_config import execute_many, execute_query

from data_sync.sync_status import get_sector_member_codes, list_sectors_from_db

BASE_INDEX = 1000.0


def get_all_trade_dates() -> List[date]:
    rows = execute_query(
        "SELECT DISTINCT trade_date FROM trade_stock_daily ORDER BY trade_date ASC")
    return [r["trade_date"] for r in rows]


def get_prev_close_idx(sector_name: str, sector_level: int,
                       prev_trade_date: date) -> float:
    rows = execute_query(
        "SELECT close_idx FROM trade_sector_daily "
        "WHERE sector_name = %s AND sector_level = %s AND trade_date = %s",
        (sector_name, sector_level, prev_trade_date))
    if rows and rows[0]["close_idx"] is not None:
        return float(rows[0]["close_idx"])
    return BASE_INDEX


def fetch_member_kline_panel(member_codes: List[str],
                             trade_dates: List[date]) -> Dict[Tuple[str, date], Dict]:
    if not member_codes or not trade_dates:
        return {}
    ph_codes = ",".join(["%s"] * len(member_codes))
    ph_dates = ",".join(["%s"] * len(trade_dates))
    sql = f"""
        SELECT stock_code, trade_date, open_price, high_price, low_price,
               close_price, volume, amount
        FROM trade_stock_daily
        WHERE stock_code IN ({ph_codes}) AND trade_date IN ({ph_dates})
    """
    params = list(member_codes) + list(trade_dates)
    rows = execute_query(sql, params)
    result: Dict[Tuple[str, date], Dict] = {}
    for r in rows:
        result[(r["stock_code"], r["trade_date"])] = {
            "open":   float(r["open_price"])  if r["open_price"]  is not None else None,
            "high":   float(r["high_price"])  if r["high_price"]  is not None else None,
            "low":    float(r["low_price"])   if r["low_price"]   is not None else None,
            "close":  float(r["close_price"]) if r["close_price"] is not None else None,
            "volume": int(r["volume"])        if r["volume"]      is not None else 0,
            "amount": float(r["amount"])      if r["amount"]      is not None else 0.0,
        }
    return result


def aggregate_sector_one_day(sector_name: str, sector_level: int,
                             member_codes: List[str],
                             trade_date: date,
                             prev_trade_date: Optional[date],
                             prev_close_idx: float,
                             kline_panel: Dict[Tuple[str, date], Dict]) -> Optional[Dict]:
    pcts: List[Tuple[str, Dict, Dict]] = []
    for code in member_codes:
        today = kline_panel.get((code, trade_date))
        if not today or today["close"] is None:
            continue
        prev = kline_panel.get((code, prev_trade_date)) if prev_trade_date else None
        pcts.append((code, today, prev))
    if not pcts:
        return None

    rise = fall = flat = lu = ld = 0
    pct_list: List[Tuple[str, float]] = []
    for code, today, prev in pcts:
        if not prev or not prev["close"]:
            continue
        pct = today["close"] / prev["close"] - 1
        pct_list.append((code, pct))
        if pct > 0.0001:
            rise += 1
        elif pct < -0.0001:
            fall += 1
        else:
            flat += 1
        if pct >= 0.097:
            lu += 1
        elif pct <= -0.097:
            ld += 1

    change_pct = float(np.mean([p for _, p in pct_list])) * 100 if pct_list else 0.0
    if pct_list:
        top_code, top_pct = max(pct_list, key=lambda x: x[1])
    else:
        top_code, top_pct = "", 0.0

    total_volume = sum(t["volume"] for _, t, _ in pcts)
    total_amount = sum(t["amount"] for _, t, _ in pcts)

    open_rets, high_rets, low_rets, close_rets = [], [], [], []
    for code, today, prev in pcts:
        if not prev or not prev["close"]:
            continue
        if today["open"] is not None:
            open_rets.append(today["open"] / prev["close"] - 1)
        if today["high"] is not None:
            high_rets.append(today["high"] / prev["close"] - 1)
        if today["low"] is not None:
            low_rets.append(today["low"] / prev["close"] - 1)
        close_rets.append(today["close"] / prev["close"] - 1)

    if close_rets:
        open_idx = prev_close_idx * (1 + np.mean(open_rets)) if open_rets else prev_close_idx
        high_idx = prev_close_idx * (1 + np.mean(high_rets)) if high_rets else prev_close_idx
        low_idx = prev_close_idx * (1 + np.mean(low_rets)) if low_rets else prev_close_idx
        close_idx = prev_close_idx * (1 + np.mean(close_rets))
    else:
        open_idx = high_idx = low_idx = close_idx = prev_close_idx

    return {
        "sector_name": sector_name,
        "sector_level": sector_level,
        "trade_date": trade_date,
        "change_pct": round(change_pct, 4),
        "stock_count": len(member_codes),
        "rise_count": rise,
        "fall_count": fall,
        "flat_count": flat,
        "limit_up": lu,
        "limit_down": ld,
        "top_stock": top_code,
        "top_stock_name": top_code,
        "top_stock_pct": round(top_pct * 100, 2),
        "open_idx": round(open_idx, 4),
        "high_idx": round(high_idx, 4),
        "low_idx": round(low_idx, 4),
        "close_idx": round(close_idx, 4),
        "total_volume": total_volume,
        "total_amount": round(total_amount, 2),
        "avg_turnover": 0.0,
        "kline_stock_count": len(pcts),
    }


def save_sector_rows(rows: List[Dict]) -> int:
    if not rows:
        return 0
    sql = """
        INSERT INTO trade_sector_daily
            (sector_name, sector_level, trade_date, change_pct, stock_count,
             rise_count, fall_count, flat_count, limit_up, limit_down,
             top_stock, top_stock_name, top_stock_pct,
             open_idx, high_idx, low_idx, close_idx,
             total_volume, total_amount, avg_turnover, kline_stock_count)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON DUPLICATE KEY UPDATE
            change_pct = VALUES(change_pct),
            stock_count = VALUES(stock_count),
            rise_count = VALUES(rise_count),
            fall_count = VALUES(fall_count),
            flat_count = VALUES(flat_count),
            limit_up = VALUES(limit_up),
            limit_down = VALUES(limit_down),
            top_stock = VALUES(top_stock),
            top_stock_name = VALUES(top_stock_name),
            top_stock_pct = VALUES(top_stock_pct),
            open_idx = VALUES(open_idx),
            high_idx = VALUES(high_idx),
            low_idx = VALUES(low_idx),
            close_idx = VALUES(close_idx),
            total_volume = VALUES(total_volume),
            total_amount = VALUES(total_amount),
            avg_turnover = VALUES(avg_turnover),
            kline_stock_count = VALUES(kline_stock_count)
    """
    rows_tuples = [(
        r["sector_name"], r["sector_level"], r["trade_date"], r["change_pct"], r["stock_count"],
        r["rise_count"], r["fall_count"], r["flat_count"], r["limit_up"], r["limit_down"],
        r["top_stock"], r["top_stock_name"], r["top_stock_pct"],
        r["open_idx"], r["high_idx"], r["low_idx"], r["close_idx"],
        r["total_volume"], r["total_amount"], r["avg_turnover"], r["kline_stock_count"],
    ) for r in rows]
    return execute_many(sql, rows_tuples)


def rebuild_one_sector(sector_name: str, sector_level: int,
                       target_dates: List[date],
                       all_dates: List[date]) -> int:
    members = get_sector_member_codes(sector_name, level=sector_level)
    if not members:
        return 0
    date_to_idx = {d: i for i, d in enumerate(all_dates)}
    fetch_dates = set()
    for d in target_dates:
        fetch_dates.add(d)
        idx = date_to_idx.get(d)
        if idx is not None and idx > 0:
            fetch_dates.add(all_dates[idx - 1])
    panel = fetch_member_kline_panel(members, sorted(fetch_dates))

    rows = []
    prev_close_idx = None
    for d in target_dates:
        idx = date_to_idx.get(d)
        prev_d = all_dates[idx - 1] if idx and idx > 0 else None
        if prev_close_idx is None:
            prev_close_idx = get_prev_close_idx(sector_name, sector_level, prev_d) if prev_d else BASE_INDEX
        row = aggregate_sector_one_day(
            sector_name=sector_name, sector_level=sector_level,
            member_codes=members,
            trade_date=d, prev_trade_date=prev_d,
            prev_close_idx=prev_close_idx,
            kline_panel=panel)
        if row is None:
            continue
        rows.append(row)
        prev_close_idx = row["close_idx"]
    return save_sector_rows(rows)


def sync_sector(
    on_progress: Callable[..., None] | None = None,
    should_stop: Callable[[], bool] | None = None,
    days: int = 60,
    full: bool = False,
    level: int = 2,
) -> dict:
    all_dates = get_all_trade_dates()
    if not all_dates:
        raise RuntimeError("trade_stock_daily 为空，请先同步日K")
    target_dates = all_dates if full else all_dates[-days:]
    sectors = list_sectors_from_db(level=level)
    if not sectors:
        raise RuntimeError("trade_stock_status 没有申万分类，请先同步股票资料")

    total = len(sectors)
    written = 0
    if on_progress:
        on_progress(current=0, total=total, message=f"合成板块 {total} 个")
    for i, sector in enumerate(sectors, 1):
        if should_stop and should_stop():
            break
        n = rebuild_one_sector(sector, level, target_dates, all_dates)
        written += n
        if on_progress:
            on_progress(
                current=i, total=total, success=i, rows_written=written,
                message=f"板块 {sector} ({i}/{total})",
            )
    return {"success": total, "failed": 0, "rows_written": written, "total": total}
