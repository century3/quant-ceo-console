# -*- coding: utf-8 -*-
# Author：@跟陈博士学AI
# CASE-AI 量化系统: 定时调度器（实盘时段启停 + 可选数据增量）
"""
TradingScheduler -- 按 A 股交易时段自动启停 +（可选）板块日更脚本

单独进程的原因:
    - Web (app.py) 与调度解耦：浏览器关了不影响调度，调度挂了不影响 Web。
    - APScheduler + cron。

3 个 cron job（周一到周五，时区 Asia/Shanghai）:
    08:30   job_data_refresh   -> .env 中 CASE_A_BOARD_DATA_PREP_DIR/run_daily.py
    09:30   job_start_engine   -> 启动模拟盘 LiveSimRunner
    15:00   job_stop_engine    -> 收盘时停止主循环

主循环状态在 outputs/live_state.json，进程重启可从最近一次 state 恢复。

用法:
    python scheduler.py
    python scheduler.py --simulate
    python scheduler.py --job data | engine | all
"""
from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

if sys.platform == "win32":
    os.environ.setdefault("PYTHONUTF8", "1")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS_DIR))

from dotenv import load_dotenv
from lib.paths import ENV_FILE, PROJECT_ROOT, setup_sys_path

load_dotenv(ENV_FILE)

setup_sys_path()

from lib.live_simulator import LiveSimRunner, merge_watch_codes


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("trading-scheduler")


def _case_a_prep_dir() -> Path | None:
    """CASE-A「板块数据准备」目录（内含 run_daily.py），由 .env CASE_A_BOARD_DATA_PREP_DIR 指定."""
    raw = (os.environ.get("CASE_A_BOARD_DATA_PREP_DIR") or "").strip()
    if not raw:
        return None
    return Path(raw).expanduser()


_CASE_A_DIR = _case_a_prep_dir()
CASE_A_RUN_DAILY = (_CASE_A_DIR / "run_daily.py") if _CASE_A_DIR else None


# ============================================================
# 任务
# ============================================================

def job_data_refresh():
    """08:30: 在项目目录下运行 run_daily.py（若未配置 CASE_A_BOARD_DATA_PREP_DIR 则跳过并打日志）"""
    log.info("[JOB] 数据增量 - 触发")
    if not _CASE_A_DIR:
        log.error(
            "未配置环境变量 CASE_A_BOARD_DATA_PREP_DIR（指向内含 run_daily.py 的目录）；已跳过。"
        )
        return
    if not CASE_A_RUN_DAILY or not CASE_A_RUN_DAILY.exists():
        log.error("找不到 run_daily.py: %s", CASE_A_RUN_DAILY)
        return
    ret = subprocess.run([sys.executable, str(CASE_A_RUN_DAILY)], cwd=str(_CASE_A_DIR))
    log.info("[JOB] 数据增量 - 完成 (returncode=%s)", ret.returncode)


def job_start_engine():
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
    log.info(f"[JOB] 启动主循环 - 完成: {msg.splitlines()[0] if msg else 'OK'}")


def job_stop_engine():
    """15:00: 收盘时停, 尾盘已走完; 停的是盘后空转"""
    log.info("[JOB] 停止主循环 - 触发")
    sim = LiveSimRunner()
    if not sim.status().get("running"):
        log.info("[JOB] 主循环未在运行, 跳过")
        return
    msg = sim.stop()
    log.info("[JOB] 停止主循环 - 完成: %s", msg or "OK")


# ============================================================
# 主入口
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="实盘工作台调度器（与 app.py 共用 .env）",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument(
        "--simulate", action="store_true",
        help="模拟模式: 立刻把每个 job 跑一次然后退出",
    )
    parser.add_argument(
        "--job",
        choices=["data", "engine", "all"],
        default="all",
        help="只注册某一组 job: data | engine | all（默认）",
    )
    args = parser.parse_args()

    if args.simulate:
        log.info("=" * 60)
        log.info("[SIMULATE] 模拟模式")
        log.info("=" * 60)
        if args.job in ("data", "all"):
            job_data_refresh()
        if args.job in ("engine", "all"):
            job_start_engine()
            job_stop_engine()
        return

    sched = BlockingScheduler(timezone="Asia/Shanghai")

    if args.job in ("data", "all"):
        sched.add_job(
            job_data_refresh,
            id="data_refresh",
            name="08:30 数据增量",
            trigger=CronTrigger(hour=8, minute=30, day_of_week="mon-fri",
                                timezone="Asia/Shanghai"),
        )
        log.info("[REG] 08:30 CASE_A_BOARD_DATA_PREP_DIR -> run_daily.py")

    if args.job in ("engine", "all"):
        sched.add_job(
            job_start_engine,
            id="start_engine",
            name="09:30 启动主循环",
            trigger=CronTrigger(hour=9, minute=30, day_of_week="mon-fri",
                                timezone="Asia/Shanghai"),
        )
        sched.add_job(
            job_stop_engine,
            id="stop_engine",
            name="15:00 停止主循环",
            trigger=CronTrigger(hour=15, minute=0, day_of_week="mon-fri",
                                timezone="Asia/Shanghai"),
        )
        log.info("[REG] 09:30 / 15:00 引擎启停")

    log.info("=" * 60)
    log.info("[BOOT] 调度器前台运行（Ctrl+C 退出） cwd=%s", PROJECT_ROOT)
    log.info("       当前时间 %s", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    log.info("=" * 60)

    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("[EXIT] 调度器已退出")


if __name__ == "__main__":
    main()
