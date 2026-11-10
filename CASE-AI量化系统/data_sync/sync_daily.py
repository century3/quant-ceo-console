# -*- coding: utf-8 -*-
# Author：@跟陈博士学AI
# 日K 同步：QMT HTTP / QMT共享内存 / 通达信统一入口
# 口径与第4讲一致：open/high/low/close 只存前复权。
# 无新除权则从库里最新日增量；有新除权，或这只股票还没整段刷过前复权，
# 从该股最早入库日起整段重拉并覆盖。除权日水位记在 trade_stock_qfq_mark。
from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone

import pandas as pd

from dragon_strategy.db_config import execute_many, execute_query, execute_update

from data_sync.qmt_http import http_call, stock_list_in_sector
from lib.qmt_backend import is_tdx

SECTOR = "沪深A股"
DATA_START = "20230101"
NUM_WORKERS = 4
CHUNK = 40
# 全量 upsert 时 4 线程会同时改同一批交易日，InnoDB 在 idx_stock_daily_date
# 上交叉加锁触发 1213。取数继续并行，写库串行。
_WRITE_LOCK = threading.Lock()

INSERT_SQL = """
    INSERT INTO trade_stock_daily
    (stock_code, trade_date, open_price, high_price, low_price, close_price, volume, amount, turnover_rate)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON DUPLICATE KEY UPDATE
    open_price=VALUES(open_price), high_price=VALUES(high_price),
    low_price=VALUES(low_price), close_price=VALUES(close_price),
    volume=VALUES(volume), amount=VALUES(amount),
    turnover_rate=VALUES(turnover_rate)
"""

# QMT get_divid_factors 的 key 是 UTC 毫秒，换成北京时间才是除权日
_CST = timezone(timedelta(hours=8))

_MARK_DDL = """
CREATE TABLE IF NOT EXISTS trade_stock_qfq_mark (
    stock_code VARCHAR(20) NOT NULL PRIMARY KEY COMMENT '股票代码',
    last_ex_date DATE NULL COMMENT '已覆盖进前复权K线的最近除权日，从未除权则为空',
    refreshed_at DATETIME NOT NULL COMMENT '最近一次整段前复权重刷时间'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='前复权除权水位'
"""

_PRICE_COMMENT = {
    "open_price": "开盘价(前复权)",
    "high_price": "最高价(前复权)",
    "low_price": "最低价(前复权)",
    "close_price": "收盘价(前复权)",
}


def _ensure_qfq_schema() -> None:
    """水位表 + 把开高低收注释改成前复权。精度保持 DECIMAL(14,4)。"""
    execute_update(_MARK_DDL)
    rows = execute_query(
        "SELECT COLUMN_NAME, COLUMN_TYPE, COLUMN_COMMENT "
        "FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'trade_stock_daily' "
        "AND COLUMN_NAME IN ('open_price','high_price','low_price','close_price')"
    )
    pending = []
    for r in rows:
        name = r["COLUMN_NAME"]
        if r["COLUMN_COMMENT"] == _PRICE_COMMENT[name]:
            continue
        col_type = r["COLUMN_TYPE"] or "decimal(14,4)"
        pending.append(
            f"MODIFY {name} {col_type} NULL COMMENT '{_PRICE_COMMENT[name]}'"
        )
    if pending:
        execute_update("ALTER TABLE trade_stock_daily " + ", ".join(pending))


def _ex_date_from_key(raw) -> date | None:
    """除权因子字典的 key -> 除权日。8 位日期直接解析，否则按 UTC 毫秒转北京时间。"""
    s = str(raw).strip()
    if len(s) == 8 and s.isdigit() and s[0] in "12":
        return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
    try:
        ms = int(s)
    except ValueError:
        return None
    if ms < 10**11:
        ms *= 1000
    return datetime.fromtimestamp(ms / 1000, _CST).date()


def _latest_ex_date(stock_code: str) -> date | None:
    """这只股票目前最新的除权除息日。没有分红送转则为 None。"""
    if is_tdx():
        from data_sync import tdx_source
        return tdx_source.latest_ex_date(stock_code)
    body = http_call("/api/data/divid_factors", {"stockcode": stock_code})
    factors = (body or {}).get("factors") or {}
    if not isinstance(factors, dict):
        return None
    latest = None
    for raw in factors:
        d = _ex_date_from_key(raw)
        if d and (latest is None or d > latest):
            latest = d
    return latest


