# -*- coding: utf-8 -*-
# Author：@跟陈博士学AI
# 后台下载线程：系统状态页点按钮 / scheduler.py 到点走同一套
from __future__ import annotations

import threading
import time
import traceback

from data_sync import job_store
from data_sync.qmt_http import ping
from lib.qmt_backend import is_tdx, label as tool_label, tdx_source_mode
from data_sync.sync_calendar import sync_calendar
from data_sync.sync_daily import sync_daily, sync_qfq_full
from data_sync.sync_financial import sync_financial
from data_sync.sync_macro import sync_macro, sync_rate
from data_sync.sync_news import sync_news
from data_sync.sync_report import sync_report
from data_sync.sync_sector import sync_sector
from data_sync.sync_status import sync_status
from data_sync.tasks import FAST_CATCHUP, SCHEDULE_JOBS, TASK_META, TASK_ORDER, VALID_TASKS

_FUNCS = {
    "macro": sync_macro,
    "rate": sync_rate,
    "calendar": sync_calendar,
    "status": sync_status,
    "daily": sync_daily,
    "qfq_full": sync_qfq_full,
    "financial": sync_financial,
    "sector": sync_sector,
    "news": sync_news,
    "report": sync_report,
}


def _on_progress(**kwargs):
    job_store.update(**kwargs)


_VIPDOC_TASKS = ("daily", "status", "qfq_full")


def _refresh_official_vipdoc(steps: list[str]) -> None:
    """官网模式下，日K/资料同步前先拉通达信完整包。不用打开客户端。"""
    if not is_tdx() or tdx_source_mode() != "official":
        return
    if not any(name in _VIPDOC_TASKS for name in steps):
        return
    from data_sync.tdx_source import client as tdx_client
    job_store.update(phase="vipdoc", current=0, total=0, message="检查通达信官网日线包")
    tdx_client(online=False).ensure_vipdoc(
        on_progress=_on_progress, should_stop=job_store.should_stop,
    )


def _ensure_market_source(meta: dict) -> None:
    """日K / 财务 / 资料需要当前工具就绪。"""
    if not meta.get("needs_qmt"):
        return
    if is_tdx():
        from data_sync.tdx_source import probe
        ready = probe()
        if not ready["ok"]:
            raise RuntimeError(ready["message"])
        return
    qmt = ping()
    if not qmt["ok"]:
        from lib.qmt_backend import current
        strategy = "qmt_gateway_loader.py" if current() == "qmt_shared" else "CY_QMT_API.py"
        raise RuntimeError(
            f"{tool_label()} 未连接 ({qmt.get('base')})，请先在 QMT 运行 {strategy}"
        )


def _run_one(name: str) -> dict:
    job_store.update(phase=name, current=0, total=0, message=f"开始 {TASK_META[name]['label']}")
    return _FUNCS[name](on_progress=_on_progress, should_stop=job_store.should_stop)


def _worker(task: str) -> None:
    try:
        steps = list(TASK_ORDER) if task == "all" else [task]
        _refresh_official_vipdoc(steps)
        for name in steps:
            if job_store.should_stop():
                job_store.finish(cancelled=True)
                return
            meta = TASK_META[name]
            if meta["needs_qmt"]:
                _ensure_market_source(meta)
            _run_one(name)
        if job_store.should_stop():
            job_store.finish(cancelled=True)
            return
        job_store.finish()
    except Exception as e:
        traceback.print_exc()
        job_store.finish(error=str(e)[:200])


def start_task(task: str) -> dict:
    if task not in VALID_TASKS:
        return {"ok": False, "message": f"未知任务: {task}"}
    reason = job_store.try_start(task)
    if reason:
        return {"ok": False, "message": reason}
    threading.Thread(target=_worker, args=(task,), daemon=True, name=f"data-sync-{task}").start()
    return {"ok": True, "message": f"已启动: {task}", "job": job_store.snapshot()}


def stop_task() -> dict:
    if not job_store.request_stop():
        return {"ok": False, "message": "当前没有运行中的任务"}
    return {"ok": True, "message": "已请求停止", "job": job_store.snapshot()}


def _names_from_token(token: str) -> list[str]:
    if token in TASK_META:
        return [token]
    if token == "all":
        return list(TASK_ORDER)
    for job in SCHEDULE_JOBS:
        if job["id"] == token:
            return list(job["names"])
    return []


def run_startup_catchup() -> None:
    """app.py 起来后：续跑被掐断的任务，并补下落后的快表。新闻/研报只在被掐断时续。"""
    from data_sync.inventory import build_inventory

    wanted: list[str] = []
    task, phase = job_store.take_interrupted()
    resume = _names_from_token(task or "")
    if phase and phase in TASK_META:
        if phase in resume:
            resume = resume[resume.index(phase):]
        else:
            resume = [phase] + [n for n in resume if n != phase]
    wanted.extend(resume)

    inv = build_inventory()
    for t in inv.get("tasks") or []:
        name = t.get("name")
        if name not in TASK_META or name in wanted:
            continue
        if t.get("fresh") not in ("warn", "stale", "idle"):
            continue
        if name in FAST_CATCHUP:
            wanted.append(name)

    ordered = [n for n in TASK_ORDER if n in wanted]
    if not ordered:
        print("[scheduler] 各表已齐，无需补下", flush=True)
        return
    print(f"[scheduler] 补下未完成: {' -> '.join(ordered)}", flush=True)
    run_scheduled(ordered, label="startup_catchup")


def run_scheduled(names: tuple[str, ...] | list[str], label: str) -> None:
    """调度器调用：占槽后按顺序跑。页面正在同步则等到空闲（最多 45 分钟）。"""
    deadline = time.time() + 45 * 60
    reason = job_store.try_start(label)
    while reason:
        if time.time() >= deadline:
            raise RuntimeError(reason)
        time.sleep(15)
        reason = job_store.try_start(label)
    try:
        _refresh_official_vipdoc(list(names))
        for name in names:
            if job_store.should_stop():
                job_store.finish(cancelled=True)
                return
            meta = TASK_META[name]
            if meta["needs_qmt"]:
                _ensure_market_source(meta)
            _run_one(name)
        if job_store.should_stop():
            job_store.finish(cancelled=True)
            return
        job_store.finish()
    except Exception as e:
        traceback.print_exc()
        job_store.finish(error=str(e)[:200])
        raise
