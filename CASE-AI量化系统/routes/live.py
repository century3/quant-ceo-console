# -*- coding: utf-8 -*-
# Author：@跟陈博士学AI
# 实盘监控路由 -- REST
"""
GET  /api/live/state                 -- 读 live_state.json
GET  /api/live/sim/status            -- 模拟盘运行状态
POST /api/live/sim/start             -- 启动模拟盘
POST /api/live/sim/stop              -- 停止模拟盘
POST /api/live/sim/reset_positions   -- 按当前股票/策略/档位从起始日重置
POST /api/live/control               -- 修改 control 字段 (pause_buying / force_clear_all 等)
POST /api/live/slots                 -- 改本金 / 档位数 / 分档 / 释放档位
POST /api/live/status                -- 修改 trading_status (PAUSED / RUNNING / HALTED)
GET  /api/live/strategies/registry   -- 列出所有可选策略 (按分组)
GET  /api/live/strategies/config     -- 读当前路由表 (default + per_stock)
POST /api/live/strategies/config     -- 保存路由表 (热加载)
GET  /api/live/watch_merge           -- 合并后的监控列表 (?ui= 额外逗号分隔代码)
GET  /api/live/watch_pool            -- 读监控代码列表 (watch_pool.yaml)
POST /api/live/watch_pool            -- 写监控代码列表
POST /api/live/stock/bind            -- 添加代码 + 绑定策略 (写 watch_pool + strategies.yaml 并热加载)
GET  /api/live/kline                 -- 日 K (弹窗图用, 复用 backtest_data.load_daily_kline)
"""

from __future__ import annotations
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Query

from lib.paths import setup_sys_path, OUTPUTS_LIVE_STATE, OUTPUTS_DIR, PROJECT_ROOT
setup_sys_path()

import yaml as _yaml   # 读写 binding_source.yaml 用

from lib.live_simulator import (
    LiveSimRunner,
    load_strategy_config,
    load_mock_config,
    save_slot_book,
    merge_watch_codes,
    load_watch_pool,
    save_watch_pool,
    is_a_share_trading_day,
    weekday_cn,
)
from lib.strategy_registry import list_groups, list_strategies

router = APIRouter()
_SIM = LiveSimRunner()


@router.get("/ping")
def live_ping():
    """健康检查：浏览器打开 /api/live/ping 可确认当前进程已加载 live 路由 (含 stock/bind)"""
    return {"ok": True, "module": "live", "hint": "绑定接口: POST /api/live/stock/bind"}


def _normalize_stock_code(code: str) -> str:
    """股票代码归一化: 600519 -> 600519.SH; COHR / COHR.US -> COHR.US"""
    import re
    s = (code or "").strip().upper()
    if not s:
        return ""
    if s.endswith(".US") or re.fullmatch(r"[A-Z][A-Z0-9]*(-[A-Z0-9]+)?", s):
        return s if s.endswith(".US") else f"{s}.US"
    if "." in s:
        return s
    if s.isdigit() and len(s) == 6:
        if s.startswith(("60", "68", "90", "11")):
            return f"{s}.SH"
        return f"{s}.SZ"
    return s


@router.get("/kline")
def live_kline(
    code: str = Query("", description="股票代码, 如 600519.SH / 600519"),
    count: int = Query(120, ge=20, le=500, description="最近 N 根日 K"),
):
    """日 K 数据 -- /live 持仓表点代码/名称弹窗用

    复用 lib.backtest_data.load_daily_kline (QMT/miniQMT 前复权), 与回测同源.
    """
    stock_code = _normalize_stock_code(code)
    if not stock_code:
        return {"ok": False, "message": "请提供股票代码"}

    try:
        from lib.backtest_data import load_daily_kline, get_stock_name
        df = load_daily_kline(stock_code, use_cache=False)
    except Exception as e:
        return {
            "ok": False,
            "code": stock_code,
            "message": f"加载 K 线失败: {type(e).__name__}: {e}",
        }

    if df is None or len(df) == 0:
        return {"ok": False, "code": stock_code, "message": f"{stock_code} 无日 K 数据"}

    n = int(count)
    slice_df = df.tail(n)
    bars = []
    for d, r in slice_df.iterrows():
        if hasattr(d, "strftime"):
            date_s = d.strftime("%Y-%m-%d")
        else:
            date_s = str(d)[:10]
        vol = r["volume"] if "volume" in r.index else 0
        try:
            vol_i = int(vol) if vol == vol else 0  # NaN 判断
        except (TypeError, ValueError):
            vol_i = 0
        bars.append({
            "date":   date_s,
            "open":   round(float(r["open"]), 4),
            "high":   round(float(r["high"]), 4),
            "low":    round(float(r["low"]), 4),
            "close":  round(float(r["close"]), 4),
            "volume": vol_i,
        })

    first = bars[0]
    last = bars[-1]
    prev_close = bars[-2]["close"] if len(bars) >= 2 else first["open"]
    chg = last["close"] - prev_close
    chg_pct = (chg / prev_close) if prev_close else 0.0
    period_high = max(b["high"] for b in bars)
    period_low = min(b["low"] for b in bars)
    period_ret = (last["close"] / first["open"] - 1.0) if first["open"] else 0.0

    return {
        "ok": True,
        "code": stock_code,
        "name": get_stock_name(stock_code),
        "count": len(bars),
        "bars": bars,
        "stats": {
            "last_close":  last["close"],
            "change":      round(chg, 4),
            "change_pct":  round(chg_pct, 6),
            "period_high": period_high,
            "period_low":  period_low,
            "period_ret":  round(period_ret, 6),
            "start":       first["date"],
            "end":         last["date"],
        },
    }


def _empty_state():
    return {
        "trading_status": "UNKNOWN", "capital": 0, "positions": [],
        "today_pnl": 0, "today_pnl_pct": 0,
        "events": [], "signals": [], "orders": [], "pnl_history": [],
        "control": {"pause_buying": False, "force_clear_all": False,
                    "max_daily_loss": -0.02, "dry_run": True,
                    "max_slots": 5, "slot_codes": []},
        "health": {"qmt_connected": False, "last_heartbeat": None, "errors_24h": 0},
    }


