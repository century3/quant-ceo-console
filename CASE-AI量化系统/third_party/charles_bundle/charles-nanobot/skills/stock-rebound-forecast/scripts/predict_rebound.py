# -*- coding: utf-8 -*-
"""控制台入口，转调本机技能里的美股反弹概率预测。预测脚本依赖 pytorch 环境。"""
import subprocess
import sys
from pathlib import Path

TARGET = Path(r"C:\Users\ADMIN\.claude\skills\stock-rebound-forecast\scripts\predict_rebound.py")
PY = Path(r"C:\Users\ADMIN\.conda\envs\pytorch\python.exe")

if not TARGET.is_file():
    raise SystemExit(f"找不到反弹预测技能脚本: {TARGET}")
if not PY.is_file():
    raise SystemExit(f"找不到预测用的 Python: {PY}")

raise SystemExit(subprocess.call([str(PY), "-X", "utf8", str(TARGET), *sys.argv[1:]]))
