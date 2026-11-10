# -*- coding: utf-8 -*-
# Author：@跟陈博士学AI
# 看板任务：7 张业务表全部可同步，另两张（花名册 / 板块）跟在后面
from __future__ import annotations

TASKS = [
    {
        "name": "daily",
        "label": "A股日K",
        "table": "trade_stock_daily",
        "cron": "工作日 15:40",
        "hint": "QMT 直拉；通达信默认官网日线包，可切本机 vipdoc",
        "needs_qmt": True,
        "can_sync": True,
        "date_field": "trade_date",
        "entity_field": "stock_code",
        "fresh_mode": "trade_day",
    },
    {
        "name": "news",
        "label": "新闻事件",
        "table": "trade_stock_news",
        "cron": "工作日 16:30",
        "hint": "全市场较慢",
        "needs_qmt": False,
        "can_sync": True,
        "date_field": "published_at",
        "entity_field": "stock_code",
        "fresh_mode": "trade_day",
    },
    {
        "name": "financial",
        "label": "财务报表",
        "table": "trade_stock_financial",
        "cron": "工作日 15:40",
        "hint": "QMT 按报告期，或通达信季报",
        "needs_qmt": True,
        "can_sync": True,
        "date_field": "report_date",
        "entity_field": "stock_code",
        "fresh_mode": "report",
    },
    {
        "name": "macro",
        "label": "宏观指标",
        "table": "trade_macro_indicator",
        "cron": "周日 21:00",
        "hint": "月度指标",
        "needs_qmt": False,
        "can_sync": True,
        "date_field": "indicator_date",
        "entity_field": None,
        "fresh_mode": "month",
    },
    {
        "name": "rate",
        "label": "日频利率",
        "table": "trade_rate_daily",
        "cron": "工作日 18:00",
        "hint": "中美十年国债",
        "needs_qmt": False,
        "can_sync": True,
        "date_field": "rate_date",
        "entity_field": None,
        "fresh_mode": "trade_day",
    },
    {
        "name": "report",
        "label": "研报预期",
        "table": "trade_report_consensus",
        "cron": "工作日 22:30",
        "hint": "全市场较慢",
        "needs_qmt": False,
        "can_sync": True,
        "date_field": "report_date",
        "entity_field": "stock_code",
        "fresh_mode": "trade_day",
    },
    {
        "name": "calendar",
        "label": "财经日历",
        "table": "trade_calendar_event",
        "cron": "每日 07:00",
        "hint": "前7后30天",
        "needs_qmt": False,
        "can_sync": True,
        "date_field": "event_date",
        "entity_field": None,
        "fresh_mode": "calendar",
    },
    {
        "name": "status",
        "label": "股票资料",
        "table": "trade_stock_status",
        "cron": "工作日 15:40",
        "hint": "QMT 含申万；通达信只补名称股本",
        "needs_qmt": True,
        "can_sync": True,
        "date_field": None,
        "entity_field": "stock_code",
        "fresh_mode": "count",
    },
    {
        "name": "sector",
        "label": "板块指数",
        "table": "trade_sector_daily",
        "cron": "工作日 15:40",
        "hint": "板块用",
        "needs_qmt": False,
        "can_sync": True,
        "date_field": "trade_date",
        "entity_field": "sector_name",
        "fresh_mode": "trade_day",
    },
]

# 全部同步：先快（宏观/利率/日历），再 QMT（资料/日K/财务/板块），最后全市场慢任务
TASK_ORDER = (
    "macro", "rate", "calendar",
    "status", "daily", "financial", "sector",
    "news", "report",
)
VALID_TASKS = TASK_ORDER + ("all", "qfq_full")
TASK_META = {t["name"]: t for t in TASKS}
TASK_META["qfq_full"] = {
    "name": "qfq_full",
    "label": "日K前复权全量",
    "needs_qmt": True,
}
CORE_NAMES = ("daily", "news", "financial", "macro", "rate", "report", "calendar")
# 启动补下：快任务落后就跑；新闻/研报只在上次被掐断时续跑
FAST_CATCHUP = ("macro", "rate", "calendar", "status", "daily", "financial", "sector")

# 调度真源：scheduler.py 注册 + 系统状态页时间轴都读这里
SCHEDULE_JOBS = [
    {
        "id": "data_calendar",
        "time": "07:00",
        "action": "财经日历",
        "desc": "每天刷前7后30天，给晨会用",
        "names": ("calendar",),
        "hour": 7,
        "minute": 0,
        "day_of_week": "*",
    },
    {
        "id": "data_close",
        "time": "15:40",
        "action": "收盘增量",
        "desc": "日K(前复权) → 资料 → 板块 → 财务；QMT 或通达信（官网包/本机 vipdoc）",
        "names": ("daily", "status", "sector", "financial"),
        "hour": 15,
        "minute": 40,
        "day_of_week": "mon-fri",
    },
    {
        "id": "data_news",
        "time": "16:30",
        "action": "新闻事件",
        "desc": "盘后一档，当日已采过的跳过",
        "names": ("news",),
        "hour": 16,
        "minute": 30,
        "day_of_week": "mon-fri",
    },
    {
        "id": "data_rate",
        "time": "18:00",
        "action": "日频利率",
        "desc": "中美十年国债",
        "names": ("rate",),
        "hour": 18,
        "minute": 0,
        "day_of_week": "mon-fri",
    },
    {
        "id": "data_report",
        "time": "22:30",
        "action": "研报预期",
        "desc": "全市场较慢，近7天已采过的跳过",
        "names": ("report",),
        "hour": 22,
        "minute": 30,
        "day_of_week": "mon-fri",
    },
    {
        "id": "data_macro",
        "time": "周日 21:00",
        "action": "宏观指标",
        "desc": "月度数，每周日跑一次",
        "names": ("macro",),
        "hour": 21,
        "minute": 0,
        "day_of_week": "sun",
    },
]

ENGINE_JOBS = [
    {
        "id": "engine_start",
        "time": "09:30",
        "action": "启动主循环",
        "desc": "开盘启动 dry-run 模拟盘，每分钟巡检一次",
        "hour": 9,
        "minute": 30,
        "day_of_week": "mon-fri",
    },
    {
        "id": "engine_stop",
        "time": "15:00",
        "action": "停止主循环",
        "desc": "收盘停主循环，避免盘后空转",
        "hour": 15,
        "minute": 0,
        "day_of_week": "mon-fri",
    },
]


def timeline() -> list[dict]:
    """系统状态页时间轴：只列数据任务。主循环跨过 A 股收盘，美股夜盘还要跑。"""
    rows = []
    for job in SCHEDULE_JOBS:
        rows.append({
            "id": job["id"],
            "time": job["time"],
            "action": job["action"],
            "desc": job["desc"],
            "sunday": job["time"].startswith("周日"),
            "sort": (job["hour"], job["minute"]),
        })
    rows.sort(key=lambda r: (r["sunday"], r["sort"]))
    return [{"id": r["id"], "time": r["time"], "action": r["action"], "desc": r["desc"]} for r in rows]
