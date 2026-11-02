# -*- coding: utf-8 -*-
# Author：@跟陈博士学AI
# 龙头战法路由 -- 候选名单 + 一键加入监控池
"""
GET  /api/dragon/candidates    -- 拉今日龙头候选
                                  数据源优先级 (默认 source=auto):
                                    1) MySQL wucai_trade.* 最近一个真实交易日 (含板块共振 v2 全开)
                                    2) QMT 实时盘中行情 (盘中无板块, 自动关共振)
                                  query 参数:
                                    source=auto|mysql|xtdata   数据源选择, 默认 auto
                                    use_xtdata=1                    旧参数, 等价 source=xtdata
                                    min_change=0.05                 最低涨幅
                                    max_price=30                    最高价格
                                    min_vol_ratio=2.0               最小量比
                                    require_resonance=-1            v2 板块共振:
                                                                    1=开 0=关 -1=按 source 自动
                                                                    (mysql 默认开; QMT 默认关)

POST /api/dragon/bind          -- 一键把候选写入 watch_pool + strategies.per_stock=dragon_picker
                                  body: {"codes": [...], "version": "v1"|"v2"}
                                  绑定后, 主循环下一轮就用 dragon_picker 策略给这些股票自动算
                                  入场 (跟 body.version / strategies.yaml dragon_version)
                                  和出场 (当日跌幅>3% 或 从当日最高回撤>3%) 信号.
POST /api/dragon/version       -- 只切主循环入场版本, body: {"version": "v1"|"v2"}
"""
from __future__ import annotations
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Body, Query

from lib.paths import setup_sys_path
setup_sys_path()

from live_trading.live_loop import _qmt_http
from lib.qmt_backend import can_live_quote, is_miniqmt

from lib.live_simulator import (
    LiveSimRunner, load_strategy_config, load_watch_pool, save_watch_pool,
    set_dragon_version,
)
from dragon_strategy.dragon_picker import (
    filter_dragon_candidates, dragon_version_params,
)
from dragon_strategy.dragon_backtest import (
    load_status_meta,
    load_sector_panel,
    load_daily_panel,
    build_today_candidates,
)


router = APIRouter()
_SIM = LiveSimRunner()

def _dragon_strategy_name(version: str) -> str:
    return f"dragon_picker_{version}"


def _ensure_market_dot(code: str) -> str:
    """补全市场后缀 (002432 -> 002432.SZ; 600519 -> 600519.SH)"""
    code = code.strip().upper()
    if "." in code:
        return code
    if not code.isdigit() or len(code) != 6:
        return code
    if code.startswith(("60", "68", "9")):
        return code + ".SH"
    return code + ".SZ"


def _candidates_from_mysql_latest_day(
    min_change: float, max_change: float, max_price: float,
    min_vol_ratio: float, mcap_range: Tuple[float, float],
    min_listed_days: int, require_sector_resonance: bool, top_k: int,
) -> Tuple[List[Dict[str, Any]], str]:
    """从 MySQL wucai_trade.* 拉「最近一个真实交易日」的全市场涨跌 + 板块共振.

    使用本 CASE 内 dragon_strategy/dragon_backtest 中的 SQL 拼装；失败则抛错。

    返回: (候选列表, 真实交易日 yyyy-MM-dd)
    """
    import pandas as pd
    from datetime import date, timedelta

    # 1) 元信息
    meta = load_status_meta()
    if meta.empty:
        raise RuntimeError("trade_stock_status 为空, 请先同步 MySQL 元数据表")

    # 2) 拉最近 12 个自然日的日 K (足够覆盖一个交易日 + 5 日均量窗口, 节假日富余)
    end_d = date.today()
    start_d = end_d - timedelta(days=12)
    panel = load_daily_panel(start_d.strftime("%Y-%m-%d"), end_d.strftime("%Y-%m-%d"))
    if panel.empty:
        raise RuntimeError(f"trade_stock_daily 在 {start_d}~{end_d} 区间为空, 请先跑日更入库")

    # 3) 拿最近一个真实交易日 (panel 里出现的最大日期)
    latest_t = panel["trade_date"].max()
    latest_str = pd.Timestamp(latest_t).strftime("%Y-%m-%d")

    # 4) 板块面板 (同区间, 用 sector_2 = 申万二级)
    sector_panel = load_sector_panel(start_d.strftime("%Y-%m-%d"),
                                      end_d.strftime("%Y-%m-%d"), sector_level=2)
    if sector_panel.empty and require_sector_resonance:
        raise RuntimeError("trade_sector_daily 为空, 无法做板块共振过滤; 请先准备板块日表")

    # 5) 复用 backtest 的拼装函数 (含 picker 完整 v1+v2 过滤 + dragon_score)
    cands = build_today_candidates(
        panel, meta, sector_panel, latest_t,
        min_change=min_change,
        max_change=max_change,
        max_price=max_price,
        min_vol_ratio=min_vol_ratio,
        mcap_range=mcap_range,
        min_listed_days=min_listed_days,
        require_sector_resonance=require_sector_resonance,
        top_k=top_k,
    )
    return cands, latest_str


