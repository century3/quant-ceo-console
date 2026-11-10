# -*- coding: utf-8 -*-
# Author：@跟陈博士学AI
# 25-AI量化系统 回测路由
"""
GET  /api/backtest/ping              -- 健康检查 (含 MySQL 是否可连)
GET  /api/backtest/strategies        -- 可选策略列表（strategy_registry）
POST /api/backtest/run               -- 单股 + 单策略回测
POST /api/backtest/recommend         -- 单股 + 多策略评分 (推荐)
POST /api/backtest/recommend_apply   -- 把推荐策略写到 strategies.yaml.per_stock (热加载)

请求示例:
    POST /api/backtest/run
    {"code":"600519.SH","strategy":"macd_1d","start":"2024-01-01","end":"2025-12-31"}

    POST /api/backtest/recommend
    {"code":"600519.SH","start":"2024-01-01","end":"2025-12-31"}

    POST /api/backtest/recommend_apply
    {"code":"600519.SH","strategy":"macd_1d"}    # 写入 strategies.yaml + 通知 _SIM 热加载
"""

from __future__ import annotations
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body

from lib.paths import setup_sys_path
setup_sys_path()

from lib.backtest_data import mysql_available, get_stock_name
from lib.backtest_engine import run_backtest, score_strategies
from lib.strategy_registry import list_strategies, list_groups
from lib.live_simulator import load_strategy_config
from routes.live import _SIM, _set_binding_source  # 复用同一个 LiveSimRunner 单例

router = APIRouter()


def _with_shared_tip(result: Dict[str, Any]) -> Dict[str, Any]:
    if result.get("ok"):
        return result
    from lib.qmt_backend import connect_tip
    tip = connect_tip()
    if not tip:
        return result
    result["tip"] = tip
    msg = result.get("message") or ""
    if "TIPS" not in msg:
        result["message"] = f"{msg}。TIPS：{tip}" if msg else f"TIPS：{tip}"
    return result


# ============================================================
# 工具
# ============================================================

def _normalize_code(code: str) -> str:
    """股票代码归一化: 支持 600519 / 600519.SH / 中文名(唯一命中)."""
    from lib.backtest_data import resolve_stock_query

    raw = (code or "").strip()
    if not raw:
        return ""
    resolved, _name = resolve_stock_query(raw)
    if resolved:
        return resolved
    # 解析失败时仍走旧逻辑 (纯数字补后缀), 便于报错信息带上原始输入
    s = raw.upper().replace(" ", "")
    if "." in s:
        return s
    if s.isdigit() and len(s) == 6:
        if s.startswith(("60", "68", "90", "11")):
            return f"{s}.SH"
        return f"{s}.SZ"
    return s


def _refresh_us_daily(code: str) -> str:
    """美股回测前先把这一只的日 K 写入库。失败则不回测。"""
    if not str(code).upper().endswith(".US"):
        return ""
    from lib.backtest_data import clear_kline_cache
    from lib.us_daily_sync import sync_one_us_daily

    message = sync_one_us_daily(code)
    clear_kline_cache()
    return message


def _resolve_code_or_error(raw: str) -> Dict[str, Any]:
    """解析 code/名称; 成功返回 {ok, code, name}, 失败返回带 candidates 的错误."""
    from lib.backtest_data import resolve_stock_query, search_stocks

    text = (raw or "").strip()
    if not text:
        return {"ok": False, "message": "请填写股票代码或名称 (例: 600519.SH / 贵州茅台 / COHR)"}
    code, name = resolve_stock_query(text)
    if code:
        return {"ok": True, "code": code, "name": name}
    hits = search_stocks(text, limit=8)
    if hits:
        preview = "、".join(f"{h['name']}({h['code']})" for h in hits[:5])
        return {
            "ok": False,
            "message": f"「{text}」匹配到多只股票, 请点选一条: {preview}",
            "candidates": hits,
        }
    return {"ok": False, "message": f"未找到股票「{text}」, 请换代码或更完整的名称"}


# ============================================================
# 端点
# ============================================================

@router.get("/ping")
def backtest_ping():
    """健康检查 + 数据源探活"""
    from lib.qmt_backend import connect_tip, probe
    p = probe()
    return {
        "ok": True,
        "module": "backtest",
        "mysql_available": mysql_available(),
        "strategies": [s["name"] for s in list_strategies()],
        "probe": p,
        "connected": bool(p.get("ok")),
        "tip": p.get("tip") or (connect_tip() if not p.get("ok") else ""),
        "hint": "POST /api/backtest/run {code, strategy, start, end} 单回测; "
                "POST /api/backtest/recommend {code, start, end} 推荐策略",
    }


@router.get("/strategies")
def backtest_strategies():
    """可选策略列表 (按分组), 前端下拉用 -- 复用 registry"""
    return {
        "ok": True,
        "groups": list_groups(),
        "list": list_strategies(),
    }


