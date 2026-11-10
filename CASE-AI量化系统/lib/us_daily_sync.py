# -*- coding: utf-8 -*-
"""启动时把监控池里的美股日 K 补进 trade_stock_us_daily。

拉取和入库走课程脚本 8-美股行情采集-yahoo.py，这里只决定补哪些代码、从哪一天续。
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import date, timedelta
from pathlib import Path

_YAHOO_FILE = (
    Path(__file__).resolve().parents[3]
    / "4-数据获取与清洗" / "CASE-数据采集" / "8-美股行情采集-yahoo.py"
)
_HISTORY_YEARS = 5


def _load_yahoo():
    if not _YAHOO_FILE.is_file():
        raise FileNotFoundError(f"找不到美股采集脚本: {_YAHOO_FILE}")
    collect_dir = str(_YAHOO_FILE.parent)
    inserted = collect_dir not in sys.path
    if inserted:
        sys.path.insert(0, collect_dir)
    try:
        spec = importlib.util.spec_from_file_location("us_yahoo_collect", _YAHOO_FILE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        if inserted:
            sys.path.remove(collect_dir)
    return module


def _yahoo_symbol(stock_code: str) -> str:
    if stock_code in ("IXIC.US", "^IXIC"):
        return "^IXIC"
    return stock_code[:-3]


def _us_codes(codes) -> list[str]:
    seen = set()
    out = []
    for raw in codes or []:
        code = str(raw or "").strip().upper()
        if not code.endswith(".US") or code in seen:
            continue
        seen.add(code)
        out.append(code)
    return out


def _last_trade_date(mod, stock_code: str):
    rows = mod.execute_query(
        f"SELECT MAX(trade_date) AS d FROM {mod.US_TABLE} WHERE stock_code = %s",
        (stock_code,),
    )
    last = rows[0]["d"] if rows else None
    if hasattr(last, "date"):
        last = last.date()
    return last


def sync_one_us_daily(stock_code: str) -> str:
    """把一只美股日 K 续到今天。Yahoo 没写下数据时抛错。"""
    code = str(stock_code or "").strip().upper()
    if not code.endswith(".US"):
        raise ValueError(f"不是美股代码: {stock_code}")
    mod = _load_yahoo()
    mod.execute_update(mod.CREATE_TABLE_SQL)
    today = date.today()
    end = today + timedelta(days=1)
    last = _last_trade_date(mod, code)
    start = last or (today - timedelta(days=365 * _HISTORY_YEARS))
    if start > today:
        return f"{code} 日K已到 {start}"
    ok = bool(mod.save_symbol(_yahoo_symbol(code), code, start, end))
    if not ok:
        raise RuntimeError(f"{code} 没有采集到日K")
    latest = _last_trade_date(mod, code)
    return f"{code} 日K已更新到 {latest}"


def sync_watch_us_daily(codes) -> None:
    """把监控池中的 .US 代码补到今天。已有数据从最后一根续，没有的拉近 5 年。"""
    us_codes = _us_codes(codes)
    if not us_codes:
        return
    print(f"[us] 启动补齐美股日K {len(us_codes)} 只", flush=True)
    for code in us_codes:
        try:
            print(f"[us] {sync_one_us_daily(code)}", flush=True)
        except Exception as exc:
            print(f"[us] {code} 采集失败: {type(exc).__name__}: {exc}", flush=True)
