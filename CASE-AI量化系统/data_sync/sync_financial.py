# -*- coding: utf-8 -*-
# Author：@跟陈博士学AI
# 财务同步：HTTP 取数 copy 自第4讲 2-财务数据采集-QMT.py
# 增量判断 copy 自 CY_股票财务下载_定时_MYSQL.py 的 latest_due_period
from __future__ import annotations

import math
from collections.abc import Callable
from datetime import date

from pathlib import Path

from dragon_strategy.db_config import execute_query, get_connection

from data_sync.qmt_http import http_call, stock_list_in_sector
from lib.qmt_backend import is_tdx

SECTOR = "沪深A股"
BATCH_SIZE = 50
DATA_START = "20150101"
REPORT_MMDD = ("0331", "0630", "0930", "1231")
REPORT_DATE_SQL = """(
    (MONTH(report_date) = 3 AND DAY(report_date) = 31)
    OR (MONTH(report_date) = 6 AND DAY(report_date) = 30)
    OR (MONTH(report_date) = 9 AND DAY(report_date) = 30)
    OR (MONTH(report_date) = 12 AND DAY(report_date) = 31)
)"""

QMT_FIELD_LIST = [
    "PERSHAREINDEX.s_fa_eps_basic",
    "ASHAREINCOME.revenue_inc",
    "ASHAREINCOME.net_profit_incl_min_int_inc",
    "ASHAREINCOME.total_expense",
    "ASHAREBALANCESHEET.tot_assets",
    "ASHAREBALANCESHEET.tot_liab",
    "ASHAREBALANCESHEET.total_equity",
    "ASHAREBALANCESHEET.total_current_assets",
    "ASHAREBALANCESHEET.total_current_liability",
    "ASHARECASHFLOW.net_cash_flows_oper_act",
]

INSERT_SQL = """
    INSERT INTO trade_stock_financial
    (stock_code, report_date, revenue, net_profit, eps, roe, roa,
     gross_margin, net_margin, debt_ratio, current_ratio,
     operating_cashflow, total_assets, total_equity, data_source)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON DUPLICATE KEY UPDATE
    revenue=VALUES(revenue), net_profit=VALUES(net_profit), eps=VALUES(eps),
    roe=VALUES(roe), roa=VALUES(roa), gross_margin=VALUES(gross_margin),
    net_margin=VALUES(net_margin), debt_ratio=VALUES(debt_ratio),
    current_ratio=VALUES(current_ratio), operating_cashflow=VALUES(operating_cashflow),
    total_assets=VALUES(total_assets), total_equity=VALUES(total_equity)
"""

GPCW_CACHE = Path(__file__).resolve().parent / "_gpcw_cache"


def latest_due_period(today=None) -> str:
    """最近一个已结束的报告期 YYYYMMDD。"""
    d = today or date.today()
    y, m = d.year, d.month
    if m >= 10:
        return f"{y}0930"
    if m >= 7:
        return f"{y}0630"
    if m >= 4:
        return f"{y}0331"
    return f"{y - 1}1231"


def get_existing_latest_report_dates() -> dict[str, str]:
    rows = execute_query(
        "SELECT stock_code, MAX(report_date) AS max_date "
        "FROM trade_stock_financial WHERE " + REPORT_DATE_SQL +
        " GROUP BY stock_code"
    )
    result = {}
    for r in rows:
        if r["max_date"]:
            result[r["stock_code"]] = r["max_date"].strftime("%Y%m%d")
    return result


def _norm_val(v):
    if isinstance(v, float) and math.isnan(v):
        return None
    return v


def safe_float(val):
    if val is None:
        return None
    try:
        v = float(val)
        return v if v == v else None
    except (ValueError, TypeError):
        return None


def safe_divide(a, b, pct=False):
    if a is None or b is None:
        return None
    a, b = float(a), float(b)
    if b == 0:
        return None
    result = a / b
    if pct:
        result *= 100
    result = round(result, 4)
    if result > 999999.9999:
        return 999999.9999
    if result < -999999.9999:
        return -999999.9999
    return result


