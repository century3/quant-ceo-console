# -*- coding: utf-8 -*-
# Author：@跟陈博士学AI
# 系统状态路由 -- REST
"""
GET  /api/system/health          -- 健康检查
POST /api/system/tool            -- 切换 WUCAI_QMT {backend, tdx_source?}
GET  /api/system/data/inventory  -- 四张表概况 + 新鲜度
GET  /api/system/data/status     -- 当前/上次下载进度
POST /api/system/data/start      -- 启动同步 {task}
POST /api/system/data/stop       -- 请求停止
"""

from __future__ import annotations
import os
import sys

from fastapi import APIRouter
from pydantic import BaseModel

from lib.paths import setup_sys_path, OUTPUTS_LIVE_STATE, OUTPUTS_RESEARCH
setup_sys_path()

from data_sync.inventory import build_inventory
from data_sync.job_store import snapshot
from data_sync.runner import start_task, stop_task
from data_sync.scheduler_runtime import snapshot as scheduler_snapshot
from lib.qmt_backend import (
    can_trade, choices, current, is_miniqmt, is_tdx, label as tool_label,
    probe as tool_probe, set_current, set_tdx_source, snapshot as tool_snapshot,
    tdx_path, tdx_source_choices, tdx_source_mode,
)

router = APIRouter()


class DataStartBody(BaseModel):
    task: str = "all"


class ToolBody(BaseModel):
    backend: str
    tdx_source: str | None = None


def _reset_after_switch():
    from data_sync.tdx_source import reset_client
    from data_sync.qmt_shared import reset_client as reset_shared_client
    from lib.qmt_shared import reset_client as reset_live_shared
    from lib.backtest_data import clear_kline_cache
    from routes.live import reset_real_trader
    reset_client()
    reset_shared_client()
    reset_live_shared()
    reset_real_trader()
    clear_kline_cache()