def _load_spans() -> dict[str, dict[str, str]]:
    rows = execute_query(
        "SELECT stock_code, MIN(trade_date) AS min_date, MAX(trade_date) AS max_date "
        "FROM trade_stock_daily GROUP BY stock_code"
    )
    spans = {}
    for r in rows:
        if r["min_date"] and r["max_date"]:
            spans[r["stock_code"]] = {
                "min": r["min_date"].strftime("%Y%m%d"),
                "max": r["max_date"].strftime("%Y%m%d"),
            }
    return spans


def _load_marks() -> dict[str, date | None]:
    rows = execute_query("SELECT stock_code, last_ex_date FROM trade_stock_qfq_mark")
    return {r["stock_code"]: r["last_ex_date"] for r in rows}


def _save_mark(stock_code: str, last_ex: date | None) -> None:
    with _WRITE_LOCK:
        execute_update(
            "INSERT INTO trade_stock_qfq_mark (stock_code, last_ex_date, refreshed_at) "
            "VALUES (%s, %s, NOW()) "
            "ON DUPLICATE KEY UPDATE last_ex_date=VALUES(last_ex_date), refreshed_at=NOW()",
            (stock_code, last_ex),
        )


def _need_full(marked: bool, prev_ex: date | None, latest_ex: date | None) -> bool:
    """没刷过前复权，或出现了更新的除权日，才整段重拉。"""
    if not marked:
        return True
    if latest_ex is None:
        return False
    if prev_ex is None:
        return True
    return latest_ex > prev_ex


def get_existing_latest_dates() -> dict[str, str]:
    rows = execute_query(
        "SELECT stock_code, MAX(trade_date) AS max_date FROM trade_stock_daily GROUP BY stock_code"
    )
    result = {}
    for r in rows:
        if r["max_date"]:
            result[r["stock_code"]] = r["max_date"].strftime("%Y%m%d")
    return result


def _get_float_shares(stock_code: str) -> float:
    if is_tdx():
        from data_sync import tdx_source
        wan = tdx_source.client(online=False).get_ltag_wan(stock_code)
        return float(wan) * 10000 if wan else 0
    body = http_call("/api/data/last_volume", {"stockcode": stock_code})
    neg = (body or {}).get("last_volume") or 0
    return float(neg) if neg else 0


def _config_codes() -> list[str]:
    """监控池 / 模拟持仓里的代码。纳指 ETF 等不在沪深A股扫描里，也要能入库。"""
    from pathlib import Path
    import yaml
    root = Path(__file__).resolve().parent.parent
    codes: list[str] = []
    watch = root / "config" / "watch_pool.yaml"
    mock = root / "config" / "mock_positions.yaml"
    if watch.exists():
        data = yaml.safe_load(watch.read_text(encoding="utf-8")) or {}
        codes.extend(str(c).strip() for c in (data.get("codes") or []) if str(c).strip())
    if mock.exists():
        data = yaml.safe_load(mock.read_text(encoding="utf-8")) or {}
        codes.extend(str(c).strip() for c in (data.get("slot_codes") or []) if str(c).strip())
        for pos in data.get("positions") or []:
            c = str((pos or {}).get("code") or "").strip()
            if c:
                codes.append(c)
    return list(dict.fromkeys(codes))


def _market_codes() -> list[str]:
    if is_tdx():
        from data_sync import tdx_source
        codes = list(tdx_source.stock_list())
        seen = set(codes)
        for c in _config_codes():
            if c not in seen:
                codes.append(c)
                seen.add(c)
        return codes
    return stock_list_in_sector(SECTOR)