def _load_state() -> dict:
    if not OUTPUTS_LIVE_STATE.exists():
        return _empty_state()
    try:
        return json.loads(OUTPUTS_LIVE_STATE.read_text(encoding="utf-8"))
    except Exception:
        return _empty_state()


def _save_state(state: dict):
    state["_updated_at"] = datetime.now().isoformat(timespec="seconds")
    tmp = OUTPUTS_LIVE_STATE.with_suffix(".tmp")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, OUTPUTS_LIVE_STATE)


# ------------------------------------------------------------
def _live_market():
    """复用模拟盘已连上的 QMT, 没有再新建"""
    loop = getattr(_SIM, "_loop", None)
    if loop is not None:
        return loop.market
    from live_trading.live_loop import MarketDataProvider
    return MarketDataProvider()


def _overlay_sim_live_prices(s: dict) -> dict:
    """页面每 5 秒读 state 时用 QMT tick 刷现价, 不等主循环 60 秒"""
    positions = s.get("positions") or []
    if not positions:
        return s
    from live_trading.live_loop import update_positions_from_market
    try:
        s["positions"] = update_positions_from_market(positions, _live_market())
    except Exception as e:
        print(f"[WARN] 模拟盘刷现价失败: {type(e).__name__}: {e}", flush=True)
    return s


def _stamp_real_quotes(positions: list) -> list:
    """实盘持仓补中文名 + QMT 最新价"""
    from lib.backtest_data import get_stock_names
    from live_trading.live_loop import tick_last_price

    if not positions:
        return []
    codes = [p.get("stock_code") for p in positions if p.get("stock_code")]
    names = get_stock_names(codes)
    try:
        ticks = _live_market().get_latest_ticks(codes)
    except Exception as e:
        print(f"[WARN] 实盘刷现价失败: {type(e).__name__}: {e}", flush=True)
        ticks = {}
    out = []
    for p in positions:
        row = dict(p)
        code = row.get("stock_code") or ""
        if code:
            row["name"] = names.get(code) or code
        last = tick_last_price(ticks.get(code) or {})
        if last > 0:
            row["last_price"] = last
            vol = int(row.get("volume") or 0)
            if vol > 0:
                row["market_value"] = last * vol
        out.append(row)
    return out


@router.get("/state")
def get_state():
    """读 live_state; 附带 names 映射, 供信号/订单表显示中文名"""
    from lib.backtest_data import get_stock_names

    s = _overlay_sim_live_prices(_load_state())
    codes = set()
    for p in s.get("positions") or []:
        c = p.get("code")
        if c:
            codes.add(c)
    for sig in s.get("signals") or []:
        c = sig.get("code")
        if c:
            codes.add(c)
    for o in s.get("orders") or []:
        c = o.get("code")
        if c:
            codes.add(c)
    names = get_stock_names(list(codes))
    # 持仓里已有手写 name (mock_positions) 优先
    for p in s.get("positions") or []:
        c, n = p.get("code"), (p.get("name") or "").strip()
        if c and n and n != c:
            names[c] = n
    s["names"] = names
    return s


@router.get("/sim/status")
def sim_status():
    return _SIM.status()


@router.post("/sim/start")
def sim_start(payload: dict = Body(...)):
    watch = payload.get("watch_stocks", "")
    if isinstance(watch, list):
        ui_codes = [str(c).strip() for c in watch if str(c).strip()]
    else:
        ui_codes = [c.strip() for c in str(watch or "").split(",") if c.strip()]
    merged = merge_watch_codes(ui_codes)
    if not merged:
        return {
            "ok": False,
            "message": "监控池为空: 请在自选监控池、mock 持仓、策略路由 per_stock 或「额外监控」中至少保留一只股票",
        }
    dry_run = bool(payload.get("dry_run", True))   # 默认模拟
    msg = _SIM.start(watch_stocks=merged, dry_run=dry_run, cycle_seconds=60)
    return {"ok": "OK" in msg, "message": msg}


@router.post("/sim/stop")
def sim_stop(payload: dict = Body({})):
    msg = _SIM.stop()
    return {"ok": True, "message": msg}


@router.post("/sim/trial_run")
@router.post("/sim/trial_run/")
def sim_trial_run(payload: Optional[Dict[str, Any]] = Body(None)):
    """非交易时段也强制让引擎跑一次 (按最近一根 K 线生成信号)

    用途: 晚上/周末打开页面想看「现在各策略给的方向」时, 不用等到开盘.
    若引擎是 dry_run 模式 (默认), 只产信号 + 模拟撮合; 若是实盘模式, 这里也会真下单, 谨慎.

    返回: {ok, message, summary{buy,sell,hold,total}, diagnoses[{code,strategy,side,reason}]}
          summary/diagnoses 用于"为什么没看到信号" -- 哪怕全 hold 也告诉用户每只各自给了什么方向.
    """
    return _SIM.trial_run()


@router.post("/sim/reset_positions")
def sim_reset_positions(payload: Optional[Dict[str, Any]] = Body(None)):
    """按当前股票 / 策略 / 档位, 从 start_date 重新开仓并回放"""
    payload = payload or {}
    raw_rows = payload.get("rows")
    rows = raw_rows if isinstance(raw_rows, list) else None
    msg = _SIM.reset_positions(
        str(payload.get("start_date") or ""),
        rows,
        payload.get("capital"),
        payload.get("max_slots"),
    )
    return {"ok": "OK" in msg, "message": msg}


@router.post("/sim/clear_history")
def sim_clear_history(payload: dict = Body({})):
    """清空 events / signals / orders / pnl_history (持仓不动)"""
    msg = _SIM.clear_history()
    return {"ok": "OK" in msg, "message": msg}


def _append_event(state: dict, level: str, title: str, source: str = "ceo_console") -> None:
    """往 state.events 追加一条带级别的事件（告警四级分层，供前端徽章用）"""
    ev = {
        "ts":     datetime.now().isoformat(timespec="seconds"),
        "level":  level,
        "title":  title,
        "source": source,
    }
    state.setdefault("events", []).append(ev)
    state["events"] = state["events"][-200:]


# 危险操作的级别映射: control 的字段 / status 的取值 -> 事件级别
_CONTROL_LEVEL = {
    "force_clear_all": "CRITICAL",
    "pause_buying":    "WARN",
    "max_daily_loss":  "INFO",
    "dry_run":         "INFO",
}
_STATUS_LEVEL = {"HALTED": "CRITICAL", "PAUSED": "WARN", "RUNNING": "INFO"}