def _a_share_elapsed_minutes() -> int:
    """A 股已开盘分钟 (9:30-11:30 + 13:00-15:00, 满 240)。"""
    from datetime import datetime, time
    t = datetime.now().time()
    if t < time(9, 30):
        return 0
    if t < time(11, 30):
        return (t.hour - 9) * 60 + t.minute - 30
    if t < time(13, 0):
        return 120
    if t < time(15, 0):
        return 120 + (t.hour - 13) * 60 + t.minute
    return 240


def _load_vol5_map() -> Dict[str, float]:
    """近 5 个完整交易日均量, 给盘中量比当分母。"""
    from dragon_strategy.db_config import execute_query
    days = execute_query(
        """
        SELECT DISTINCT trade_date FROM trade_stock_daily
        WHERE trade_date < CURDATE()
        ORDER BY trade_date DESC
        LIMIT 5
        """
    )
    if not days:
        raise RuntimeError("trade_stock_daily 没有可用于量比的历史交易日")
    dates = [r["trade_date"] for r in days]
    rows = execute_query(
        """
        SELECT stock_code, AVG(volume) AS vol5
        FROM trade_stock_daily
        WHERE trade_date IN (%s)
        GROUP BY stock_code
        """ % ",".join(["%s"] * len(dates)),
        dates,
    )
    return {r["stock_code"]: float(r["vol5"] or 0) for r in rows if r["vol5"]}


def _candidates_from_xtdata(min_change: float, max_price: float,
                             min_vol_ratio: float) -> List[Dict[str, Any]]:
    """开盘时段从 QMT 抓全市场涨跌, 拼出 dragon_picker 需要的字段。
    WUCAI_QMT 默认 qmt, 走大 QMT HTTP; 仅 miniqmt/xtquant 才走 xtdata。
    量比用盘中真口径: 今日累计量 / (近5日均量 × 已开盘分钟/240)。
    """
    if is_miniqmt():
        from xtquant import xtdata
        xtdata.connect()
        sectors = xtdata.get_stock_list_in_sector("沪深A股") or []
        if not sectors:
            raise RuntimeError("get_stock_list_in_sector 返回空")
        ticks = xtdata.get_full_tick(sectors) or {}
    else:
        sec = _qmt_http('/api/data/stock_list_in_sector', {"sectorname": "沪深A股"})
        sectors = (sec or {}).get("stocks", []) or []
        if not sectors:
            raise RuntimeError("get_stock_list_in_sector 返回空")
        # 全市场 tick 分批拉（一次传太多代码可能超时）
        ticks = {}
        for i in range(0, len(sectors), 800):
            batch = sectors[i:i + 800]
            tk = _qmt_http('/api/data/full_tick', {"stocks": ",".join(batch)})
            if isinstance(tk, dict):
                ticks.update({k: v for k, v in tk.items() if k != "error"})
    elapsed = _a_share_elapsed_minutes()
    if elapsed <= 0:
        raise RuntimeError("未开盘, 无法按已开盘分钟计算量比")
    vol5_map = _load_vol5_map()
    from dragon_strategy.db_config import execute_query
    share_rows = execute_query("SELECT stock_code, float_shares FROM trade_stock_status")
    share_map = {r["stock_code"]: float(r["float_shares"] or 0) for r in share_rows}
    out: List[Dict[str, Any]] = []
    for code, t in ticks.items():
        try:
            last = float(t.get("lastPrice", 0))
            pre  = float(t.get("lastClose", 0))
            if last <= 0 or pre <= 0:
                continue
            vol5 = vol5_map.get(code, 0)
            fshare = share_map.get(code, 0)
            if vol5 <= 0 or fshare <= 0:
                continue
            chg = last / pre - 1.0
            vol = float(t.get("volume", 0))
            expect = vol5 * (elapsed / 240.0)
            vr = vol / expect if expect > 0 else 0.0
            mcap = fshare * last
            out.append({
                "code":            code,
                "name":            t.get("instrumentName", "") or "",
                "day_change_pct":  chg,
                "price":           last,
                "volume_ratio":    vr,
                "float_market_cap": mcap,
            })
        except Exception:
            continue
    if not out:
        raise RuntimeError("QMT 候选构造为空")
    return out