def extract_qmt_periods(rows):
    """从 /api/data/financial_data 的报告期列表算指标，只写入四季报期。"""
    records = []
    if not isinstance(rows, list):
        return records
    for row in rows:
        if not isinstance(row, dict):
            continue
        period = str(row.get("end_date") or "").strip()
        if len(period) != 8 or period[4:] not in REPORT_MMDD:
            continue
        revenue = _norm_val(row.get("revenue_inc"))
        net_profit = _norm_val(row.get("net_profit_incl_min_int_inc"))
        total_expense = _norm_val(row.get("total_expense"))
        total_assets = _norm_val(row.get("tot_assets"))
        total_liab = _norm_val(row.get("tot_liab"))
        total_equity = _norm_val(row.get("total_equity"))
        current_assets = _norm_val(row.get("total_current_assets"))
        current_liab = _norm_val(row.get("total_current_liability"))
        eps = _norm_val(row.get("s_fa_eps_basic"))
        operating_cashflow = _norm_val(row.get("net_cash_flows_oper_act"))
        if all(x is None for x in (revenue, net_profit, eps, total_assets)):
            continue
        gross_margin = None
        if revenue is not None and total_expense is not None:
            gross_margin = safe_divide(float(revenue) - float(total_expense), revenue, pct=True)
        records.append({
            "report_date": period,
            "revenue": safe_float(revenue),
            "net_profit": safe_float(net_profit),
            "eps": safe_float(eps),
            "roe": safe_divide(net_profit, total_equity, pct=True),
            "roa": safe_divide(net_profit, total_assets, pct=True),
            "gross_margin": safe_float(gross_margin),
            "net_margin": safe_divide(net_profit, revenue, pct=True),
            "debt_ratio": safe_divide(total_liab, total_assets, pct=True),
            "current_ratio": safe_divide(current_assets, current_liab),
            "operating_cashflow": safe_float(operating_cashflow),
            "total_assets": safe_float(total_assets),
            "total_equity": safe_float(total_equity),
        })
    return records


def _fetch_batch(batch_codes: list[str], start_date: str) -> dict:
    body = http_call("/api/data/financial_data", {
        "fieldList": ",".join(QMT_FIELD_LIST),
        "stockList": ",".join(batch_codes),
        "startDate": start_date,
        "endDate": date.today().strftime("%Y%m%d"),
        "report_type": "report_time",
    })
    data = (body or {}).get("data")
    if not isinstance(data, dict):
        raise TypeError(f"/api/data/financial_data 返回结构异常: {type(data)}")
    return data


def process_batch(batch_codes: list[str], start_date: str) -> tuple[int, int]:
    data = _fetch_batch(batch_codes, start_date)
    conn = get_connection()
    cursor = conn.cursor()
    batch_rows = 0
    batch_ok = 0
    try:
        for code in batch_codes:
            records = extract_qmt_periods(data.get(code) or [])
            if not records:
                continue
            rows = []
            for rec in records:
                p = rec["report_date"]
                rows.append((
                    code, f"{p[:4]}-{p[4:6]}-{p[6:8]}",
                    rec["revenue"], rec["net_profit"], rec["eps"],
                    rec["roe"], rec["roa"], rec["gross_margin"], rec["net_margin"],
                    rec["debt_ratio"], rec["current_ratio"],
                    rec["operating_cashflow"], rec["total_assets"], rec["total_equity"],
                    "qmt",
                ))
            cursor.executemany(INSERT_SQL, rows)
            batch_rows += len(rows)
            batch_ok += 1
        conn.commit()
    finally:
        cursor.close()
        conn.close()
    return batch_rows, batch_ok