@router.post("/control")
def control(payload: dict = Body(...)):
    field = payload.get("field")
    value = payload.get("value")
    if not field:
        return {"ok": False, "message": "field 不能为空"}
    s = _load_state()
    s.setdefault("control", {})[field] = value
    level = _CONTROL_LEVEL.get(field, "INFO")
    _append_event(s, level, f"control.{field} = {value}")
    _save_state(s)
    return {"ok": True, "message": f"control.{field} = {value}"}


@router.post("/slots")
def slots_update(payload: Optional[Dict[str, Any]] = Body(None)):
    """改本金 / 档位数 / 给某只分档 / 空仓释放档位. 写入 yaml + state"""
    payload = payload or {}
    s = _load_state()
    ctrl = s.setdefault("control", {})
    capital = float(s["capital"])
    max_slots = int(ctrl["max_slots"])
    codes = [str(c) for c in ctrl["slot_codes"]]
    msg = ""

    if payload.get("capital") is not None:
        try:
            cap = float(payload["capital"])
        except (TypeError, ValueError):
            return {"ok": False, "message": "本金必须是数字"}
        if cap < 1:
            return {"ok": False, "message": "本金必须大于 0"}
        cost_used = sum(
            float(p.get("cost") or 0) * int(p.get("volume") or 0)
            for p in (s.get("positions") or [])
        )
        if cap < cost_used:
            return {"ok": False, "message": f"本金不能低于当前持仓成本占用 {cost_used:,.0f}"}
        capital = cap
        msg = f"本金 {capital:,.0f}, 每档 {capital / max_slots:,.0f}"

    if payload.get("max_slots") is not None:
        try:
            n = int(payload["max_slots"])
        except (TypeError, ValueError):
            return {"ok": False, "message": "档数必须是整数"}
        if n < 1 or n > 10:
            return {"ok": False, "message": "档数须在 1～10"}
        if n < len(codes):
            return {"ok": False, "message": f"已分 {len(codes)} 档, 先释放再把上限改为 {n}"}
        max_slots = n
        msg = f"本金 {capital:,.0f}, 最多 {max_slots} 档, 每档 {capital / max_slots:,.0f}"

    assign = _normalize_stock_code(str(payload.get("assign") or ""))
    if assign:
        if assign in codes:
            return {"ok": True, "message": f"{assign} 已有档位"}
        if len(codes) >= max_slots:
            return {"ok": False, "message": f"档已满 ({len(codes)}/{max_slots})"}
        codes.append(assign)
        msg = f"{assign} 已分档"

    release = _normalize_stock_code(str(payload.get("release") or ""))
    if release:
        vol = 0
        for p in s.get("positions") or []:
            if p.get("code") == release:
                vol = int(p.get("volume") or 0)
                break
        if vol > 0:
            return {"ok": False, "message": f"{release} 仍有持仓 {vol} 股, 空仓才能释放"}
        if release not in codes:
            return {"ok": False, "message": f"{release} 未分档"}
        codes = [c for c in codes if c != release]
        msg = f"{release} 已释放档位"

    drop = _normalize_stock_code(str(payload.get("drop") or ""))
    if drop:
        if drop not in codes:
            return {"ok": True, "message": f"{drop} 未分档"}
        codes = [c for c in codes if c != drop]
        msg = f"{drop} 已从档位册拿掉"

    if not msg:
        return {"ok": False, "message": "需要 capital / max_slots / assign / release / drop"}

    save_slot_book(max_slots, codes, capital)
    s["capital"] = capital
    ctrl["max_slots"] = max_slots
    ctrl["slot_codes"] = codes
    from live_trading.live_loop import calc_today_pnl
    today_pnl, today_pct = calc_today_pnl(s.get("positions") or [], capital)
    s["today_pnl"] = today_pnl
    s["today_pnl_pct"] = today_pct
    _SIM.apply_capital(capital)
    _append_event(s, "INFO", msg)
    _save_state(s)
    return {
        "ok": True,
        "message": msg,
        "capital": capital,
        "max_slots": max_slots,
        "slot_codes": codes,
    }


@router.post("/status")
def set_status(payload: dict = Body(...)):
    status = payload.get("status")
    if status not in ("RUNNING", "PAUSED", "HALTED"):
        return {"ok": False, "message": "status 必须是 RUNNING/PAUSED/HALTED"}
    s = _load_state()
    s["trading_status"] = status
    level = _STATUS_LEVEL.get(status, "INFO")
    _append_event(s, level, f"trading_status -> {status}")
    _save_state(s)
    return {"ok": True, "message": f"trading_status = {status}"}


# ============================================================
# 策略路由表 -- registry / config
# ============================================================

@router.get("/strategies/registry")
def strategies_registry():
    """列出所有可注册策略 (按分组)
    返回:
        {
          "groups": {"技术指标": [{"name", "label", "description"}, ...], ...},
          "flat":   [{"name", "label", "group", "description"}, ...]
        }
    """
    return {
        "groups": list_groups(),
        "flat":   list_strategies(),
    }


@router.get("/strategies/config")
def strategies_config_get():
    """读当前路由表 -- {default, per_stock}"""
    return load_strategy_config()


@router.post("/strategies/config")
def strategies_config_set(payload: dict = Body(...)):
    """保存路由表 + 热加载到 LiveSimRunner

    payload: {"default": "macd_1d", "per_stock": {"600519.SH": "grid_classic", ...}}
    """
    default = payload.get("default", "macd_1d")
    per_stock = payload.get("per_stock", {}) or {}

    # 校验所有引用的策略都已注册
    valid_names = {s["name"] for s in list_strategies()}
    invalid = [s for s in [default, *per_stock.values()] if s not in valid_names]
    if invalid:
        return {"ok": False,
                "message": f"未知策略: {invalid}, 有效策略: {sorted(valid_names)}"}

    msg = _SIM.apply_strategy_config(default=default, per_stock=per_stock)
    return {"ok": True, "message": msg}


# ============================================================
# 监控列表合并 + 自选池
# ============================================================

