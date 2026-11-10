# -*- coding: utf-8 -*-
# Author：@跟陈博士学AI
# 调度器运行时：app.py 进程内默认拉起；也可 python scheduler.py 单独挂
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from datetime import datetime
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from lib.live_simulator import LiveSimRunner, merge_watch_codes
from lib.paths import OUTPUTS_SCHEDULER_LOCK
from data_sync.runner import run_scheduled
from data_sync.tasks import ENGINE_JOBS, SCHEDULE_JOBS

log = logging.getLogger("trading-scheduler")

_sched: BackgroundScheduler | None = None
_mode: str = ""


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _pid_alive(pid: int) -> bool:
    """进程是否还在。Windows 上 os.kill(pid, 0) 会 TerminateProcess，不能用来探活。"""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _read_lock() -> dict[str, Any] | None:
    if not OUTPUTS_SCHEDULER_LOCK.exists():
        return None
    try:
        data = json.loads(OUTPUTS_SCHEDULER_LOCK.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    pid = int(data.get("pid") or 0)
    if not _pid_alive(pid):
        return None
    return data


def _write_lock(mode: str) -> None:
    OUTPUTS_SCHEDULER_LOCK.write_text(
        json.dumps({
            "pid": os.getpid(),
            "mode": mode,
            "started_at": _now(),
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _release_lock() -> None:
    data = None
    if OUTPUTS_SCHEDULER_LOCK.exists():
        try:
            data = json.loads(OUTPUTS_SCHEDULER_LOCK.read_text(encoding="utf-8"))
        except Exception:
            data = None
    if data and int(data.get("pid") or 0) != os.getpid():
        return
    if OUTPUTS_SCHEDULER_LOCK.exists():
        OUTPUTS_SCHEDULER_LOCK.unlink()


def _cron_trigger(job: dict) -> CronTrigger:
    kwargs = {
        "hour": job["hour"],
        "minute": job["minute"],
        "timezone": "Asia/Shanghai",
    }
    dow = job.get("day_of_week") or "*"
    if dow != "*":
        kwargs["day_of_week"] = dow
    return CronTrigger(**kwargs)


def job_data(job: dict) -> None:
    names = job["names"]
    log.info("[JOB] %s - 触发 %s", job["action"], "+".join(names))
    try:
        run_scheduled(names, label=job["id"])
        log.info("[JOB] %s - 完成", job["action"])
    except Exception as e:
        log.error("[JOB] %s - 失败: %s", job["action"], e)


def job_start_engine() -> None:
    log.info("[JOB] 启动主循环 - 触发")
    sim = LiveSimRunner()
    if sim.status().get("running"):
        log.info("[JOB] 主循环已在运行, 跳过")
        return
    watch = merge_watch_codes([])
    if not watch:
        log.warning("[JOB] 监控池为空, 不启动")
        return
    msg = sim.start(watch_stocks=watch, dry_run=True, cycle_seconds=60)
    log.info("[JOB] 启动主循环 - 完成: %s", msg.splitlines()[0] if msg else "OK")


def job_stop_engine() -> None:
    log.info("[JOB] 停止主循环 - 触发")
    sim = LiveSimRunner()
    if not sim.status().get("running"):
        log.info("[JOB] 主循环未在运行, 跳过")
        return
    msg = sim.stop()
    log.info("[JOB] 停止主循环 - 完成: %s", msg or "OK")


def _engine_fn(job_id: str):
    if job_id == "engine_start":
        return job_start_engine
    if job_id == "engine_stop":
        return job_stop_engine
    raise KeyError(job_id)


def register_jobs(sched, which: str = "all") -> None:
    """往 APScheduler 注册数据任务 / 引擎启停。"""
    if which in ("data", "all"):
        for job in SCHEDULE_JOBS:
            sched.add_job(
                job_data,
                kwargs={"job": job},
                id=job["id"],
                name=f"{job['time']} {job['action']}",
                trigger=_cron_trigger(job),
            )
            log.info("[REG] %s %s -> %s", job["time"], job["action"], "+".join(job["names"]))
    if which in ("engine", "all"):
        for job in ENGINE_JOBS:
            sched.add_job(
                _engine_fn(job["id"]),
                id=job["id"],
                name=f"{job['time']} {job['action']}",
                trigger=_cron_trigger(job),
            )
            log.info("[REG] %s %s", job["time"], job["action"])


def _jobs_from_sched(sched) -> list[dict]:
    rows = []
    for j in sched.get_jobs():
        nxt = j.next_run_time
        rows.append({
            "id": j.id,
            "name": j.name,
            "next_run": nxt.strftime("%Y-%m-%d %H:%M:%S") if nxt else None,
        })
    return rows


def snapshot() -> dict[str, Any]:
    """系统状态页用：调度器是否在跑、下次触发。"""
    global _sched, _mode
    if _sched is not None and _sched.running:
        lock = _read_lock() or {}
        return {
            "running": True,
            "mode": _mode or "app",
            "pid": os.getpid(),
            "started_at": lock.get("started_at"),
            "jobs": _jobs_from_sched(_sched),
            "message": "调度器已随 app.py 启动",
        }
    lock = _read_lock()
    if lock:
        mode = lock.get("mode") or "standalone"
        msg = (
            "调度器已随 app.py 启动" if mode == "app"
            else f"调度器在独立进程运行 pid={lock.get('pid')}"
        )
        return {
            "running": True,
            "mode": mode,
            "pid": lock.get("pid"),
            "started_at": lock.get("started_at"),
            "jobs": [],
            "message": msg,
        }
    return {
        "running": False,
        "mode": "",
        "pid": None,
        "started_at": None,
        "jobs": [],
        "message": "调度器未启动",
    }


def start_embedded() -> dict[str, Any]:
    """app.py 启动时调用：本进程后台线程跑 cron。"""
    global _sched, _mode
    if _sched is not None and _sched.running:
        return snapshot()
    lock = _read_lock()
    if lock and int(lock.get("pid") or 0) != os.getpid():
        log.warning("[BOOT] 调度器已在 pid=%s 运行, 本进程不再重复拉起", lock.get("pid"))
        return snapshot()
    _sched = BackgroundScheduler(timezone="Asia/Shanghai")
    # 只挂数据任务。15:00 停主循环会掐掉随后的美股夜盘，模拟盘仍由 app.py 自己拉起并跨过 A 股收盘。
    register_jobs(_sched, "data")
    _sched.start()
    _mode = "app"
    _write_lock("app")
    print(f"[scheduler] 已随 app.py 启动 pid={os.getpid()}", flush=True)
    log.info("[BOOT] 调度器已随 app.py 启动 pid=%s", os.getpid())
    threading.Thread(target=_boot_catchup, daemon=True, name="data-catchup").start()
    return snapshot()


def _boot_catchup() -> None:
    """等进程站稳后补下被掐断 / 落后的表。"""
    time.sleep(2)
    try:
        from data_sync.runner import run_startup_catchup
        run_startup_catchup()
    except Exception as e:
        log.error("[BOOT] 补下失败: %s", e)
        print(f"[scheduler] 补下失败: {e}", flush=True)


def stop_embedded() -> None:
    global _sched, _mode
    sched = _sched
    _sched = None
    _mode = ""
    if sched is not None:
        try:
            sched.shutdown(wait=False)
        except Exception:
            pass
    _release_lock()


def run_blocking(which: str = "all") -> None:
    """python scheduler.py：不启 Web，前台阻塞跑。"""
    lock = _read_lock()
    if lock:
        raise RuntimeError(
            f"调度器已在运行 pid={lock.get('pid')} mode={lock.get('mode')}，不要重复开"
        )
    sched = BlockingScheduler(timezone="Asia/Shanghai")
    register_jobs(sched, which)
    _write_lock("standalone")
    log.info("[BOOT] 调度器独立进程运行 pid=%s（Ctrl+C 退出）", os.getpid())
    threading.Thread(target=_boot_catchup, daemon=True, name="data-catchup").start()
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("[EXIT] 调度器已退出")
    finally:
        _release_lock()


def run_simulate(which: str = "all") -> None:
    """立刻把每个 job 跑一次然后退出。"""
    if which in ("data", "all"):
        for job in SCHEDULE_JOBS:
            job_data(job)
    if which in ("engine", "all"):
        job_start_engine()
        job_stop_engine()
