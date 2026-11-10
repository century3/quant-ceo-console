# -*- coding: utf-8 -*-
# Author：@跟陈博士学AI
# QMT HTTP / 共享内存统一封装，按 WUCAI_QMT 选择传输方式
"""
qmt 使用 CY_QMT_API.py；qmt_shared 使用 qmt_gateway_loader.py。
这里不再走 miniQMT / xtdata。
"""
from __future__ import annotations

import json
import os
from datetime import date
from typing import Any

import requests

SECTOR = "沪深A股"


def _base() -> str:
    return os.environ.get("QMT_BASE_URL", "http://127.0.0.1:15588").rstrip("/")


def _token() -> str:
    return os.environ.get("QMT_TOKEN", "learning_ai_with_dr_chen")


def http_call(path: str, payload: dict | None = None, method: str = "POST",
              timeout: int = 120) -> dict:
    """按当前后端走 HTTP 或共享内存，返回结构保持一致。"""
    from lib.qmt_backend import is_shared
    if is_shared():
        from data_sync.qmt_shared import shared_http
        return shared_http(path, payload, method, timeout)
    resp = requests.request(
        method, f"{_base()}{path}",
        headers={"Content-Type": "application/json", "X-Token": _token()},
        json=payload or {},
        timeout=timeout,
        proxies={"http": None, "https": None},
    )
    resp.raise_for_status()
    return json.loads(resp.text)


def ping() -> dict:
    """探活当前 QMT 后端。成功返回 {ok, python_version, base}。"""
    from lib.qmt_backend import is_shared
    base = "shared-memory://SentinelQmtLoaderV1" if is_shared() else _base()
    try:
        body = http_call("/api/sys/python_version", method="GET", timeout=8)
        return {
            "ok": True,
            "base": base,
            "python_version": str(body.get("python_version") or "")[:20],
        }
    except Exception as e:
        return {"ok": False, "base": base, "message": str(e)[:120]}


def stock_list_in_sector(sector: str = SECTOR) -> list[str]:
    body = http_call("/api/data/stock_list_in_sector", {"sectorname": sector})
    codes = (body or {}).get("stocks") or []
    return [c for c in codes if "." in str(c)]


def last_trading_date(today: date | None = None) -> date | None:
    """最近一个已结束的交易日。用上证指数日K（market_data_ex），不走会返回空列表的 trading_dates。"""
    d = today or date.today()
    body = http_call("/api/data/market_data_ex", {
        "stock_code": "000001.SH",
        "fields": "close",
        "period": "1d",
        "start_time": "",
        "end_time": "",
        "count": 3,
        "dividend_type": "front",
    })
    close_map = ((body or {}).get("data") or {}).get("000001.SH", {}).get("close") or {}
    parsed: list[date] = []
    for raw in close_map.keys():
        s = str(raw).replace("-", "").replace("/", "").strip()
        if len(s) >= 8 and s[:8].isdigit() and s[0] in "12":
            parsed.append(date(int(s[:4]), int(s[4:6]), int(s[6:8])))
    if not parsed:
        return None
    parsed.sort()
    last = parsed[-1]
    if last == d:
        return parsed[-2] if len(parsed) >= 2 else last
    return last
