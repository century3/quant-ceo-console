# -*- coding: utf-8 -*-
# Author：@跟陈博士学AI
# 股票资料同步：copy 自 21-CASE-A industry_meta.py（HTTP + sw_sectors 静态名录）
from __future__ import annotations

from collections.abc import Callable
from datetime import date

from dragon_strategy.db_config import execute_many, execute_query

from data_sync.qmt_http import http_call
from data_sync.sw_sectors import SW_LEVEL_1, SW_LEVEL_2, xt_name
from lib.qmt_backend import is_tdx

SW2014_EXCLUDE = frozenset([
    "SW1银行",
    "饮料制造",
    "电子制造",
    "家用轻工",
])


def is_sw_level_1(name: str) -> bool:
    return (name.startswith("SW1")
            and not name.endswith("加权")
            and name not in SW2014_EXCLUDE)


def is_sw_level_2(name: str) -> bool:
    if name.endswith("加权") or name in SW2014_EXCLUDE:
        return False
    if name.startswith("SW2"):
        return True
    if name.endswith("II") and not name.startswith("SW"):
        return True
    return False


def display_name(sector_xt_name: str, level: int) -> str:
    if level == 1 and sector_xt_name.startswith("SW1"):
        return sector_xt_name[3:]
    if level == 2:
        if sector_xt_name.startswith("SW2"):
            return sector_xt_name[3:]
        if sector_xt_name.endswith("II"):
            return sector_xt_name[:-2]
    return sector_xt_name


def list_sectors_from_db(level: int = 2) -> list[str]:
    field = "sector_1" if level == 1 else "sector_2"
    rows = execute_query(
        f"SELECT DISTINCT {field} AS name FROM trade_stock_status "
        f"WHERE {field} IS NOT NULL ORDER BY {field}")
    return [r["name"] for r in rows]


def get_sector_member_codes(sector_name: str, level: int = 2) -> list[str]:
    field = "sector_1" if level == 1 else "sector_2"
    rows = execute_query(
        f"SELECT stock_code FROM trade_stock_status WHERE {field} = %s ORDER BY stock_code",
        (sector_name,))
    return [r["stock_code"] for r in rows]


def _parse_list_date(raw) -> date | None:
    if raw is None:
        return None
    s = str(raw).replace("-", "")[:8]
    if len(s) != 8 or not s.isdigit() or s.startswith("0"):
        return None
    return date(int(s[:4]), int(s[4:6]), int(s[6:8]))


def _existing_status() -> dict[str, dict]:
    try:
        rows = execute_query(
            "SELECT stock_code, stock_name, float_shares, list_date FROM trade_stock_status"
        )
    except Exception:
        return {}
    return {r["stock_code"]: r for r in rows}


