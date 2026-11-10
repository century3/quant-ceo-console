# -*- coding: utf-8 -*-
# Author：@跟陈博士学AI
# 表概况 + 新鲜度：7 张业务表 + 花名册 / 板块
from __future__ import annotations

from datetime import date, datetime

from dragon_strategy.db_config import execute_query

from data_sync.job_store import snapshot
from data_sync.qmt_http import last_trading_date, ping
from data_sync.sync_financial import latest_due_period
from data_sync.scheduler_runtime import snapshot as scheduler_snapshot
from data_sync.sync_daily import qfq_gap
from data_sync.tasks import CORE_NAMES, TASKS, timeline
from lib.qmt_backend import is_tdx, snapshot as tool_snapshot


def _fmt_date(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d")
    if isinstance(v, date):
        return v.isoformat()
    return str(v)[:10]


def _table_stats(table: str, date_field: str | None, entity_field: str | None) -> dict:
    date_select = f", MIN({date_field}) AS min_date, MAX({date_field}) AS max_date" if date_field else ""
    try:
        rows = execute_query(f"SELECT COUNT(*) AS row_count {date_select} FROM {table}")
    except Exception as e:
        return {
            "row_count": 0, "stock_count": 0,
            "min_date": None, "max_date": None,
            "error": str(e)[:80],
        }
    row = rows[0] if rows else {}
    stock_count = 0
    if entity_field:
        try:
            sc = execute_query(f"SELECT COUNT(DISTINCT {entity_field}) AS n FROM {table}")
            stock_count = int((sc[0] or {}).get("n") or 0) if sc else 0
        except Exception:
            stock_count = 0
    return {
        "row_count": int(row.get("row_count") or 0),
        "stock_count": stock_count,
        "min_date": _fmt_date(row.get("min_date")) if date_field else None,
        "max_date": _fmt_date(row.get("max_date")) if date_field else None,
        "error": None,
    }


def _lag_vs(max_s: str | None, target: date | None) -> tuple[str, int | None]:
    if target is None:
        return "unknown", None
    if not max_s:
        return "idle", None
    max_d = date.fromisoformat(max_s[:10])
    lag = (target - max_d).days
    if lag <= 0:
        return "fresh", 0
    if lag <= 7:
        return "warn", lag
    return "stale", lag


def _freshness(task: dict, stats: dict, *, last_trade: date | None,
               due_period: str | None, job: dict) -> tuple[str, int | None]:
    name = task["name"]
    if job.get("running") and job.get("phase") == name:
        return "running", None
    if stats.get("error"):
        return "stale", None
    if int(stats.get("row_count") or 0) == 0:
        return "idle", None

    mode = task.get("fresh_mode") or "trade_day"
    mx = stats.get("max_date")

    if mode == "count":
        return ("fresh", None) if int(stats.get("stock_count") or 0) > 0 else ("idle", None)

    if mode == "report":
        if not due_period:
            return "unknown", None
        raw = (mx or "").replace("-", "")
        if len(raw) != 8:
            return "idle", None
        if raw >= due_period:
            return "fresh", 0
        return "warn", None

    if mode == "month":
        today = date.today()
        first_this = today.replace(day=1)
        # 上个月月末：本月1号的前一天
        prev_month_end = date.fromordinal(first_this.toordinal() - 1)
        return _lag_vs(mx, prev_month_end)

    if mode == "calendar":
        if not mx:
            return "idle", None
        max_d = date.fromisoformat(mx[:10])
        if max_d >= date.today():
            return "fresh", 0
        return "warn", (date.today() - max_d).days

    return _lag_vs(mx, last_trade)


def _timeline_with_next(sched: dict) -> list[dict]:
    next_by_id = {j["id"]: j.get("next_run") for j in (sched.get("jobs") or [])}
    rows = timeline()
    for row in rows:
        row["next_run"] = next_by_id.get(row.get("id"))
    return rows


def build_inventory() -> dict:
    from lib.qmt_backend import connect_tip
    tool = tool_snapshot()
    vipdoc_stale = False
    vipdoc_last = None
    source_tip = ""
    if is_tdx():
        from data_sync.tdx_source import probe as tdx_probe
        tdx = tdx_probe()
        source_ok = tdx["ok"]
        source_message = tdx["message"]
        last_trade = None
        if tdx.get("last_date"):
            last_trade = date.fromisoformat(tdx["last_date"])
        vipdoc_stale = bool(tdx.get("stale"))
        vipdoc_last = tdx.get("last_date")
    else:
        qmt = ping()
        source_ok = qmt["ok"]
        source_message = qmt.get("message") or (
            f"{tool['backend_label']} 已连接 {qmt.get('base')}" if qmt["ok"] else ""
        )
        last_trade = None
        if qmt["ok"]:
            try:
                last_trade = last_trading_date()
            except Exception as e:
                source_ok = False
                source_message = str(e)[:120]
        if not source_ok:
            source_tip = connect_tip()
            source_message = source_message or "QMT共享内存未连通"

    due = latest_due_period()
    job = snapshot()
    sched = scheduler_snapshot()

    tasks = []
    for meta in TASKS:
        stats = _table_stats(meta["table"], meta["date_field"], meta.get("entity_field"))
        fresh, lag = _freshness(
            meta, stats,
            last_trade=last_trade,
            due_period=due,
            job=job,
        )
        expected = None
        if meta.get("fresh_mode") == "report" and due:
            expected = f"{due[:4]}-{due[4:6]}-{due[6:8]}"
        elif meta.get("fresh_mode") == "trade_day" and last_trade:
            expected = last_trade.isoformat()
        tasks.append({
            **meta,
            **stats,
            "fresh": fresh,
            "lag_days": lag,
            "expected_date": expected,
        })

    daily = next((t for t in tasks if t["name"] == "daily"), None)
    qfq = qfq_gap(int((daily or {}).get("stock_count") or 0))

    return {
        "qfq": qfq,
        "tool": tool,
        "source_ok": source_ok,
        "source_message": source_message,
        "source_tip": source_tip,
        "vipdoc_stale": vipdoc_stale,
        "vipdoc_last_date": vipdoc_last,
        "last_trade_date": last_trade.isoformat() if last_trade else None,
        "job": job,
        "tasks": tasks,
        "core": [t for t in tasks if t["name"] in CORE_NAMES],
        "extra": [t for t in tasks if t["name"] not in CORE_NAMES],
        "scheduler": sched,
        "timeline": _timeline_with_next(sched),
    }
