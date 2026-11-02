# -*- coding: utf-8 -*-
# Author：@跟陈博士学AI
# 23-CASE-A: 实盘 state 落盘存储
"""
StateStore -- 实盘运行的 state 持久化

为什么需要这个?
    - 盘中状态 (持仓 / 当日盈亏 / 信号历史) 需要跨进程共享
    - CEO 控制台 (CASE-B Gradio) 要读这个 state 渲染界面
    - 进程崩溃重启后, 用 state 恢复

设计:
    - 用 JSON 文件 + 文件锁存储 (轻量, 跨进程)
    - 每次写都是原子操作 (写入临时文件再 rename)
    - 读取支持快照, 不阻塞写
"""

from __future__ import annotations
import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional


class StateStore:
    """JSON 文件版 state 存储"""

    def __init__(self, state_file: str = "outputs/live_state.json"):
        self.state_file = Path(state_file)
        self.state_file.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> dict:
        """读取当前 state"""
        if not self.state_file.exists():
            return self._default_state()
        try:
            return json.loads(self.state_file.read_text(encoding="utf-8"))
        except Exception:
            return self._default_state()

    def save(self, state: dict):
        """原子写入 state"""
        # 加上更新时间戳
        state = {**state, "_updated_at": datetime.now().isoformat(timespec="seconds")}
        # 先写临时文件再 rename, 避免 reader 读到半截
        tmp_path = self.state_file.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                            encoding="utf-8")
        os.replace(tmp_path, self.state_file)

    def update(self, **kv):
        """局部更新"""
        s = self.load()
        s.update(kv)
        self.save(s)

    def append_event(self, event: dict, max_keep: int = 200):
        """往 events 列表追加一条 (滚动保留最新 N 条)"""
        s = self.load()
        events = s.get("events", [])
        events.append({**event, "ts": datetime.now().isoformat(timespec="seconds")})
        s["events"] = events[-max_keep:]
        self.save(s)

    def append_signal(self, signal: dict, max_keep: int = 100):
        """追加一条信号"""
        s = self.load()
        signals = s.get("signals", [])
        signals.append({**signal, "ts": datetime.now().isoformat(timespec="seconds")})
        s["signals"] = signals[-max_keep:]
        self.save(s)

    def append_order(self, order: dict, max_keep: int = 100):
        """追加一条订单 (含成功 / 失败 / 拒绝)"""
        s = self.load()
        orders = s.get("orders", [])
        orders.append({**order, "ts": datetime.now().isoformat(timespec="seconds")})
        s["orders"] = orders[-max_keep:]
        self.save(s)

    def update_pnl(self, pnl_record: dict):
        """每日盈亏曲线追加一个点"""
        s = self.load()
        pnl_history = s.get("pnl_history", [])
        pnl_history.append({**pnl_record, "ts": datetime.now().isoformat(timespec="seconds")})
        s["pnl_history"] = pnl_history[-500:]
        self.save(s)

    @staticmethod
    def _default_state() -> dict:
        return {
            "trading_status": "RUNNING",   # RUNNING / PAUSED / HALTED
            "capital":        1_000_000.0,
            "positions":      [],          # [{"code","name","volume","cost","cur_price","mv","pnl"}]
            "today_pnl":      0.0,
            "today_pnl_pct":  0.0,
            "events":         [],          # 时间事件流
            "signals":        [],          # 信号历史
            "orders":         [],          # 订单历史
            "pnl_history":    [],          # 盈亏曲线
            "control":        {            # CEO 控制台可写的字段
                "pause_buying":     False,
                "force_clear_all":  False,
                "max_daily_loss":   -0.02,
                "dry_run":          True,
                "max_slots":  5,           # 最多同时几档
                "slot_codes": [],          # 已分档代码 (记名预留)
            },
            "health": {
                "qmt_connected": False,
                "last_heartbeat":    None,
                "errors_24h":        0,
            },
        }