def fetch_sw_classification(
    on_progress: Callable[..., None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> dict[str, dict]:
    sw1 = [xt_name(n, 1) for n in SW_LEVEL_1]
    sw2 = [xt_name(n, 2) for n in SW_LEVEL_2]
    classification: dict[str, dict] = {}

    def progress(msg, current=0, total=0):
        if on_progress:
            on_progress(current=current, total=total, message=msg)

    progress(f"申万一级成分 {len(sw1)} 个板块", 0, len(sw1) + len(sw2))
    for i, sector in enumerate(sw1, 1):
        if should_stop and should_stop():
            return classification
        codes = http_call("/api/data/stock_list_in_sector", {"sectorname": sector}).get("stocks") or []
        l1_name = display_name(sector, 1)
        for code in codes:
            classification.setdefault(code, {})["sector_1"] = l1_name
        progress(f"申万一级 {sector}", i, len(sw1) + len(sw2))

    for i, sector in enumerate(sw2, 1):
        if should_stop and should_stop():
            return classification
        codes = http_call("/api/data/stock_list_in_sector", {"sectorname": sector}).get("stocks") or []
        l2_name = display_name(sector, 2)
        for code in codes:
            classification.setdefault(code, {})["sector_2"] = l2_name
        progress(f"申万二级 {sector}", len(sw1) + i, len(sw1) + len(sw2))
    return classification


def _fill_instrument(classification: dict[str, dict],
                     on_progress: Callable[..., None] | None = None,
                     should_stop: Callable[[], bool] | None = None) -> None:
    """缺名称 / 流通股本 / 上市日的股票才打 instrumentdetail。"""
    existing = _existing_status()
    need = []
    for code, info in classification.items():
        old = existing.get(code) or {}
        if old.get("stock_name") and old.get("float_shares") and old.get("list_date"):
            info["stock_name"] = old["stock_name"]
            info["float_shares"] = old["float_shares"]
            info["list_date"] = old["list_date"]
            info["total_shares"] = None
            continue
        need.append(code)

    total = len(need)
    if on_progress:
        on_progress(current=0, total=total, message=f"补股票资料 {total} 只")
    for i, code in enumerate(need, 1):
        if should_stop and should_stop():
            return
        body = http_call("/api/data/instrumentdetail", {"stockcode": code})
        detail = (body or {}).get("detail") or {}
        info = classification[code]
        info["stock_name"] = detail.get("InstrumentName") or code
        info["float_shares"] = (
            detail.get("FloatVolume") or detail.get("NegotiableVolume") or None
        )
        info["total_shares"] = detail.get("TotalVolume")
        info["list_date"] = _parse_list_date(detail.get("OpenDate") or detail.get("CreateDate"))
        if on_progress and (i % 20 == 0 or i == total):
            on_progress(current=i, total=total, message=f"股票资料 {code} ({i}/{total})")


def upsert_stock_status(classification: dict[str, dict]) -> int:
    if not classification:
        return 0
    rows = []
    for code, info in classification.items():
        rows.append((
            code,
            info.get("stock_name") or code,
            info.get("list_date"),
            info.get("total_shares"),
            info.get("float_shares"),
            info.get("sector_1"),
            info.get("sector_2"),
        ))
    sql = """
        INSERT INTO trade_stock_status
            (stock_code, stock_name, list_date, total_shares, float_shares, sector_1, sector_2)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            stock_name   = VALUES(stock_name),
            list_date    = COALESCE(VALUES(list_date), list_date),
            total_shares = COALESCE(VALUES(total_shares), total_shares),
            float_shares = COALESCE(VALUES(float_shares), float_shares),
            sector_1     = COALESCE(VALUES(sector_1), sector_1),
            sector_2     = COALESCE(VALUES(sector_2), sector_2)
    """
    return execute_many(sql, rows)


def _sync_status_tdx(on_progress, should_stop) -> dict:
    """名称 / 股本 / 上市日来自通达信；申万板块沿用库里已有的，不重刷。"""
    from data_sync import tdx_source

    def progress(**kw):
        if on_progress:
            on_progress(**kw)

    ready = tdx_source.probe()
    if not ready["ok"]:
        raise RuntimeError(ready["message"])
    rows = tdx_source.stock_profiles()
    total = len(rows)
    progress(current=0, total=total, message=f"股票资料 {total} 只（通达信，不改申万板块）",
             success=0, failed=0, rows_written=0)
    if should_stop and should_stop():
        return {"success": 0, "failed": 0, "rows_written": 0, "total": 0}
    n = execute_many(
        """
        INSERT INTO trade_stock_status
            (stock_code, stock_name, list_date, total_shares, float_shares)
        VALUES (%s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            stock_name   = COALESCE(VALUES(stock_name), stock_name),
            list_date    = COALESCE(VALUES(list_date), list_date),
            total_shares = COALESCE(VALUES(total_shares), total_shares),
            float_shares = COALESCE(VALUES(float_shares), float_shares)
        """,
        rows,
    )
    progress(current=total, total=total, success=total, rows_written=n,
             message=f"股票资料写入 {total} 只（申万板块未改）")
    return {"success": total, "failed": 0, "rows_written": n, "total": total}


def sync_status(
    on_progress: Callable[..., None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> dict:
    if is_tdx():
        return _sync_status_tdx(on_progress, should_stop)
    classification = fetch_sw_classification(on_progress, should_stop)
    if should_stop and should_stop():
        return {"success": 0, "failed": 0, "rows_written": 0, "total": 0}
    _fill_instrument(classification, on_progress, should_stop)
    n = upsert_stock_status(classification)
    if on_progress:
        on_progress(
            current=len(classification), total=len(classification),
            success=len(classification), rows_written=n,
            message=f"股票资料写入 {len(classification)} 只",
        )
    return {"success": len(classification), "failed": 0, "rows_written": n, "total": len(classification)}