# ============================================================
# 绑定来源标签 (用来区分: 这只股票的策略绑定是从「模拟盘」还是「实盘」加的)
#
# 设计动机:
#   实盘视图加股票时, 其实只是想让 AI 监控那只真实持仓股, 不希望污染模拟盘的「待入场」展示.
#   所以在 strategies.yaml 之外, 单独维护一个轻量 yaml 文件, 记录每个 code 的 source.
#   - sim  : 模拟盘里加的, 模拟盘「待入场」会显示, 引擎跑模拟成交
#   - real : 实盘里加的, 模拟盘「待入场」隐藏, 但引擎仍然算信号 (供「AI 信号 → 手动授权」用)
#   旧记录 / 缺省 source 一律视为 sim, 保持向后兼容.
# ============================================================

BINDING_SOURCE_FILE = PROJECT_ROOT / "config" / "binding_source.yaml"


def _load_binding_sources() -> Dict[str, str]:
    if not BINDING_SOURCE_FILE.exists():
        return {}
    try:
        data = _yaml.safe_load(BINDING_SOURCE_FILE.read_text(encoding="utf-8")) or {}
        sources = data.get("sources") or {}
        return {str(k): str(v) for k, v in sources.items() if v in ("sim", "real")}
    except Exception:
        return {}


def _save_binding_sources(sources: Dict[str, str]):
    BINDING_SOURCE_FILE.parent.mkdir(parents=True, exist_ok=True)
    text = _yaml.safe_dump({"sources": sources}, allow_unicode=True, sort_keys=True)
    BINDING_SOURCE_FILE.write_text(text, encoding="utf-8")


def _set_binding_source(code: str, source: str):
    """更新单只股票的 source; source 必须是 'sim' / 'real'"""
    if source not in ("sim", "real"):
        return
    data = _load_binding_sources()
    data[code] = source
    _save_binding_sources(data)


def _drop_binding_source(code: str):
    data = _load_binding_sources()
    if code in data:
        data.pop(code, None)
        _save_binding_sources(data)


def _resolve_binding_sources(merged: list) -> Dict[str, str]:
    """给 mergedList 每个 code 推断 source:
       - binding_source.yaml 里显式记录过的 -> 用记录
       - 在 mock_positions / watch_pool / ui 里出现的 -> sim (模拟盘资源)
       - 都不在 -> real (推断是实盘绑的, 旧数据迁移用)
    """
    explicit = _load_binding_sources()
    mock_codes = {p.get("code") for p in load_mock_config().get("positions", []) if p.get("code")}
    watch_codes = set(load_watch_pool().get("codes") or [])
    sim_pool = mock_codes | watch_codes
    out: Dict[str, str] = {}
    for c in merged:
        if c in explicit:
            out[c] = explicit[c]
        elif c in sim_pool:
            out[c] = "sim"
        else:
            out[c] = "real"
    return out


@router.get("/watch_merge")
def watch_merge(ui: str = ""):
    """返回合并后的监控代码 (与启动模拟盘时一致)

    ui: 可选, 逗号分隔, 对应页面「额外监控」
    """
    from lib.backtest_data import get_stock_names

    ui_codes = [c.strip() for c in (ui or "").split(",") if c.strip()]
    merged = merge_watch_codes(ui_codes)
    # names: 给前端「待入场」行补中文简称 (映射自 trade_stock_status)
    return {
        "merged": merged,
        "names": get_stock_names(merged),
        "binding_source": _resolve_binding_sources(merged),
        "sources": {
            "ui_extra":    ui_codes,
            "positions":   [p.get("code", "") for p in load_mock_config().get("positions", [])],
            "watch_pool":  load_watch_pool().get("codes", []),
            "strategy_keys": list(load_strategy_config().get("per_stock", {}).keys()),
        },
    }


@router.get("/watch_pool")
def watch_pool_get():
    return load_watch_pool()


def _normalize_stock_code(raw: str) -> str:
    """去掉空格并统一大写。纯字母代码视为美股，补 .US。"""
    import re
    s = (raw or "").strip().replace(" ", "").upper()
    if re.fullmatch(r"[A-Z][A-Z0-9]*(-[A-Z0-9]+)?", s):
        return f"{s}.US"
    return s


@router.post("/watch_pool")
def watch_pool_set(payload: Optional[Dict[str, Any]] = Body(None)):
    """写 watch_pool.yaml；失败时仍返回 JSON, 避免前端拿不到原因"""
    payload = payload or {}
    try:
        raw = payload.get("codes", "")
        if isinstance(raw, list):
            codes = [str(c).strip() for c in raw if str(c).strip()]
        else:
            codes = [
                c.strip()
                for c in str(raw or "").replace("，", ",").replace("\n", ",").split(",")
                if c.strip()
            ]
        codes = [_normalize_stock_code(c) for c in codes]
        save_watch_pool(codes)
        return {"ok": True, "message": f"[OK] 监控列表已保存, 共 {len(codes)} 只", "codes": codes}
    except Exception as e:
        return {"ok": False, "message": str(e), "codes": []}


# ============================================================
# 实盘账户 (miniQMT) -- 真实持仓 / 资金 / 委托
# ============================================================

# 进程级缓存: trader 实例 + 上次查询时间戳, 避免每 5 秒前端轮询都重连 miniQMT
_REAL_TRADER = None
_REAL_CACHE: Dict[str, Any] = {"asset": {}, "positions": [], "orders": [],
                                "ts": 0.0, "error": None}
_REAL_CACHE_TTL = 5.0   # 秒

# xtquant 委托状态码 -> 中文（与 xtconstant / 常见回报含义对齐）
_ORDER_STATUS_MAP = {
    48: "未知",   49: "未报",   50: "待报",   51: "已报",
    52: "已报待撤", 53: "部成待撤", 54: "部撤",   55: "已撤",
    56: "部成",   57: "已成",   58: "废单",
}
# 状态码 < 56 视为「未结束」(可撤单 / 等待成交)
_ORDER_PENDING_STATUS = {49, 50, 51, 52, 53}