@router.get("/candidates")
def candidates(
    source: str = Query("auto", description="auto / mysql / xtdata"),
    version: str = Query("v2", description="v1 对照 / v2 默认, 与课件回测同一套"),
    use_xtdata: int = Query(0, description="(旧) 1=等价 source=xtdata"),
    min_change: float = Query(0.05),
    max_price: float = Query(30.0),
    min_vol_ratio: float = Query(2.0),
    require_resonance: int = Query(-1,
        description="板块共振: 1=开 0=关 -1=跟 version (v1 关; v2 时 mysql 开、QMT 关)"),
):
    """返回龙头候选 (按 dragon_score 降序).

    version=v2: 涨幅<9.5% + 上市>=60日 + 共振(仅 MySQL)
    version=v1: 关共振 / 不限涨停 / 不限次新
    source=auto: 先 mysql, 无候选再 QMT.
    """
    if use_xtdata == 1 and source == "auto":
        source = "xtdata"
    source = (source or "auto").lower()
    if source not in ("auto", "mysql", "xtdata"):
        source = "auto"
    if not can_live_quote() and source != "mysql":
        source = "mysql"
    version = (version or "").strip().lower()
    if version not in ("v1", "v2"):
        return {
            "ok": False, "source": source, "version": version,
            "warning": "version 只能是 v1 或 v2", "count": 0, "items": [],
        }

    # 与课件回测对照: v1 关共振+不限涨停+不限次新; v2 三条补丁全开
    vparams = dragon_version_params(version)
    max_change = vparams["max_change"]
    min_listed_days = vparams["min_listed_days"]

    err: Optional[str] = None
    used_source = source
    trade_date: Optional[str] = None
    cands: List[Dict[str, Any]] = []

    def _resolve_resonance(src: str) -> bool:
        if require_resonance == 0:
            return False
        if require_resonance == 1:
            return True
        if version == "v1":
            return False
        return src == "mysql"

    if source in ("auto", "mysql"):
        try:
            res_on = _resolve_resonance("mysql")
            cands, trade_date = _candidates_from_mysql_latest_day(
                min_change=min_change, max_change=max_change,
                max_price=max_price, min_vol_ratio=min_vol_ratio,
                mcap_range=(30e8, 500e8), min_listed_days=min_listed_days,
                require_sector_resonance=res_on, top_k=20,
            )
            used_source = "mysql"
        except Exception as e:
            err = f"mysql: {type(e).__name__}: {e}"
            if source == "mysql":
                return {
                    "ok": False, "source": "mysql", "version": version,
                    "warning": err, "count": 0, "items": [],
                }
            cands = []

    if not cands and source in ("auto", "xtdata"):
        try:
            raw = _candidates_from_xtdata(min_change, max_price, min_vol_ratio)
            res_on = _resolve_resonance("xtdata")
            cands = filter_dragon_candidates(
                raw,
                min_change=min_change, max_price=max_price,
                mcap_range=(30e8, 500e8), min_volume_ratio=min_vol_ratio,
                require_sector_resonance=res_on,
                max_change=max_change, min_listed_days=min_listed_days,
            )
            used_source = "xtdata"
            err = None if used_source != "auto" else err
        except Exception as e:
            xt_err = f"QMT: {type(e).__name__}: {e}"
            err = (err + " | " + xt_err) if err else xt_err
            if source == "xtdata":
                return {
                    "ok": False, "source": "xtdata", "version": version,
                    "warning": err, "count": 0, "items": [],
                }

    return {
        "ok":         True,
        "source":     used_source,
        "version":    version,
        "trade_date": trade_date,
        "warning":    err,
        "params":     {
            "min_change": min_change, "max_price": max_price,
            "min_vol_ratio": min_vol_ratio,
            "max_change": max_change, "min_listed_days": min_listed_days,
            "require_sector_resonance": _resolve_resonance(used_source),
        },
        "count":      len(cands),
        "items":      cands[:20],
    }


@router.post("/bind")
def bind(payload: Optional[Dict[str, Any]] = Body(None)):
    """一键把若干候选股加入 watch_pool, 并把策略绑定到 dragon_picker (热加载)"""
    payload = payload or {}
    raw_codes = payload.get("codes") or []
    codes = [_ensure_market_dot(str(c)) for c in raw_codes if str(c).strip()]
    if not codes:
        return {"ok": False, "message": "codes 不能为空"}
    version = str(payload.get("version") or "").strip().lower()
    if version not in ("v1", "v2"):
        return {"ok": False, "message": "version 只能是 v1 或 v2"}

    wp = load_watch_pool()
    pool = list(wp.get("codes") or [])
    added = []
    for c in codes:
        if c not in pool:
            pool.append(c)
            added.append(c)
    save_watch_pool(pool)

    cfg = load_strategy_config()
    per = dict(cfg.get("per_stock") or {})
    strat_name = _dragon_strategy_name(version)
    for c in codes:
        per[c] = strat_name
    default = cfg.get("default", "macd_1d")
    msg = _SIM.apply_strategy_config(default=default, per_stock=per, dragon_version=version)

    return {
        "ok":         True,
        "message":    f"已加入 {len(added)} 只新代码, 共 {len(codes)} 只绑定 {strat_name}。{msg}",
        "added":      added,
        "all_codes":  pool,
        "strategy":   strat_name,
        "version":    version,
    }


@router.post("/version")
def set_version(payload: Optional[Dict[str, Any]] = Body(None)):
    """把主循环 strat_dragon 入场切到 v1 或 v2, 写进 strategies.yaml"""
    payload = payload or {}
    version = str(payload.get("version") or "").strip().lower()
    if version not in ("v1", "v2"):
        return {"ok": False, "message": "version 只能是 v1 或 v2"}
    set_dragon_version(version)
    return {
        "ok": True,
        "version": version,
        "message": f"主循环龙头入场已切到 {version}",
    }