@router.post("/run")
@router.post("/run/")
def backtest_run(payload: Optional[Dict[str, Any]] = Body(None)):
    """单股 + 单策略回测

    body: {
        "code":     "600519.SH" 或 "600519",
        "strategy": "macd_1d",
        "start":    "YYYY-MM-DD",
        "end":      "YYYY-MM-DD",
        # 可选覆盖默认参数:
        "initial_cash": 1000000,
        "commission":   0.0002,
        "position_pct": 95
    }
    """
    payload = payload or {}
    resolved = _resolve_code_or_error(str(payload.get("code", "")))
    if not resolved.get("ok"):
        return resolved
    code = resolved["code"]
    strategy = str(payload.get("strategy", "")).strip()
    start = str(payload.get("start", "")).strip()
    end = str(payload.get("end", "")).strip()

    if not strategy:
        return {"ok": False, "message": "strategy 不能为空"}
    if not start or not end:
        return {"ok": False, "message": "start / end 不能为空 (YYYY-MM-DD)"}

    # 校验日期格式
    try:
        from datetime import datetime
        datetime.strptime(start, "%Y-%m-%d")
        datetime.strptime(end, "%Y-%m-%d")
    except ValueError:
        return {"ok": False, "message": "日期格式应为 YYYY-MM-DD"}
    if start > end:
        return {"ok": False, "message": "起始日期晚于结束日期"}

    # 可选参数
    initial_cash = payload.get("initial_cash")
    commission = payload.get("commission")
    position_pct = payload.get("position_pct")

    try:
        data_note = _refresh_us_daily(code)
    except Exception as e:
        return {"ok": False, "message": f"日K采集失败，未开始回测: {type(e).__name__}: {e}"}

    try:
        result = run_backtest(
            stock_code=code,
            strategy_name=strategy,
            start_date=start,
            end_date=end,
            initial_cash=initial_cash,
            commission=commission,
            position_pct=position_pct,
        )
    except Exception as e:
        result = {"ok": False, "message": f"{type(e).__name__}: {e}"}
    if data_note and result.get("ok"):
        result["data_note"] = data_note
    return _with_shared_tip(result)


@router.post("/recommend")
@router.post("/recommend/")
def backtest_recommend(payload: Optional[Dict[str, Any]] = Body(None)):
    """对一只股票跑全部策略, 按夏普/卡玛/胜率综合排名给出推荐

    body: {"code":"600519.SH", "start":"2024-01-01", "end":"2025-12-31",
           "candidates": ["macd_1d","ma20_hold",...]  -- 可选, 不传跑全部}
    """
    payload = payload or {}
    resolved = _resolve_code_or_error(str(payload.get("code", "")))
    if not resolved.get("ok"):
        return resolved
    code = resolved["code"]
    start = str(payload.get("start", "")).strip()
    end = str(payload.get("end", "")).strip()
    if not start or not end:
        return {"ok": False, "message": "start / end 不能为空"}

    candidates = payload.get("candidates")
    if candidates and not isinstance(candidates, list):
        return {"ok": False, "message": "candidates 必须是 list"}

    initial_cash = payload.get("initial_cash")
    commission = payload.get("commission")
    position_pct = payload.get("position_pct")

    try:
        data_note = _refresh_us_daily(code)
    except Exception as e:
        return {"ok": False, "message": f"日K采集失败，未开始回测: {type(e).__name__}: {e}"}

    try:
        result = score_strategies(
            stock_code=code,
            start_date=start,
            end_date=end,
            candidates=candidates,
            initial_cash=initial_cash,
            commission=commission,
            position_pct=position_pct,
        )
    except Exception as e:
        result = {"ok": False, "message": f"{type(e).__name__}: {e}"}
    if data_note and result.get("ok"):
        result["data_note"] = data_note
    return _with_shared_tip(result)


@router.post("/recommend_apply")
@router.post("/recommend_apply/")
def backtest_recommend_apply(payload: Optional[Dict[str, Any]] = Body(None)):
    """把推荐结果写入 strategies.yaml.per_stock[code] = strategy, 热加载并重放模拟盘

    body: {"code":"600519.SH", "strategy":"macd_1d"}
    """
    payload = payload or {}
    resolved = _resolve_code_or_error(str(payload.get("code", "")))
    if not resolved.get("ok"):
        return resolved
    code = resolved["code"]
    strategy = str(payload.get("strategy", "")).strip()
    if not strategy:
        return {"ok": False, "message": "strategy 不能为空"}

    valid = {s["name"] for s in list_strategies()}
    if strategy not in valid:
        return {"ok": False, "message": f"未知策略: {strategy}"}

    try:
        cfg = load_strategy_config()
        per = dict(cfg.get("per_stock") or {})
        per[code] = strategy
        default = cfg.get("default", "macd_1d")
        msg = _SIM.apply_strategy_config(default=default, per_stock=per)
        _set_binding_source(code, "sim")
        return {
            "ok": True,
            "message": f"已绑定 {code} -> {strategy}; {msg}",
            "code": code,
            "strategy": strategy,
            "stock_name": get_stock_name(code),
            "per_stock": per,
        }
    except Exception as e:
        return {"ok": False, "message": f"{type(e).__name__}: {e}"}