def _query_real_orders_dict(trader) -> list:
    """走 trader.query_orders()（双后端公共方法），转成路由需要的字段。
    旧版直接访问 trader._trader（QMT HTTP 后端没有 _trader/_account 属性，会 AttributeError），
    已改为走抽象层 query_orders()。HTTP 后端部分字段（stock_code/side/price）可能为空。"""
    try:
        raw = trader.query_orders()
    except Exception:
        return []
    if not raw:
        return []
    out = []
    for o in raw:
        side_code = o.get("order_type", 0) or 0
        status_code = o.get("order_status", 0) or 0
        out.append({
            "order_id":      o.get("order_id"),
            "stock_code":    o.get("stock_code", ""),
            "side":          "buy" if side_code == 23 else ("sell" if side_code == 24 else (f"type_{side_code}" if side_code else "")),
            "order_volume":  o.get("order_volume", 0),
            "traded_volume": o.get("traded_volume", 0),
            "price":         float(o.get("price", 0) or 0),
            "order_status":  status_code,
            "status_text":   _ORDER_STATUS_MAP.get(status_code, f"未知({status_code})"),
            "cancelable":    status_code in _ORDER_PENDING_STATUS,
            "order_time":    o.get("order_time", 0),
            "strategy_name": o.get("strategy_name", ""),
            "order_remark":  o.get("order_remark", ""),
        })
    return out


def use_miniqmt_backend() -> bool:
    from lib.qmt_backend import is_miniqmt
    return is_miniqmt()


def qmt_backend_label() -> str:
    from lib.qmt_backend import label
    return label()


def tool_ctx() -> dict:
    from lib.qmt_backend import snapshot
    return snapshot()


def _get_real_trader():
    """懒加载 trader 实例（双后端，按 WUCAI_QMT 选）; 失败时抛异常 (调用方负责捕获)"""
    global _REAL_TRADER
    if _REAL_TRADER is not None:
        return _REAL_TRADER

    from lib.qmt_backend import can_trade, current
    if not can_trade():
        raise RuntimeError("通达信模式不能下单，请改用 QMT、QMT共享内存或 miniQMT")

    use_miniqmt = use_miniqmt_backend()
    qmt_path = os.environ.get("QMT_PATH", "").strip()
    account_id = os.environ.get("ACCOUNT_ID", "").strip()
    if use_miniqmt and (not qmt_path or not account_id):
        raise RuntimeError("miniQMT 后端需配置 QMT_PATH / ACCOUNT_ID；或设 WUCAI_QMT=qmt 用 QMT HTTP")

    setup_sys_path()  # live_trading / lib 等路径
    from qmt_trader_v2 import QMTTraderV2  # type: ignore
    trader = QMTTraderV2(
        qmt_path=qmt_path or None,
        account_id=account_id or None,
        enable_heartbeat=False,   # 实盘视图只读: 不需要后台心跳, 避免占用资源
        enable_reconnect=False,
        backend=current(),
    )
    trader.connect()
    _REAL_TRADER = trader
    return trader


def _market_clock() -> dict:
    """实盘页用: 今天是不是 A 股交易日 (只按周一到周五)"""
    now = datetime.now()
    return {
        "is_trading_day": is_a_share_trading_day(now),
        "weekday": weekday_cn(now),
        "date": now.strftime("%Y-%m-%d"),
    }


@router.get("/real_account")
def real_account():
    """实盘视图用: 拉 miniQMT 真实账户 + 持仓 + 当日委托 (5 秒进程级缓存).

    返回格式 (无论成功失败都返回 200, 由 connected 字段告诉前端):
      {
        "connected": true/false,
        "error":     null | "失败原因",
        "asset":     {"total_asset", "cash", "market_value", "frozen_cash"} | {},
        "positions": [{"stock_code","volume","can_use_volume","open_price","market_value"}],
        "orders":    [{"order_id","stock_code","side","order_volume","traded_volume",
                       "price","order_status","status_text","cancelable","order_time"}],
        "cached_age_sec": <float>,
        "market":    {"is_trading_day", "weekday", "date"},
      }
    """
    import time as _time
    now = _time.time()
    market = _market_clock()
    age = now - (_REAL_CACHE.get("ts") or 0)
    if _REAL_CACHE.get("ts") and age < _REAL_CACHE_TTL:
        return {
            "connected": _REAL_CACHE.get("error") is None,
            "error":     _REAL_CACHE.get("error"),
            "asset":     _REAL_CACHE.get("asset") or {},
            "positions": _stamp_real_quotes(_REAL_CACHE.get("positions") or []),
            "orders":    _REAL_CACHE.get("orders") or [],
            "cached_age_sec": round(age, 2),
            "market": market,
        }

    try:
        trader = _get_real_trader()
        asset = trader.query_asset() or {}
        positions = trader.query_positions() or []
        orders = _query_real_orders_dict(trader)
        _REAL_CACHE.update({"asset": asset, "positions": positions,
                            "orders": orders, "ts": now, "error": None})
        return {
            "connected": True, "error": None,
            "asset": asset, "positions": _stamp_real_quotes(positions), "orders": orders,
            "cached_age_sec": 0.0,
            "market": market,
        }
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        _REAL_CACHE.update({"asset": {}, "positions": [], "orders": [],
                            "ts": now, "error": err})
        return {
            "connected": False, "error": err,
            "asset": {}, "positions": [], "orders": [],
            "cached_age_sec": 0.0,
            "market": market,
        }


@router.post("/real_order/cancel")
def real_order_cancel(payload: Optional[Dict[str, Any]] = Body(None)):
    """撤销 miniQMT 上指定委托

    body: {"order_id": <int>}
    """
    payload = payload or {}
    raw_id = payload.get("order_id")
    if raw_id is None or str(raw_id).strip() == "":
        return {"ok": False, "message": "缺少 order_id"}

    try:
        trader = _get_real_trader()
        # miniQMT 后端 cancel_order_stock 需要 int 委托号;
        # QMT HTTP 后端用交易所委托号 m_strOrderSysID(字符串), 不能 int 强转
        if trader.use_miniqmt:
            try:
                order_id = int(raw_id)
            except (TypeError, ValueError):
                return {"ok": False, "message": f"order_id 必须是整数, 收到: {raw_id!r}"}
        else:
            order_id = str(raw_id).strip()
        result = trader.cancel(order_id)
    except Exception as e:
        return {"ok": False, "message": f"{type(e).__name__}: {e}"}

    # 撤单后立刻让缓存过期, 下一次 real_account 会重新拉
    _REAL_CACHE["ts"] = 0.0

    if result == 0:
        return {"ok": True, "message": f"已提交撤单请求: 编号 {order_id}"}
    return {"ok": False, "message": f"撤单失败: 编号 {order_id}, 后端返回 {result}"}