def _fetch_kline(stock_code: str, start_date: str) -> pd.DataFrame | None:
    fields = ["open", "high", "low", "close", "volume", "amount"]
    if is_tdx():
        from data_sync import tdx_source
        body = tdx_source.client(online=True).get_market_data_ex(
            stock_code, period="1d", count=-1, start_time=start_date,
            end_time="", fields=fields, dividend_type="front",
        )
    else:
        body = http_call("/api/data/market_data_ex", {
            "stock_code": stock_code, "fields": ",".join(fields),
            "period": "1d", "start_time": start_date, "end_time": "",
            "count": -1, "dividend_type": "front",
        })
    per_stock = ((body or {}).get("data") or {}).get(stock_code, {})
    if not per_stock:
        return None
    keys = sorted({k for fld in per_stock.values() for k in fld.keys()})
    rows = {k: {fld: per_stock.get(fld, {}).get(k) for fld in fields} for k in keys}
    df = pd.DataFrame.from_dict(rows, orient="index")
    return df if not df.empty else None


def download_and_save(stock_code: str, start_date: str) -> tuple[str, int]:
    df = _fetch_kline(stock_code, start_date)
    if df is None or len(df) == 0:
        return stock_code, 0

    float_shares = _get_float_shares(stock_code)
    rows = []
    for idx, row in df.iterrows():
        idx_str = str(idx)
        if len(idx_str) < 8:
            continue
        trade_date = f"{idx_str[:4]}-{idx_str[4:6]}-{idx_str[6:8]}"
        vol = int(row["volume"]) if pd.notna(row["volume"]) else 0
        # volume=手；float_shares=股。换手率% = (手×100) / 流通股本股 × 100，与第 4 课 QMT/TDX 采集同一口径
        vol_shares = vol * 100
        turnover = round(vol_shares / float_shares * 100, 4) if float_shares > 0 and vol > 0 else None
        rows.append((
            stock_code, trade_date,
            float(row["open"]) if pd.notna(row["open"]) else None,
            float(row["high"]) if pd.notna(row["high"]) else None,
            float(row["low"]) if pd.notna(row["low"]) else None,
            float(row["close"]) if pd.notna(row["close"]) else None,
            vol,
            float(row["amount"]) if pd.notna(row["amount"]) else None,
            turnover,
        ))
    if rows:
        with _WRITE_LOCK:
            execute_many(INSERT_SQL, rows)
    return stock_code, len(rows)


def qfq_eta(pending: int) -> tuple[int, int]:
    """按茅台整段覆盖实测约 1.1 秒/只、4 线程，给出分钟区间。"""
    if pending <= 0:
        return 0, 0
    sec = pending * 1.1 / NUM_WORKERS
    eta_min = max(1, int(sec * 0.6 / 60))
    eta_max = max(eta_min, int(sec * 1.4 / 60))
    return eta_min, eta_max


def qfq_gap(stock_count: int) -> dict:
    """库里有日K、但还没记前复权水位的股票数。水位表还没有时视为全部未刷。"""
    marked = 0
    try:
        rows = execute_query("SELECT COUNT(*) AS n FROM trade_stock_qfq_mark")
        marked = int((rows[0] or {}).get("n") or 0) if rows else 0
    except Exception:
        marked = 0
    pending = max(0, int(stock_count) - marked)
    eta_min, eta_max = qfq_eta(pending)
    return {
        "stocks": int(stock_count),
        "marked": marked,
        "pending": pending,
        "ready": pending == 0 and int(stock_count) > 0,
        "eta_min": eta_min,
        "eta_max": eta_max,
    }


def _sync_one(code: str, span: dict[str, str] | None, marked: bool,
              prev_ex: date | None, cutoff: str, force_full: bool) -> tuple[str, int, str]:
    """返回 (代码, 写入行数, full|incr|skip|pending)。整段重刷成功后才记水位。

    日更遇到「库里已有K线、但还没刷过前复权」时直接跳过，避免把新的前复权接到旧的后复权后面。
    这些股票留给全量更新。
    """
    if not force_full and not marked and span:
        return code, 0, "pending"
    latest_ex = _latest_ex_date(code)
    if force_full or _need_full(marked, prev_ex, latest_ex):
        start = span["min"] if span else DATA_START
        _, n = download_and_save(code, start)
        if n <= 0:
            raise RuntimeError(f"{code} 前复权日K为空")
        _save_mark(code, latest_ex)
        return code, n, "full"
    if span and span["max"] >= cutoff:
        return code, 0, "skip"
    start = span["max"] if span else DATA_START
    _, n = download_and_save(code, start)
    return code, n, "incr"


