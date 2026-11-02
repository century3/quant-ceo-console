# -*- coding: utf-8 -*-
"""CEO 控制台入口，转调本机 skills 里的美股日线采集。"""
import runpy
import sys
from pathlib import Path

TARGET = Path(r"C:\Users\ADMIN\.claude\skills\us-stock-collect\scripts\collect_us_daily.py")

if not TARGET.is_file():
    raise SystemExit(f"找不到美股采集技能脚本: {TARGET}")

sys.argv[0] = str(TARGET)
runpy.run_path(str(TARGET), run_name="__main__")
