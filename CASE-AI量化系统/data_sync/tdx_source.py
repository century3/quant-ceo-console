# -*- coding: utf-8 -*-
# Author：@跟陈博士学AI
# 通达信就绪检查 + 本地日线日期 + 股票资料
"""
日 K 只读 vipdoc/*.day。

WUCAI_TDX_SOURCE=official（默认）：同步前从通达信官网拉完整包，不用打开客户端。
WUCAI_TDX_SOURCE=local：只读本机文件，落后时要在通达信里做盘后数据下载。
"""
from __future__ import annotations

import struct
from datetime import date
from pathlib import Path

from lib.qmt_backend import tdx_path

_client = None
_client_online = None


def _dir() -> Path:
    return Path(tdx_path())


def probe() -> dict:
    """通达信目录和本地日线是否可用。不连 QMT。"""
    from lib.qmt_backend import tdx_source_mode

    root = _dir()
    vipdoc = root / "vipdoc"
    day_file = vipdoc / "sh" / "lday" / "sh600519.day"
    last = last_vipdoc_date()
    stale = False
    if last is not None:
        stale = (date.today() - last).days > 4
    official = tdx_source_mode() == "official"
    ok = vipdoc.is_dir() and day_file.exists()
    if official and not ok:
        ok = True
        stale = True
        if not vipdoc.is_dir():
            message = f"将在同步时从官网写入 {root / 'vipdoc'}"
        else:
            message = "尚未下载官网日线包，同步时自动拉取"
    elif not vipdoc.is_dir():
        message = f"找不到 vipdoc: {root}"
    elif not day_file.exists():
        message = "未找到日线文件"
    elif stale:
        message = (
            f"本地日线 {last.isoformat()}（落后，同步时从官网更新）"
            if official else f"本地日线 {last.isoformat()}（落后）"
        )
    else:
        message = f"本地日线 {last.isoformat() if last else '-'}"
    return {
        "ok": ok,
        "stale": stale,
        "path": str(root),
        "last_date": last.isoformat() if last else None,
        "tdx_source": tdx_source_mode(),
        "message": message,
    }


def last_vipdoc_date() -> date | None:
    """上证指数或茅台本地日线的最后一天。"""
    vipdoc = _dir() / "vipdoc"
    for rel in ("sh/lday/sh000001.day", "sh/lday/sh600519.day"):
        path = vipdoc / rel
        d = _day_file_last(path)
        if d:
            return d
    return None


def _day_file_last(path: Path) -> date | None:
    if not path.exists() or path.stat().st_size < 32:
        return None
    rec = 32
    n = path.stat().st_size // rec
    with path.open("rb") as f:
        f.seek((n - 1) * rec)
        raw = f.read(4)
    if len(raw) < 4:
        return None
    ymd = struct.unpack("<I", raw)[0]
    s = f"{ymd:08d}"
    try:
        return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
    except ValueError:
        return None


def expected_trade_date() -> date | None:
    """tdx 模式下，日K新鲜度对齐本地 vipdoc，而不是日历今天。"""
    return last_vipdoc_date()


def client(online: bool = True):
    """进程内复用一个 TDXClient。已在线的客户端也能当离线用。"""
    global _client, _client_online
    if _client is not None and (_client_online or not online):
        return _client
    if _client is not None:
        try:
            _client.close()
        except Exception:
            pass
        _client = None
    from data_sync.CY_TDX_Client import TDXClient
    from lib.qmt_backend import tdx_source_mode
    _client = TDXClient(tdxdir=tdx_path(), online=online, source=tdx_source_mode())
    _client_online = online
    return _client


def reset_client():
    """切换工具源时丢掉缓存的通达信客户端。"""
    global _client, _client_online
    old = _client
    _client = None
    _client_online = None
    if old is not None:
        old.close()


def stock_list() -> list[str]:
    return client(online=False).get_stock_list_in_sector("沪深A股")["stocks"]


def latest_ex_date(stock_code: str) -> date | None:
    body = client(online=True).get_xdxr(stock_code)
    latest = None
    for rec in body.get("data") or []:
        if int(rec.get("category") or 0) != 1:
            continue
        try:
            d = date(int(rec["year"]), int(rec["month"]), int(rec["day"]))
        except (KeyError, TypeError, ValueError):
            continue
        if latest is None or d > latest:
            latest = d
    return latest


def stock_profiles() -> list[tuple]:
    """(stock_code, name, list_date, total_shares, float_shares)，股本单位：股。"""
    return client(online=False).get_stock_profiles()