# ============================================================
# AI 信号 → 手动授权下单 (实盘视图用)
#
# 设计:
#   - 引擎照常跑 (dry_run=True), 把 buy/sell 信号写到 state.signals.
#   - 实盘视图把这些信号当作 "AI 建议", 用户点 [授权] 才走 miniQMT 真实下单.
#   - 授权 / 拒绝 / 过期状态写到独立文件 OUTPUTS_DIR/live_approvals.json,
#     不覆盖主 live_state.json。
#   - 5 分钟未处理自动过期 (避免旧信号误下).
#   - 单笔金额按"总资金 * 10% / 现价 / 100 * 100" 算, 与 live_loop.py 一致.
# ============================================================

_APPROVALS_FILE = OUTPUTS_DIR / "live_approvals.json"
_APPROVAL_TTL_SEC = 300   # 5 分钟


def _signal_id(sig: dict) -> str:
    """根据信号生成稳定 id (ts + code + side); 同一信号在状态文件里唯一"""
    return f"{sig.get('ts', '')}|{sig.get('code', '')}|{sig.get('side', '')}"


def _load_approvals() -> Dict[str, Any]:
    if not _APPROVALS_FILE.exists():
        return {}
    try:
        return json.loads(_APPROVALS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_approvals(data: Dict[str, Any]):
    _APPROVALS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _APPROVALS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, _APPROVALS_FILE)


def _calc_suggested_quantity(price: float, capital: float, max_slots: int, code: str = "") -> int:
    """与 live_loop.size_buy_shares 同一额度 (按每档目标金额)"""
    from live_trading.live_loop import share_lot, size_buy_shares, slot_budget
    return size_buy_shares(price, slot_budget(capital, max_slots), capital, lot=share_lot(code))


def _signal_age_sec(ts: str) -> float:
    try:
        t = datetime.fromisoformat(ts)
    except Exception:
        return 0.0
    return (datetime.now() - t).total_seconds()


def _real_approval_codes() -> set:
    """授权卡片只收实盘票: 实盘绑定, 或 QMT 账户里已经有的持仓.

    回测「绑到模拟盘」只改 strategies.yaml, 信号仍进同一份 state.signals;
    不在这里滤掉的话, 模拟盘的宁德时代也会跑到实盘待授权.
    """
    codes = {c for c, src in _load_binding_sources().items() if src == "real"}
    for p in (_REAL_CACHE.get("positions") or []):
        c = p.get("stock_code")
        if c:
            codes.add(c)
    return codes


@router.get("/approvals")
def approvals_list():
    """实盘视图调用: 把最近 buy/sell 信号 + approval 状态 + 风控建议数量 一起返回.

    返回:
      {
        "items": [
          {"id", "ts", "code", "side", "strategy", "reason",
           "suggested_quantity", "suggested_price",
           "status": "pending"|"approved"|"rejected"|"expired",
           "processed_ts": <ISO>|null,
           "order": {...}|null,
           "error": null|"...",
           "age_sec": <float>}
        ],
        "ttl_sec": 300,
        "capital": <float>,
      }
    """
    state = _load_state()
    signals = state.get("signals") or []
    approvals = _load_approvals()
    capital = float(state.get("capital") or load_mock_config()["capital"])
    max_slots = int((state.get("control") or {})["max_slots"])

    # 持仓最新价 -- 用于风控数量估算
    pos_price = {p.get("code"): float(p.get("cur_price") or 0)
                 for p in (state.get("positions") or [])}

    # === 实盘账户的 持仓 / 可用现金 (用 cache, 不强制刷新; UI 自己每 5 秒拉) ===
    real_positions_map = {}
    real_cash = 0.0
    if _REAL_CACHE.get("ts"):
        for p in (_REAL_CACHE.get("positions") or []):
            real_positions_map[p.get("stock_code")] = p
        real_cash = float((_REAL_CACHE.get("asset") or {}).get("cash") or 0)

    def _eligibility(side: str, code: str, qty: int, price_hint: float, label: str):
        """判断这条信号在实盘里是否可下单. 返回 (eligible: bool, reason: str)"""
        shown = f"{label}({code})" if label and label != code else code
        if not _REAL_CACHE.get("ts"):
            # miniQMT 还没拉过, 不阻断 (用户点了会走 approve 后端校验)
            return True, ""
        if side == "sell":
            pos = real_positions_map.get(code)
            if not pos:
                return False, f"实盘无 {shown} 持仓, 无法卖"
            can_use = int(pos.get("can_use_volume") or 0)
            if can_use < qty:
                return False, f"可用 {can_use} < 建议 {qty} (T+1 冻结)"
        else:
            need = qty * (price_hint or 0)
            if price_hint > 0 and real_cash > 0 and need > real_cash:
                return False, f"现金 {real_cash:,.0f} < 需 {need:,.0f}"
        return True, ""

    items = []
    real_codes = _real_approval_codes()
    # 最近 buy/sell, 倒序 (最新在上); 合并连续同向重复 (水平策略历史刷屏)
    recent = [
        s for s in signals
        if s.get("side") in ("buy", "sell") and s.get("code") in real_codes
    ][-60:]
    deduped = []
    for sig in reversed(recent):
        if deduped:
            p = deduped[-1]
            if (p.get("code") == sig.get("code")
                    and p.get("side") == sig.get("side")
                    and (p.get("strategy") or "") == (sig.get("strategy") or "")):
                continue
        deduped.append(sig)
        if len(deduped) >= 30:
            break
    from lib.backtest_data import get_stock_names
    names = get_stock_names([s.get("code") for s in deduped if s.get("code")])
    for sig in deduped:
        sid = _signal_id(sig)
        rec = approvals.get(sid) or {}
        age = _signal_age_sec(sig.get("ts", ""))
        status = rec.get("status") or "pending"
        # 5 分钟未处理 -> 过期; 已处理(approved/rejected)保持原状态不会被覆盖
        if status == "pending" and age > _APPROVAL_TTL_SEC:
            status = "expired"
        code = sig.get("code", "")
        price = pos_price.get(code, 0.0)
        qty = _calc_suggested_quantity(price, capital, max_slots, code)
        name = names.get(code) or code
        eligible, eligible_reason = _eligibility(sig.get("side"), code, qty, price, name)
        items.append({
            "id":         sid,
            "ts":         sig.get("ts", ""),
            "code":       code,
            "name":       name,
            "side":       sig.get("side", ""),
            "strategy":   sig.get("strategy", ""),
            "reason":     sig.get("reason", ""),
            "suggested_quantity": qty,
            "suggested_price":    price,   # 0 时前端展示「市价」, 下单也走市价
            "status":     status,
            "processed_ts": rec.get("processed_ts"),
            "order":      rec.get("order"),
            "error":      rec.get("error"),
            "age_sec":    round(age, 1),
            "eligible":         eligible,
            "eligible_reason":  eligible_reason,
        })
    return {"items": items, "ttl_sec": _APPROVAL_TTL_SEC, "capital": capital}


