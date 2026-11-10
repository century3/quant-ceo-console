# -*- coding: utf-8 -*-
# Author：@跟陈博士学AI
# 下载任务进度：内存 + outputs/data_sync_state.json，给系统状态页轮询
from __future__ import annotations

import json
import threading
from datetime import datetime
from typing import Any

from lib.paths import OUTPUTS_DATA_SYNC

_lock = threading.Lock()
_cancel = False
_state: dict[str, Any] = {}
_interrupted_task: str | None = None
_interrupted_phase: str | None = None


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _blank(task: str = "") -> dict[str, Any]:
    return {
        "running": False,
        "task": task,
        "phase": "",
        "current": 0,
        "total": 0,
        "success": 0,
        "failed": 0,
        "rows_written": 0,
        "message": "",
        "started_at": None,
        "finished_at": None,
        "error": None,
        "cancelled": False,
    }


def _persist_unlocked() -> None:
    OUTPUTS_DATA_SYNC.write_text(
        json.dumps(_state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load() -> None:
    """进程启动时读上次进度。上次还在跑就被掐断的，记下 task/phase 给补下。"""
    global _state, _interrupted_task, _interrupted_phase
    if not OUTPUTS_DATA_SYNC.exists():
        _state = _blank()
        return
    try:
        data = json.loads(OUTPUTS_DATA_SYNC.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            _state = _blank()
            return
        if data.get("running") and (data.get("phase") or data.get("task")):
            _interrupted_task = str(data.get("task") or "")
            _interrupted_phase = str(data.get("phase") or "")
        data["running"] = False
        _state = data
    except Exception:
        _state = _blank()


def take_interrupted() -> tuple[str | None, str | None]:
    """取出并清空上次被掐断的任务，只消费一次。"""
    global _interrupted_task, _interrupted_phase
    task, phase = _interrupted_task, _interrupted_phase
    _interrupted_task = None
    _interrupted_phase = None
    return task, phase


def snapshot() -> dict[str, Any]:
    with _lock:
        return dict(_state)


def try_start(task: str) -> str | None:
    """占住单任务槽。已在跑则返回原因，否则 None。"""
    global _cancel, _state
    with _lock:
        if _state.get("running"):
            return f"已有任务在运行: {_state.get('phase') or _state.get('task')}"
        _cancel = False
        _state = _blank(task)
        _state["running"] = True
        _state["started_at"] = _now()
        _state["message"] = "准备启动"
        _persist_unlocked()
        return None


def update(**kwargs: Any) -> None:
    with _lock:
        _state.update(kwargs)
        _persist_unlocked()


def should_stop() -> bool:
    with _lock:
        return _cancel


def request_stop() -> bool:
    """置取消标志。没有在跑的任务则返回 False。"""
    global _cancel
    with _lock:
        if not _state.get("running"):
            return False
        _cancel = True
        _state["message"] = "正在停止..."
        _persist_unlocked()
        return True


def finish(cancelled: bool = False, error: str | None = None) -> None:
    with _lock:
        _state["running"] = False
        _state["finished_at"] = _now()
        _state["cancelled"] = cancelled
        _state["error"] = error
        if error:
            _state["message"] = error
        elif cancelled:
            _state["message"] = "已停止"
        else:
            _state["message"] = "已完成"
        _persist_unlocked()


load()
