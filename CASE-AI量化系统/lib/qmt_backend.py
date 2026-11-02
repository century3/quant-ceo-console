# -*- coding: utf-8 -*-
# Author：@跟陈博士学AI
# 当前工具：WUCAI_QMT = qmt | qmt_shared | miniqmt | tdx
"""
  qmt        大 QMT HTTP 桥，历史数据 + 盘中行情 + 下单
  qmt_shared 大 QMT 共享内存桥，同上
  miniqmt    极简模式 xtquant，同上
  tdx     通达信，只有历史日线/财务；不能下单、没有盘中 tick
"""
from __future__ import annotations

import os
from pathlib import Path

_MINI = ("miniqmt", "xtquant", "mini_qmt")
_TDX = ("tdx", "mootdx", "tongdaxin")
_SHARED = ("qmt_shared", "qmt-shared", "shared", "shared_memory", "qmt_ipc")

LABEL = {
    "qmt": "QMT",
    "qmt_shared": "QMT共享内存",
    "miniqmt": "miniQMT",
    "tdx": "通达信",
}

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent if _HERE.name == "lib" else _HERE


def current() -> str:
    """返回 qmt / qmt_shared / miniqmt / tdx。没设环境变量时默认 qmt。"""
    val = os.environ.get("WUCAI_QMT", "qmt").strip().lower()
    if val in _MINI:
        return "miniqmt"
    if val in _TDX:
        return "tdx"
    if val in _SHARED:
        return "qmt_shared"
    return "qmt"


def choices() -> list[dict]:
    return [
        {
            "id": "tdx",
            "label": "通达信",
            "data": "本机 vipdoc 前复权日线 / 财务，入库 MySQL",
            "trade": "不能下单",
            "can_trade": False,
            "note": "只能做研究和模拟盘。没有资金、持仓、下单接口，实盘无法自动成交；请改用 QMT / miniQMT，或按信号到券商客户端手动下单。",
        },
        {
            "id": "qmt",
            "label": "QMT",
            "data": "QMT HTTP 桥：前复权日线 + 盘中行情",
            "trade": "可实盘下单",
            "can_trade": True,
            "note": "需要 QMT 已登录，并在 QMT 里跑 CY_QMT_API（本机 15588）。",
        },
        {
            "id": "qmt_shared",
            "label": "QMT共享内存",
            "data": "共享内存：前复权日线 + 财务 + 盘中行情",
            "trade": "可实盘下单",
            "can_trade": True,
            "note": "需要 QMT 已登录并运行本项目 lib/qmt_gateway_loader.py；账户功能还需配置 ACCOUNT_ID。",
        },
        {
            "id": "miniqmt",
            "label": "miniQMT",
            "data": "xtquant：前复权日线 + 盘中行情",
            "trade": "可实盘下单",
            "can_trade": True,
            "note": "需要本机 miniQMT 运行，并在 .env 填 QMT_PATH / ACCOUNT_ID。",
        },
    ]


def set_current(backend: str) -> str:
    """进程内切换 WUCAI_QMT，并写回 .env。"""
    raw = (backend or "").strip().lower()
    if raw in _MINI:
        b = "miniqmt"
    elif raw in _TDX:
        b = "tdx"
    elif raw in _SHARED:
        b = "qmt_shared"
    elif raw == "qmt":
        b = "qmt"
    else:
        raise ValueError("WUCAI_QMT 只接受 qmt / qmt_shared / miniqmt / tdx")
    os.environ["WUCAI_QMT"] = b
    _persist_env("WUCAI_QMT", b)
    return b


def _env_file() -> Path:
    try:
        from lib.paths import ENV_FILE
        return ENV_FILE
    except Exception:
        return _ROOT / ".env"


def _persist_env(key: str, value: str) -> None:
    path = _env_file()
    raw = path.read_text(encoding="utf-8") if path.exists() else ""
    nl = "\r\n" if "\r\n" in raw else "\n"
    lines = raw.splitlines()
    out = []
    found = False
    prefix = f"{key}="
    for line in lines:
        body = line.strip()
        if body.startswith(prefix):
            out.append(f"{key}={value}")
            found = True
        else:
            out.append(line)
    if not found:
        if out and out[-1] != "":
            out.append("")
        out.append(f"{key}={value}")
    path.write_text(nl.join(out) + nl, encoding="utf-8")


def connect_tip() -> str:
    if current() != "qmt_shared":
        return ""
    from lib.qmt_shared import connect_tip as shared_tip
    return shared_tip()


def probe() -> dict:
    """当前工具通不通。供健康检查和切换后提示。"""
    b = current()
    if b == "tdx":
        vipdoc = Path(tdx_path()) / "vipdoc"
        day_file = vipdoc / "sh" / "lday" / "sh600519.day"
        ok = vipdoc.is_dir() and day_file.exists()
        if not vipdoc.is_dir():
            msg = f"找不到 vipdoc: {tdx_path()}"
        elif not day_file.exists():
            msg = "未找到日线文件"
        else:
            msg = f"本地 vipdoc {tdx_path()}"
        return {"backend": b, "ok": ok, "stale": False, "message": msg}
    if b == "miniqmt":
        try:
            from xtquant import xtdata
            xtdata.connect()
            return {"backend": b, "ok": True, "stale": False, "message": "miniQMT 已连接"}
        except Exception as e:
            return {"backend": b, "ok": False, "stale": False, "message": str(e)[:160]}
    if b == "qmt_shared":
        from lib.qmt_shared import connect_tip, ping
        p = ping()
        ok = bool(p.get("ok"))
        return {
            "backend": b, "ok": ok, "stale": False,
            "message": "QMT共享内存已连接" if ok else (p.get("message") or "QMT共享内存未连通"),
            "tip": "" if ok else connect_tip(),
        }
    import requests
    base = os.environ.get("QMT_BASE_URL", "http://127.0.0.1:15588").rstrip("/")
    token = os.environ.get("QMT_TOKEN", "learning_ai_with_dr_chen")
    try:
        r = requests.get(
            f"{base}/api/sys/python_version",
            headers={"X-Token": token}, timeout=5,
            proxies={"http": None, "https": None},
        )
        r.raise_for_status()
        return {"backend": b, "ok": True, "stale": False, "message": f"QMT HTTP 已连接 {base}"}
    except Exception as e:
        return {"backend": b, "ok": False, "stale": False, "message": str(e)[:160]}


def label() -> str:
    return LABEL[current()]


def is_tdx() -> bool:
    return current() == "tdx"


def is_miniqmt() -> bool:
    return current() == "miniqmt"


def can_trade() -> bool:
    return current() in ("qmt", "qmt_shared", "miniqmt")


def can_live_quote() -> bool:
    return current() in ("qmt", "qmt_shared", "miniqmt")


def tdx_path() -> str:
    return os.environ.get("TDX_PATH", r"D:\new_tdx").strip() or r"D:\new_tdx"


def snapshot() -> dict:
    """给页面和健康检查用的一组字段。"""
    b = current()
    return {
        "backend": b,
        "backend_label": LABEL[b],
        "can_trade": b in ("qmt", "qmt_shared", "miniqmt"),
        "can_live_quote": b in ("qmt", "qmt_shared", "miniqmt"),
        "is_tdx": b == "tdx",
        "is_miniqmt": b == "miniqmt",
        "tdx_path": tdx_path() if b == "tdx" else "",
        "qmt_backend": LABEL[b],
        "qmt_is_mini": b == "miniqmt",
    }


def vipdoc_dir() -> Path:
    return Path(tdx_path()) / "vipdoc"