def _sync_financial_tdx(on_progress, should_stop) -> dict:
    """按报告期写入通达信 gpcw 季报，已有主键不覆盖。"""
    import pandas as pd
    from data_sync import tdx_source
    from data_sync.CY_TDX_Client import to_stock_code

    def progress(**kw):
        if on_progress:
            on_progress(**kw)

    ready = tdx_source.probe()
    if not ready["ok"]:
        raise RuntimeError(ready["message"])

    due = latest_due_period()
    client = tdx_source.client(online=False)
    files = [x for x in client.list_financial_files(2015) if x["report_date"] <= due]
    allow = set(tdx_source.stock_list())
    existing = set()
    for r in execute_query("SELECT stock_code, report_date FROM trade_stock_financial"):
        if r["report_date"]:
            existing.add((r["stock_code"], r["report_date"].strftime("%Y-%m-%d")))

    total = len(files)
    progress(current=0, total=total, message=f"财务 {total} 个报告期，目标 {due}",
             success=0, failed=0, rows_written=0)
    if total == 0:
        return {"success": 0, "failed": 0, "rows_written": 0, "total": 0}

    success = failed = rows_written = done = 0
    for item in files:
        if should_stop and should_stop():
            break
        period = item["report_date"]
        report_date = f"{period[:4]}-{period[4:6]}-{period[6:8]}"
        try:
            df = client.parse_financial_file(item["filename"], GPCW_CACHE)
            if df is None or getattr(df, "empty", True):
                done += 1
                failed += 1
                continue
            rows = []
            for code6 in pd.Index(df.index).unique():
                qmt_code = to_stock_code(str(code6))
                if qmt_code not in allow or (qmt_code, report_date) in existing:
                    continue
                loc = df.loc[code6]
                if isinstance(loc, pd.DataFrame):
                    loc = loc.iloc[0]
                rec = row_to_record(loc)
                if not rec:
                    continue
                rows.append((
                    qmt_code, report_date,
                    rec["revenue"], rec["net_profit"], rec["eps"],
                    rec["roe"], rec["roa"], rec["gross_margin"], rec["net_margin"],
                    rec["debt_ratio"], rec["current_ratio"],
                    rec["operating_cashflow"], rec["total_assets"], rec["total_equity"],
                    "tdx",
                ))
            if rows:
                conn = get_connection()
                cursor = conn.cursor()
                cursor.executemany(INSERT_SQL, rows)
                conn.commit()
                cursor.close()
                conn.close()
                rows_written += len(rows)
                existing.update((r[0], r[1]) for r in rows)
            success += 1
        except Exception as exc:
            failed += 1
            print(f"[财务] {period} 失败: {exc}", flush=True)
        done += 1
        progress(current=done, total=total, success=success, failed=failed,
                 rows_written=rows_written, message=f"财务 {period} ({done}/{total})")
    return {"success": success, "failed": failed, "rows_written": rows_written, "total": total}


def row_to_record(row):
    """一行 gpcw -> trade_stock_financial 字段。"""
    import pandas as pd

    def _col(name):
        if name not in row.index:
            return None
        v = row[name]
        if isinstance(v, pd.Series):
            v = v.iloc[0]
        return safe_float(v)

    revenue = _col("其中：营业收入")
    net_profit = _col("五、净利润")
    eps = _col("基本每股收益")
    total_assets = _col("资产总计")
    total_equity = _col("所有者权益（或股东权益）合计")
    total_liab = _col("负债合计")
    current_assets = _col("流动资产合计")
    current_liab = _col("流动负债合计")
    operating_cashflow = _col("经营活动产生的现金流量净额")
    cogs = _col("其中：营业成本")
    if all(x is None for x in (revenue, net_profit, eps, total_assets)):
        return None
    return {
        "revenue": revenue,
        "net_profit": net_profit,
        "eps": eps,
        "roe": safe_divide(net_profit, total_equity, pct=True),
        "roa": safe_divide(net_profit, total_assets, pct=True),
        "gross_margin": safe_divide(
            None if revenue is None or cogs is None else revenue - cogs,
            revenue, pct=True),
        "net_margin": safe_divide(net_profit, revenue, pct=True),
        "debt_ratio": safe_divide(total_liab, total_assets, pct=True),
        "current_ratio": safe_divide(current_assets, current_liab),
        "operating_cashflow": operating_cashflow,
        "total_assets": total_assets,
        "total_equity": total_equity,
    }


def sync_financial(
    on_progress: Callable[..., None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> dict:
    if is_tdx():
        return _sync_financial_tdx(on_progress, should_stop)

    def progress(**kw):
        if on_progress:
            on_progress(**kw)

    due = latest_due_period()
    all_codes = stock_list_in_sector(SECTOR)
    existing = get_existing_latest_report_dates()
    pending = []
    for code in all_codes:
        latest = existing.get(code)
        if latest and latest >= due:
            continue
        pending.append((code, latest if latest else DATA_START))

    total = len(pending)
    progress(
        current=0, total=total,
        message=f"财务待更新 {total} 只，目标报告期 {due}",
        success=0, failed=0, rows_written=0,
    )
    if total == 0:
        return {"success": 0, "failed": 0, "rows_written": 0, "total": 0}

    success = failed = rows_written = done = 0
    for i in range(0, total, BATCH_SIZE):
        if should_stop and should_stop():
            break
        chunk = pending[i:i + BATCH_SIZE]
        codes = [c for c, _ in chunk]
        start = min(s for _, s in chunk)
        try:
            n_rows, n_ok = process_batch(codes, start)
            success += n_ok
            failed += len(codes) - n_ok
            rows_written += n_rows
        except Exception:
            failed += len(codes)
        done += len(codes)
        progress(
            current=done, total=total,
            success=success, failed=failed, rows_written=rows_written,
            message=f"财务批次 {done}/{total}",
        )
    return {"success": success, "failed": failed, "rows_written": rows_written, "total": total}