def _full_codes() -> list[str]:
    """库里已有的全部代码，再并上当前沪深A股（新股还没入库的也要拉）。"""
    rows = execute_query("SELECT DISTINCT stock_code FROM trade_stock_daily")
    codes = {r["stock_code"] for r in rows if r.get("stock_code")}
    codes.update(_market_codes())
    return sorted(codes)


def sync_daily(
    on_progress: Callable[..., None] | None = None,
    should_stop: Callable[[], bool] | None = None,
    codes: list[str] | None = None,
    force_full: bool = False,
) -> dict:
    """同步前复权日K。

    日更（force_full=False）：已刷过的股票，除权日没变就增量，有新除权则整段覆盖。
    还没刷过、库里又已有历史的，跳过，不混写。
    全量（force_full=True）：每只都从最早入库日整段覆盖，并记下水位。
    """
    def progress(**kw):
        if on_progress:
            on_progress(**kw)

    _ensure_qfq_schema()
    if is_tdx():
        from data_sync import tdx_source
        ready = tdx_source.probe()
        if not ready["ok"]:
            raise RuntimeError(ready["message"])
    all_codes = codes if codes is not None else _market_codes()
    spans = _load_spans()
    marks = _load_marks()
    cutoff = date.today().strftime("%Y%m%d")
    total = len(all_codes)
    if force_full:
        opening = f"日K 全量前复权 {total} 只，从各股最早入库日整段覆盖"
    else:
        opening = f"日K 共 {total} 只；已刷过的增量或除权重刷，未刷过的跳过"
    progress(
        current=0, total=total,
        message=opening,
        success=0, failed=0, rows_written=0,
    )
    if total == 0:
        return {"success": 0, "failed": 0, "rows_written": 0, "total": 0, "full": 0, "skipped": 0}

    success = failed = rows_written = done = 0
    full_n = incr_n = skip_n = pending_n = 0
    stopped = False
    for i in range(0, total, CHUNK):
        if should_stop and should_stop():
            stopped = True
            break
        chunk = all_codes[i:i + CHUNK]

        def _worker(code: str):
            return _sync_one(
                code, spans.get(code), code in marks, marks.get(code), cutoff, force_full,
            )

        with ThreadPoolExecutor(max_workers=NUM_WORKERS) as pool:
            futures = {pool.submit(_worker, code): code for code in chunk}
            for fut in as_completed(futures):
                code = futures[fut]
                try:
                    _, n, mode = fut.result()
                    rows_written += max(n, 0)
                    if mode == "full":
                        full_n += 1
                    elif mode == "incr":
                        incr_n += 1
                    elif mode == "pending":
                        pending_n += 1
                    else:
                        skip_n += 1
                    if mode != "pending":
                        success += 1
                    label = {
                        "full": "前复权重刷", "incr": "增量",
                        "skip": "已齐", "pending": "待全量",
                    }[mode]
                except Exception as exc:
                    failed += 1
                    label = "失败"
                    print(f"[日K] {code} 失败: {exc}", flush=True)
                done += 1
                progress(
                    current=done, total=total,
                    success=success, failed=failed, rows_written=rows_written,
                    message=f"日K {label} {code} ({done}/{total})",
                )
    if not stopped:
        progress(
            current=done, total=total,
            success=success, failed=failed, rows_written=rows_written,
            message=(
                f"日K 完成 重刷 {full_n} 只，增量 {incr_n} 只，已齐 {skip_n} 只，"
                f"待全量 {pending_n} 只，失败 {failed} 只"
            ),
        )
    return {
        "success": success, "failed": failed, "rows_written": rows_written,
        "total": total, "full": full_n, "skipped": skip_n, "pending": pending_n,
    }


def sync_qfq_full(
    on_progress: Callable[..., None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> dict:
    """把库里已有日K全部整段换成前复权。系统页「全量更新」走这里。"""
    return sync_daily(
        on_progress=on_progress,
        should_stop=should_stop,
        codes=_full_codes(),
        force_full=True,
    )