def _health_payload() -> dict:
    rows = []
    rows.append({"item": "Python 版本", "value": sys.version.split()[0], "status": "OK"})
    p = tool_probe()
    tool = tool_snapshot()
    cap = "历史数据 + 盘中行情 + 下单" if can_trade() else "仅历史数据，不能下单、没有盘中行情"
    rows.append({
        "item": "当前工具",
        "value": f"{tool_label()}（WUCAI_QMT={current()}）· {cap}",
        "status": "OK" if p["ok"] else "ERROR",
    })

    if is_tdx():
        src = tdx_source_mode()
        src_label = "官网日线包" if src == "official" else "本机 vipdoc"
        rows.append({
            "item": "通达信日线来源",
            "value": f"{src_label}（WUCAI_TDX_SOURCE={src}）",
            "status": "OK",
        })
        rows.append({
            "item": "通达信目录",
            "value": tdx_path() + (" · 只当存放 vipdoc，不必安装客户端" if src == "official" else ""),
            "status": "OK" if p["ok"] else "ERROR",
        })
        rows.append({
            "item": "本地日线",
            "value": p["message"],
            "status": "WARN" if p.get("stale") else ("OK" if p["ok"] else "ERROR"),
        })
        rows.append({
            "item": "实盘下单",
            "value": "通达信模式不可用，请改用 QMT、QMT共享内存或 miniQMT",
            "status": "WARN",
        })
    elif is_miniqmt():
        rows.append({
            "item": "行情连接",
            "value": p["message"],
            "status": "OK" if p["ok"] else "ERROR",
        })
        if os.environ.get("QMT_PATH") and os.environ.get("ACCOUNT_ID"):
            rows.append({"item": "QMT_PATH", "value": os.environ["QMT_PATH"], "status": "OK"})
        else:
            rows.append({
                "item": "QMT_PATH",
                "value": "miniQMT 需在 .env 填 QMT_PATH / ACCOUNT_ID",
                "status": "WARN",
            })
    elif current() == "qmt_shared":
        rows.append({
            "item": "行情连接",
            "value": p["message"],
            "status": "OK" if p["ok"] else "ERROR",
        })
        if not p["ok"] and p.get("tip"):
            rows.append({
                "item": "TIPS",
                "value": p["tip"],
                "status": "WARN",
            })
        rows.append({
            "item": "实盘下单",
            "value": "走 qmt_gateway_loader（QMT共享内存），不使用15588端口",
            "status": "OK" if p["ok"] else "WARN",
        })
    else:
        rows.append({
            "item": "行情连接",
            "value": p["message"],
            "status": "OK" if p["ok"] else "ERROR",
        })
        rows.append({
            "item": "实盘下单",
            "value": "走 CY_QMT_API（QMT HTTP），不用配 QMT_PATH",
            "status": "OK" if p["ok"] else "WARN",
        })

    if os.environ.get("DASHSCOPE_API_KEY"):
        rows.append({"item": "DASHSCOPE_API_KEY", "value": "已配置", "status": "OK"})
    else:
        rows.append({"item": "DASHSCOPE_API_KEY",
                     "value": "未配置 (Charles 不可用)", "status": "WARN"})

    if OUTPUTS_LIVE_STATE.exists():
        import json
        try:
            s = json.loads(OUTPUTS_LIVE_STATE.read_text(encoding="utf-8"))
            ts = (s.get("_updated_at") or "").replace("T", " ")
            running = s.get("trading_status") == "RUNNING"
            sim_text = "运行中" if running else "已停止"
            value = f"{sim_text}，最后更新 {ts}" if ts else sim_text
            rows.append({"item": "模拟盘状态", "value": value,
                         "status": "OK" if running else "WARN"})
        except Exception as e:
            rows.append({"item": "模拟盘状态",
                         "value": f"状态文件读失败: {e}", "status": "ERROR"})
    else:
        rows.append({"item": "模拟盘状态",
                     "value": "尚未生成，启动模拟盘后会自动创建", "status": "WARN"})

    sch = scheduler_snapshot()
    rows.append({
        "item": "调度器",
        "value": sch.get("message") or ("运行中" if sch.get("running") else "未启动"),
        "status": "OK" if sch.get("running") else "WARN",
    })

    if OUTPUTS_RESEARCH.exists():
        n = len(list(OUTPUTS_RESEARCH.glob("morning_brief_*.html")))
        rows.append({"item": "晨会分析 HTML", "value": f"{n} 份",
                     "status": "OK" if n > 0 else "WARN"})
    else:
        rows.append({"item": "晨会分析 HTML",
                     "value": "目录不存在", "status": "WARN"})

    return {
        "rows": rows,
        "tool": tool,
        "probe": p,
        "choices": choices(),
        "tdx_source": tdx_source_mode() if is_tdx() else "",
        "tdx_source_choices": tdx_source_choices() if is_tdx() else [],
    }


@router.get("/health")
def health():
    return _health_payload()


@router.get("/probe")
def tool_probe_api():
    """页面横幅用的快速探活，共享内存只试一次。"""
    from lib.qmt_backend import connect_tip, probe, snapshot
    p = probe(quick=True)
    snap = snapshot()
    return {
        **snap,
        "ok": bool(p.get("ok")),
        "message": p.get("message") or "",
        "tip": p.get("tip") or (connect_tip() if not p.get("ok") else ""),
    }


@router.post("/tool")
def set_tool(body: ToolBody):
    try:
        set_current(body.backend)
        if body.backend.strip().lower() in ("tdx", "mootdx", "tongdaxin"):
            set_tdx_source(body.tdx_source or tdx_source_mode())
    except ValueError as e:
        return {"ok": False, "message": str(e)}
    _reset_after_switch()
    payload = _health_payload()
    p = payload["probe"]
    return {
        "ok": True,
        "connected": bool(p.get("ok")),
        "message": p.get("message") or "",
        "tip": p.get("tip") or "",
        "tool": payload["tool"],
        "rows": payload["rows"],
        "choices": payload["choices"],
        "tdx_source": payload.get("tdx_source") or "",
        "tdx_source_choices": payload.get("tdx_source_choices") or [],
        "probe": p,
    }


@router.get("/data/inventory")
def data_inventory():
    return build_inventory()


@router.get("/data/status")
def data_status():
    return snapshot()


@router.post("/data/start")
def data_start(body: DataStartBody):
    return start_task(body.task)


@router.post("/data/stop")
def data_stop():
    return stop_task()
