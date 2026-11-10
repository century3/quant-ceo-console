# -*- coding: utf-8 -*-
# Author：@跟陈博士学AI
# CASE-AI量化系统 路径常量（见 lib/ 旁目录树；唯一 .env 见 ENV_FILE）
import sys
from pathlib import Path

# 工作台根目录
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 同级子目录
LIVE_TRADING_DIR    = PROJECT_ROOT / "live_trading"
ALERTING_DIR        = PROJECT_ROOT / "alerting"
DRAGON_STRATEGY_DIR = PROJECT_ROOT / "dragon_strategy"
MORNING_BRIEF_DIR   = PROJECT_ROOT / "morning_brief"
PAGES_DIR           = PROJECT_ROOT / "pages"
LIB_DIR             = PROJECT_ROOT / "lib"
CONFIG_DIR          = PROJECT_ROOT / "config"
OUTPUTS_DIR         = PROJECT_ROOT / "outputs"
DATA_DIR            = PROJECT_ROOT / "data"

# 关键产出
OUTPUTS_LIVE_STATE = OUTPUTS_DIR / "live_state.json"
OUTPUTS_RESEARCH   = MORNING_BRIEF_DIR / "outputs" / "reports"
OUTPUTS_EVOLVE_REGISTRY = OUTPUTS_DIR / "strategy_registry.json"
OUTPUTS_DATA_SYNC = OUTPUTS_DIR / "data_sync_state.json"
OUTPUTS_SCHEDULER_LOCK = OUTPUTS_DIR / "scheduler.lock"

# 全项目唯一环境变量文件
ENV_FILE = PROJECT_ROOT / ".env"


def setup_sys_path() -> None:
    """把工作台子模块加入 sys.path, 让 import 直接用模块名."""
    for p in (PROJECT_ROOT, LIVE_TRADING_DIR, ALERTING_DIR,
              DRAGON_STRATEGY_DIR, LIB_DIR):
        sp = str(p)
        if p.exists() and sp not in sys.path:
            sys.path.insert(0, sp)


# 确保 outputs 与 data 目录存在
OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)
