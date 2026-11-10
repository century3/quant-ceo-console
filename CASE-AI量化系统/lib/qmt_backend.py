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
_TDX_SRC_LABEL = {
    "official": "官网日线包",
    "local": "本机 vipdoc",
}

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
            "data": "官网日线包（默认）或本机 vipdoc，前复权入库 MySQL",
            "trade": "不能下单",
            "can_trade": False,
            "note": "只能做研究和模拟盘。默认从通达信官网拉日线完整包，不用安装或打开通达信；也可切到本机 vipdoc。没有资金、持仓、下单接口，实盘请改用 QMT / miniQMT。",
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


def probe(*, quick: bool = False) -> dict:
    """当前工具通不通。供健康检查和切换后提示。quick=True 时共享内存只探一次。"""
    b = current()
    if b == "tdx":
        from data_sync.tdx_source import probe as tdx_probe
        p = tdx_probe()
        return {
            "backend": b,
            "ok": bool(p.get("ok")),
            "stale": bool(p.get("stale")),
            "message": p.get("message") or "",
            "tip": "",
        }
    if b == "miniqmt":
        try:
            from xtquant import xtdata
            xtdata.connect()
            return {"backend": b, "ok": True, "stale": False, "message": "miniQMT 已连接"}
        except Exception as e:
            return {"backend": b, "ok": False, "stale": False, "message": str(e)[:160]}
    if b == "qmt_shared":
        from lib.qmt_shared import connect_tip, ping
        p = ping(attempts=1, timeout=3) if quick else ping()
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


def is_shared() -> bool:
    return current() == "qmt_shared"


def can_trade() -> bool:
    return current() in ("qmt", "qmt_shared", "miniqmt")


def can_live_quote() -> bool:
    return current() in ("qmt", "qmt_shared", "miniqmt")


def tdx_path() -> str:
    return os.environ.get("TDX_PATH", r"D:\new_tdx").strip() or r"D:\new_tdx"


def tdx_source_mode() -> str:
    """official=官网 hsjday.zip（默认）；local=只读本机 vipdoc。"""
    val = os.environ.get("WUCAI_TDX_SOURCE", "official").strip().lower()
    if val in ("local", "vipdoc", "tdx_local"):
        return "local"
    return "official"


def tdx_source_choices() -> list[dict]:
    return [
        {
            "id": "official",
            "label": "官网日线包",
            "desc": "读官网 _hsjdayinfo.js，下载 hsjday.zip 写进 vipdoc。不用安装或打开通达信。",
        },
        {
            "id": "local",
            "label": "本机 vipdoc",
            "desc": "只读通达信已经下载好的盘后文件。软件需开着定时下载，或自己点盘后数据下载。",
        },
    ]


def set_tdx_source(mode: str) -> str:
    raw = (mode or "").strip().lower()
    if raw in ("local", "vipdoc", "tdx_local"):
        src = "local"
    elif raw in ("official", "js", "hsjday", ""):
        src = "official"
    else:
        raise ValueError("WUCAI_TDX_SOURCE 只接受 official / local")
    os.environ["WUCAI_TDX_SOURCE"] = src
    _persist_env("WUCAI_TDX_SOURCE", src)
    return src


def snapshot() -> dict:
    """给页面和健康检查用的一组字段。"""
    b = current()
    src = tdx_source_mode() if b == "tdx" else ""
    return {
        "backend": b,
        "backend_label": LABEL[b],
        "can_trade": b in ("qmt", "qmt_shared", "miniqmt"),
        "can_live_quote": b in ("qmt", "qmt_shared", "miniqmt"),
        "is_tdx": b == "tdx",
        "is_miniqmt": b == "miniqmt",
        "is_shared": b == "qmt_shared",
        "tdx_path": tdx_path() if b == "tdx" else "",
        "tdx_source": src,
        "tdx_source_label": _TDX_SRC_LABEL.get(src, ""),
        "tdx_source_choices": tdx_source_choices() if b == "tdx" else [],
        "qmt_backend": LABEL[b],
        "qmt_is_mini": b == "miniqmt",
    }


def vipdoc_dir() -> Path:
    return Path(tdx_path()) / "vipdoc"
