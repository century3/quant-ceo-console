# -*- coding: utf-8 -*-
# Author：@跟陈博士学AI
# 独立挂调度器（不启 Web）。平时开 app.py 就会在进程内拉起同一套。
"""
用法:
    python scheduler.py
    python scheduler.py --simulate
    python scheduler.py --job data | engine | all
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

if sys.platform == "win32":
    os.environ.setdefault("PYTHONUTF8", "1")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")

THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS_DIR))

from dotenv import load_dotenv
from lib.paths import ENV_FILE, setup_sys_path

load_dotenv(ENV_FILE, override=True)
setup_sys_path()

from data_sync.scheduler_runtime import run_blocking, run_simulate

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


def main():
    parser = argparse.ArgumentParser(
        description="调度器（日常请直接 python app.py，会自动拉起）",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument(
        "--simulate", action="store_true",
        help="立刻把每个 job 跑一次然后退出",
    )
    parser.add_argument(
        "--job",
        choices=["data", "engine", "all"],
        default="data",
        help="data=只采数（默认，不在 15:00 停模拟盘）| engine | all",
    )
    args = parser.parse_args()

    if args.simulate:
        run_simulate(args.job)
        return
    run_blocking(args.job)


if __name__ == "__main__":
    main()