def _find_signal_by_id(sid: str) -> Optional[dict]:
    state = _load_state()
    for sig in (state.get("signals") or []):
        if _signal_id(sig) == sid:
            return sig
    return None


@router.post("/approvals/approve")
def approvals_approve(payload: Optional[Dict[str, Any]] = Body(None)):
    """授权下单: 通过 miniQMT 真实下单, 状态写入 approvals.json.

    body: {"id": "<signal_id>", "quantity": <int 可选, 覆盖建议数量>, "price": <float 可选, 0=市价>}
    """
    payload = payload or {}
    sid = str(payload.get("id", "")).strip()
    if not sid:
        return {"ok": False, "message": "id 不能为空"}

    sig = _find_signal_by_id(sid)
    if sig is None:
        return {"ok": False, "message": f"找不到信号 (可能已过期被清理): {sid}"}
    if sig.get("code") not in _real_approval_codes():
        return {"ok": False, "message": "这是模拟盘信号, 不能授权实盘下单"}

    approvals = _load_approvals()
    if approvals.get(sid, {}).get("status") in ("approved", "rejected"):
        return {"ok": False, "message": f"信号已处理过 (status={approvals[sid].get('status')})"}
    if _signal_age_sec(sig.get("ts", "")) > _APPROVAL_TTL_SEC:
        return {"ok": False, "message": f"信号已过期 (>{_APPROVAL_TTL_SEC}s), 请等待新信号"}

    code = sig.get("code", "")
    side = sig.get("side", "")
    if side not in ("buy", "sell"):
        return {"ok": False, "message": f"信号方向异常: {side}"}

    # 数量: 用户传了就用, 否则按每档目标金额算
    state = _load_state()
    capital = float(state.get("capital") or load_mock_config()["capital"])
    max_slots = int((state.get("control") or {})["max_slots"])
    pos_price = {p.get("code"): float(p.get("cur_price") or 0)
                 for p in (state.get("positions") or [])}
    price_hint = pos_price.get(code, 0.0)
    quantity = int(payload.get("quantity") or _calc_suggested_quantity(
        price_hint, capital, max_slots, code,
    ))
    price = float(payload.get("price") or 0)   # 0 -> 市价

    # === 实盘前置校验 (避免下了券商必拒的单) ===
    real_acc = _REAL_CACHE if _REAL_CACHE.get("ts") else None
    if side == "sell":
        # 必须实盘账户里有这只持仓且 can_use_volume >= quantity
        positions = (real_acc or {}).get("positions") or []
        match = next((p for p in positions if p.get("stock_code") == code), None)
        if not match:
            return {"ok": False, "message": f"实盘持仓里没有 {code}, 无法卖出 (T+1 限制)"}
        can_use = int(match.get("can_use_volume") or 0)
        if can_use < quantity:
            return {"ok": False,
                    "message": f"可用持仓不足: {code} 可用 {can_use} 股 < 建议 {quantity} 股 (其余被 T+1 冻结)"}
    elif side == "buy":
        # 现金校验 (粗略: 按 price 或 0 跳过)
        cash = float((real_acc or {}).get("asset", {}).get("cash") or 0)
        if cash > 0 and price_hint > 0 and quantity * price_hint > cash:
            return {"ok": False,
                    "message": f"可用现金不足: 需要 ~{quantity * price_hint:,.0f} 元, 实盘可用 {cash:,.0f} 元"}

    # 调 miniQMT 真实下单
    err = None
    order = None
    try:
        trader = _get_real_trader()
        if side == "buy":
            order_id = trader.buy(code, quantity, price=price,
                                  strategy_name=sig.get("strategy", ""),
                                  remark="manual_approval")
        else:
            order_id = trader.sell(code, quantity, price=price,
                                   strategy_name=sig.get("strategy", ""),
                                   remark="manual_approval")
        # QMT HTTP 后端拿不到真实委托编号(passorder 无返回值, order_id 是 "unknown" 字符串),
        # 只有数值型才能做 <0 判断, 否则 "unknown"<0 会抛 TypeError 把已成功的下单误报成失败
        if order_id is None or (isinstance(order_id, (int, float)) and order_id < 0):
            err = f"后端返回 order_id={order_id} (常见原因: 未连接 / 风控拦截 / 余额不足)"
        else:
            order = {"order_id": order_id, "code": code, "side": side,
                     "quantity": quantity, "price": price}
    except Exception as e:
        err = f"{type(e).__name__}: {e}"

    approvals[sid] = {
        "status":       "approved" if err is None else "rejected",
        "processed_ts": datetime.now().isoformat(timespec="seconds"),
        "order":        order,
        "error":        err,
    }
    _save_approvals(approvals)

    if err:
        return {"ok": False, "message": f"下单失败: {err}", "error": err}
    return {"ok": True, "message": f"已授权下单 {side} {code} {quantity}股", "order": order}


@router.post("/approvals/reject")
def approvals_reject(payload: Optional[Dict[str, Any]] = Body(None)):
    """拒绝信号: 标记不下单, 状态落盘"""
    payload = payload or {}
    sid = str(payload.get("id", "")).strip()
    if not sid:
        return {"ok": False, "message": "id 不能为空"}

    approvals = _load_approvals()
    if approvals.get(sid, {}).get("status") in ("approved", "rejected"):
        return {"ok": False, "message": f"信号已处理过 (status={approvals[sid].get('status')})"}

    approvals[sid] = {
        "status":       "rejected",
        "processed_ts": datetime.now().isoformat(timespec="seconds"),
        "order":        None,
        "error":        None,
    }
    _save_approvals(approvals)
    return {"ok": True, "message": "已拒绝该信号"}


@router.get("/stock/search")
def stock_search(q: str = "", limit: int = 20):
    """股票搜索: 支持代码 / 中文名模糊匹配 (trade_stock_status)"""
    from lib.backtest_data import search_stocks

    q = (q or "").strip()
    if not q:
        return {"ok": True, "items": [], "message": "请输入代码或名称"}
    items = search_stocks(q, limit=limit)
    return {"ok": True, "items": items, "q": q}


@router.post("/stock/bind")
@router.post("/stock/bind/")
def stock_bind(payload: Optional[Dict[str, Any]] = Body(None)):
    """添加一只股票到监控列表, 并写入 per_stock 策略绑定 (写盘 + 热加载)

    body: {"code": "002432.SZ" 或 "兆易创新", "strategy": "macd_1d", "source": "sim"|"real"}
    source 默认 'sim'; 'real' 表示这只股票是从「实盘」视图绑的, 模拟盘的「待入场」不显示它.
    code 支持中文名: 唯一命中或精确名称时可直接解析成代码.
    """
    from lib.backtest_data import resolve_stock_query, search_stocks

    payload = payload or {}
    raw = str(payload.get("code", "")).strip()
    strategy = str(payload.get("strategy", "")).strip()
    source = str(payload.get("source", "sim")).strip().lower()
    if source not in ("sim", "real"):
        source = "sim"
    if not raw:
        return {"ok": False, "message": "请填写股票代码或名称 (例: 002432.SZ / 兆易创新)"}
    if not strategy:
        return {"ok": False, "message": "请选择策略"}

    code, name = resolve_stock_query(raw)
    if not code:
        # 多候选时提示前端去点选
        hits = search_stocks(raw, limit=8)
        if hits:
            preview = "、".join(f"{h['name']}({h['code']})" for h in hits[:5])
            return {
                "ok": False,
                "message": f"「{raw}」匹配到多只股票, 请点选一条: {preview}",
                "candidates": hits,
            }
        return {"ok": False, "message": f"未找到股票「{raw}」, 请换代码或更完整的名称"}

    code = _normalize_stock_code(code)

    valid_names = {s["name"] for s in list_strategies()}
    if strategy not in valid_names:
        return {"ok": False, "message": f"未知策略: {strategy}, 有效: {sorted(valid_names)}"}

    try:
        # source=real 时不写 watch_pool (watch_pool 是模拟盘的自选池, 别污染);
        # 但 strategies.per_stock 还是要写, 否则引擎不会算它的信号 (实盘授权用).
        if source == "sim":
            wp = load_watch_pool()
            codes = list(wp.get("codes") or [])
            if code not in codes:
                codes.append(code)
            save_watch_pool(codes)
        else:
            # source=real: 反向把 code 从 watch_pool 拉出来 (兼容旧数据 -- 之前的版本会写入 watch_pool)
            wp = load_watch_pool()
            codes = list(wp.get("codes") or [])
            if code in codes:
                codes = [c for c in codes if c != code]
                save_watch_pool(codes)

        cfg = load_strategy_config()
        per = dict(cfg.get("per_stock") or {})
        per[code] = strategy
        default = cfg.get("default", "macd_1d")
        if default not in valid_names:
            default = "macd_1d"

        msg = _SIM.apply_strategy_config(default=default, per_stock=per)
        _set_binding_source(code, source)
        label = f"{name}({code})" if name else code
        return {
            "ok": True,
            "message": f"已添加 {label} ({source}), 策略: {strategy}。{msg}",
            "code": code,
            "name": name,
            "codes": codes,
            "per_stock": per,
            "default": default,
            "source": source,
        }
    except Exception as e:
        return {"ok": False, "message": str(e)}


@router.post("/stock/unbind")
@router.post("/stock/unbind/")
def stock_unbind(payload: Optional[Dict[str, Any]] = Body(None)):
    """解除某只股票的策略绑定, 并把它从 watch_pool 里移除.

    用途: 实盘里有真实持仓但不想让引擎对它产出 buy/sell 信号
          (典型场景: 自己手动操作, 不想被 AI 误下单).

    注意:
        - mock_positions.yaml 里的初始模拟持仓不会被动 (那是模拟盘演示用的种子数据).
        - 解绑后该 code 仍可能因为在 mock_positions 里残留在 mergedList,
          此时引擎会用 default 策略跑它; 如果连默认信号都不想要, 请同时改 mock_positions.yaml.

    body: {"code": "002432.SZ"}
    """
    payload = payload or {}
    code = _normalize_stock_code(str(payload.get("code", "")))
    if not code:
        return {"ok": False, "message": "请填写股票代码"}

    actions = []
    try:
        wp = load_watch_pool()
        codes = list(wp.get("codes") or [])
        if code in codes:
            codes = [c for c in codes if c != code]
            save_watch_pool(codes)
            actions.append(f"已从 watch_pool 移除")

        cfg = load_strategy_config()
        per = dict(cfg.get("per_stock") or {})
        if code in per:
            per.pop(code, None)
            actions.append(f"已解除策略绑定")
        default = cfg.get("default", "macd_1d")
        valid_names = {s["name"] for s in list_strategies()}
        if default not in valid_names:
            default = "macd_1d"
        msg = _SIM.apply_strategy_config(default=default, per_stock=per)
        _drop_binding_source(code)

        if not actions:
            return {"ok": True,
                    "message": f"{code} 本来就没绑策略 / 不在 watch_pool, 无需操作",
                    "codes": codes, "per_stock": per}

        return {
            "ok": True,
            "message": f"{code} {' + '.join(actions)}; 引擎已热加载, 不会再对该股出信号. {msg}",
            "codes": codes,
            "per_stock": per,
            "default": default,
        }
    except Exception as e:
        return {"ok": False, "message": str(e)}
