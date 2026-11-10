# -*- coding: utf-8 -*-
# Author：@跟陈博士学AI
# 复盘归因路由 -- REST
"""
POST /api/review/trade_record         -- 加载交割单 (CSV / 模拟盘 state.json) + 成本汇总
POST /api/review/brinson              -- Brinson 三因子归因 (示例数据)
POST /api/review/brinson_real         -- Brinson 三因子归因 (sim 成交 / real 快照 / csv)
GET  /api/review/wf_strategies        -- 列出 Walk-Forward 可选策略 + 默认参数
POST /api/review/walk_forward         -- Walk-Forward 滚动窗口验证 + 过拟合检测
GET  /api/review/registry             -- 读策略生命周期 registry (课件示例, 页面不再用)
POST /api/review/lifecycle_eval       -- 跑生命周期评估 (课件示例, 页面不再用)
POST /api/review/lifecycle_sim_eval   -- 按策略+股票体检: 继续观察 / 可以上实盘 / 建议停用
"""

from __future__ import annotations
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from fastapi import APIRouter, Body

from lib.paths import (
    setup_sys_path, OUTPUTS_DIR, OUTPUTS_LIVE_STATE, OUTPUTS_EVOLVE_REGISTRY,
)
setup_sys_path()

router = APIRouter()


# ============================================================
# 实盘 NAV 历史持久化 (lifecycle real 模式专用)
# ============================================================
# 实盘没有"账户级历史净值"接口, 我们自己累积 daily snapshot 到这个文件
# 文件结构: {"baseline_total_asset": float, "history": [{"ts": str, "total_asset": float, "cum_pct": float}, ...]}
REAL_PNL_HISTORY_PATH: Path = OUTPUTS_DIR / "real_pnl_history.json"


def _load_real_pnl_history() -> dict:
    if not REAL_PNL_HISTORY_PATH.exists():
        return {"baseline_total_asset": None, "history": []}
    try:
        return json.loads(REAL_PNL_HISTORY_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"baseline_total_asset": None, "history": []}


def _save_real_pnl_history(data: dict) -> None:
    REAL_PNL_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    REAL_PNL_HISTORY_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _record_real_pnl_snapshot(force: bool = False) -> dict:
    """拉一次 real 账户 total_asset, 写入 real_pnl_history.json (每日最多一条)

    Args:
        force: True 强制写, 即使今天已有记录 (覆盖); False 当日已有则跳过

    Returns:
        {"ok": bool, "message": str, "baseline": float|None,
         "today_total": float|None, "today_pct": float|None,
         "data_points": int}
    """
    import os as _os
    qmt_path = _os.getenv("QMT_PATH")
    account_id = _os.getenv("ACCOUNT_ID")
    if not qmt_path or not account_id:
        return {"ok": False, "message": "QMT_PATH / ACCOUNT_ID 未配置 (.env)"}

    try:
        setup_sys_path()
        from qmt_trader_v2 import QMTTraderV2  # type: ignore
        trader = QMTTraderV2(
            qmt_path=qmt_path, account_id=account_id,
            enable_heartbeat=False, enable_reconnect=False,
        )
        trader.connect()
        asset = trader.query_asset() or {}
    except Exception as e:
        return {"ok": False, "message": f"miniQMT 连接/查询失败: {type(e).__name__}: {e}"}

    total_asset = float(asset.get("total_asset") or 0)
    if total_asset <= 0:
        return {"ok": False, "message": f"total_asset = {total_asset}, 实盘账户为空或未连接"}

    data = _load_real_pnl_history()
    history = data.get("history") or []

    today = datetime.now().strftime("%Y-%m-%d")
    today_records = [h for h in history if str(h.get("ts", ""))[:10] == today]

    # baseline: 第一次记录时锁定
    if data.get("baseline_total_asset") is None:
        data["baseline_total_asset"] = total_asset
        baseline = total_asset
    else:
        baseline = float(data["baseline_total_asset"])

    cum_pct = (total_asset - baseline) / baseline if baseline > 0 else 0.0
    new_record = {
        "ts": datetime.now().strftime("%Y-%m-%dT15:00:00"),
        "total_asset": round(total_asset, 2),
        "cum_pct": round(cum_pct, 6),
    }

    if today_records and not force:
        # 当日已有记录, 不重复写
        msg = f"今天 {today} 已有快照, 跳过 (用 force=true 覆盖)"
    else:
        if today_records and force:
            history = [h for h in history if str(h.get("ts", ""))[:10] != today]
        history.append(new_record)
        history.sort(key=lambda x: str(x.get("ts", "")))
        data["history"] = history
        _save_real_pnl_history(data)
        msg = (f"已记录 {today} 快照: total_asset={total_asset:.2f}, "
               f"baseline={baseline:.2f}, cum={cum_pct*100:+.2f}%")

    return {
        "ok": True,
        "message": msg,
        "baseline": round(baseline, 2),
        "today_total": round(total_asset, 2),
        "today_pct": round(cum_pct, 6),
        "data_points": len(data.get("history") or []),
    }


# ------------------------------------------------------------
# Brinson 归因
# ------------------------------------------------------------

@router.get("/trade_csv_files")
@router.get("/trade_csv_files/")
def trade_csv_files_endpoint():
    """列出 data 目录下所有可用的交割单 CSV (用于前端下拉选择).

    response: {
        "ok":      true,
        "default": "<absolute path>",      # 默认选中那份 (最新区间)
        "files":   [{name, path, label, start, end}, ...],
    }
    """
    try:
        from attribution.trade_record import list_available_csvs, DEFAULT_CSV_PATH
        files = list_available_csvs()
        return {
            "ok":      True,
            "default": str(DEFAULT_CSV_PATH),
            "files":   files,
        }
    except Exception as e:
        return {"ok": False, "message": f"{type(e).__name__}: {e}"}


@router.post("/trade_record")
@router.post("/trade_record/")
def trade_record_endpoint(payload: dict = Body({})):
    """加载交易记录 (CSV 交割单 / 模拟盘 state.json) + 自动算成本汇总.

    body: {
        "source":   "csv" | "sim" (默认 "csv"),
        "csv_path": 可选, 默认走项目内置 data/历史成交*.csv
    }

    response: {
        "ok":           true,
        "source":       "csv",
        "source_label": "CSV (历史成交_xxx.csv)",
        "real_cost":    true,                 # CSV 是真实手续费, sim 是估算
        "rows":         [...],                # 标准化订单明细
        "summary":      {n, commission, stamp_duty, transfer_fee, total_cost, amount, bps}
    }
    """
    source = str(payload.get("source") or "csv").strip()
    try:
        from attribution.trade_record import (
            load_from_csv, load_from_state_json, add_costs,
            DEFAULT_CSV_PATH, DEFAULT_SIM_STATE,
        )
        if source == "csv":
            csv_path = str(payload.get("csv_path") or DEFAULT_CSV_PATH)
            p = Path(csv_path)
            if not p.exists():
                return {"ok": False, "message": f"CSV 文件不存在: {p.name}"}
            df = load_from_csv(csv_path)
            source_label = f"CSV ({p.name})"
            real_cost = True
        elif source == "sim":
            state_p = Path(DEFAULT_SIM_STATE)
            if not state_p.exists():
                return {"ok": False, "message": "模拟盘 live_state.json 还没生成, 模拟盘可能未启动"}
            df = load_from_state_json(str(state_p))
            df = add_costs(df) if not df.empty else df
            source_label = f"模拟盘 ({state_p.name})"
            real_cost = False
        else:
            return {"ok": False, "message": f"未知数据源: {source}"}

        # CSV 解析失败 (文件存在但内容空 / 格式错) -- 这是真错误
        if df.empty and source == "csv":
            return {"ok": False, "message": "CSV 解析后为空, 请检查文件格式"}

        # sim 模式 + orders 空 -- 不是错误, 跑一笔模拟下单才会有 orders. 返回 ok=True 让前端展示友好提示
        if df.empty and source == "sim":
            return {
                "ok":           True,
                "source":       source,
                "source_label": source_label,
                "real_cost":    real_cost,
                "rows":         [],
                "summary":      {"n": 0, "commission": 0.0, "stamp_duty": 0.0,
                                 "transfer_fee": 0.0, "total_cost": 0.0,
                                 "amount": 0.0, "bps": 0.0},
                "empty_reason": "模拟盘当前没有订单流水 (live_state.json 里 orders 字段为空). "
                                "跑一笔模拟下单后这里会显示订单明细 + 成本核算. "
                                "下方 Brinson 归因同样依赖这笔流水, 没有订单就无法按成交复盘.",
            }

        # NaN -> 0 + float 化, 让前端 JSON 不爆
        for col in ("price", "amount", "commission", "stamp_duty",
                    "transfer_fee", "settle_fee", "total_cost"):
            if col in df.columns:
                df[col] = df[col].fillna(0).astype(float)
        rows = df.to_dict(orient="records")

        amount_sum = float(df["amount"].sum()) if "amount" in df else 0.0
        total_cost_sum = float(df["total_cost"].sum()) if "total_cost" in df else 0.0
        summary = {
            "n":            int(len(df)),
            "commission":   float(df["commission"].sum()) if "commission" in df else 0.0,
            "stamp_duty":   float(df["stamp_duty"].sum()) if "stamp_duty" in df else 0.0,
            "transfer_fee": float(df["transfer_fee"].sum()) if "transfer_fee" in df else 0.0,
            "total_cost":   total_cost_sum,
            "amount":       amount_sum,
            "bps":          (total_cost_sum / amount_sum * 10000) if amount_sum > 0 else 0.0,
        }
        return {
            "ok":           True,
            "source":       source,
            "source_label": source_label,
            "real_cost":    real_cost,
            "rows":         rows,
            "summary":      summary,
        }
    except Exception as e:
        return {"ok": False, "message": f"{type(e).__name__}: {e}"}


@router.post("/brinson")
def brinson(payload: dict = Body({})):
    from attribution.brinson import run_demo_brinson

    result, scenario = run_demo_brinson()

    by_industry = []
    if hasattr(result, "by_industry"):
        df = result.by_industry
        for ind, row in df.iterrows():
            by_industry.append({
                "industry":    row.get("industry", ind),
                "Wp":          float(row.get("Wp", 0)),
                "Wb":          float(row.get("Wb", 0)),
                "Rp":          float(row.get("Rp", 0)),
                "Rb":          float(row.get("Rb", 0)),
                "allocation":  float(row.get("allocation", 0)),
                "selection":   float(row.get("selection", 0)),
                "interaction": float(row.get("interaction", 0)),
                "total":       float(row.get("total", 0)),
            })

    return {
        "source":             "demo",
        "scenario":           scenario,
        "portfolio_return":   result.portfolio_return,
        "benchmark_return":   result.benchmark_return,
        "excess_return":      result.excess_return,
        "allocation_effect":  result.allocation_effect,
        "selection_effect":   result.selection_effect,
        "interaction_effect": result.interaction_effect,
        "by_industry":        by_industry,
    }


# ------------------------------------------------------------
# Brinson 归因 -- 真实数据 (V1: sim 持仓 + 沪深300 等权基准 + 申万一级)
# ------------------------------------------------------------

@router.post("/brinson_real")
@router.post("/brinson_real/")
def brinson_real(payload: dict = Body({})):
    """真实数据 Brinson 归因 (申万一级 + 沪深300)

    body: {
        "start":     "YYYY-MM-DD" 区间起始 (含, 默认 2026-04-01),
        "end":       "YYYY-MM-DD" 区间结束 (含, 默认今天),
        "benchmark": "沪深300" (V1 只支持这一个),
        "source":    数据源:
                     - "sim"  (默认): 模拟盘 live_state.json 订单流水
                     - "real" / "csv": 实盘交割单 CSV (已平仓 + 期末未平仓)
        "csv_path":  source 为 real / csv 时的交割单路径 (可选, 默认 data/历史成交*.csv)
    }
    """
    from datetime import date
    start = str(payload.get("start") or "2026-04-01").strip()
    end = str(payload.get("end") or date.today().isoformat()).strip()
    benchmark = str(payload.get("benchmark") or "沪深300").strip()
    source = str(payload.get("source") or "sim").strip()
    market = str(payload.get("market") or "cn").strip().lower()

    try:
        from attribution.trade_record import DEFAULT_CSV_PATH
        csv_path = str(payload.get("csv_path") or DEFAULT_CSV_PATH)
        if market == "us":
            from importlib.util import module_from_spec, spec_from_file_location
            us_file = (Path(__file__).resolve().parents[3]
                       / "23-团队复盘-持续进化" / "CASEA-Brinson归因"
                       / "attribution" / "brinson_us.py")
            spec = spec_from_file_location("brinson_us", us_file)
            mod = module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod.compute_us_brinson(
                start=start, end=end, source=source,
                state_file=str(OUTPUTS_LIVE_STATE),
                csv_path=csv_path,
            )
        if market != "cn":
            return {"ok": False, "message": f"market 必须是 cn / us, 收到 {market}"}
        from lib.brinson_real import compute_real_brinson
        return compute_real_brinson(
            start=start, end=end, benchmark=benchmark,
            source=source, csv_path=csv_path,
        )
    except Exception as e:
        return {"ok": False, "message": f"{type(e).__name__}: {e}"}


@router.post("/industry_map_refresh")
@router.post("/industry_map_refresh/")
def industry_map_refresh(payload: dict = Body({})):
    """强制重建申万一级行业字典 cache (默认 7 天 TTL, 可手动刷新)"""
    try:
        from lib.brinson_real import build_industry_map
        data = build_industry_map(force_refresh=True)
        return {
            "ok": True,
            "built_at":   data.get("built_at"),
            "sw1_count":  data.get("sw1_count"),
            "hs300_count": len(data.get("hs300") or []),
            "message": f"已重建申万一级字典: {data.get('sw1_count')} 个一级行业, "
                       f"沪深300 {len(data.get('hs300') or [])} 只成分股",
        }
    except Exception as e:
        return {"ok": False, "message": f"{type(e).__name__}: {e}"}


# ------------------------------------------------------------
# Walk-Forward 过拟合检测
# ------------------------------------------------------------
# 策略注册表: 4 个内置策略, 覆盖 趋势 / 均值回归 / 突破 三大经典风格.
# 每个策略定义:
#   fn_name      -- parameter_tuning/walk_forward.py 里的函数名
#   label        -- 前端下拉显示文字
#   description  -- 一句话风格说明
#   param_cols   -- 参数列名 (顺序对应 textarea 每行的逗号分隔字段)
#   defaults     -- 默认参数候选 (textarea 初始值, 也是"重置默认"的内容)
#   bounds       -- 每列的合理范围 + 类型 (用来校验 + 业务约束)
# ------------------------------------------------------------

_WF_STRATEGIES: Dict[str, Dict[str, Any]] = {
    "double_ma": {
        "fn_name":     "double_ma_strategy",
        "label":       "双均线 (趋势跟踪)",
        "description": "短均线上穿长均线买入, 下穿卖出. 适合趋势行情, 横盘易过拟合.",
        "param_cols":  ["ma_short", "ma_long"],
        "defaults": [
            {"ma_short": 5,  "ma_long": 20},
            {"ma_short": 5,  "ma_long": 30},
            {"ma_short": 10, "ma_long": 30},
            {"ma_short": 10, "ma_long": 60},
            {"ma_short": 20, "ma_long": 60},
            {"ma_short": 20, "ma_long": 120},
        ],
        "bounds": {
            "ma_short": {"type": "int", "min": 2, "max": 200},
            "ma_long":  {"type": "int", "min": 3, "max": 500},
        },
        "extra_check": "ma_short < ma_long",
    },
    "bollinger": {
        "fn_name":     "bollinger_reversion_strategy",
        "label":       "布林带反转 (均值回归)",
        "description": "收盘触下轨买入 (超卖), 触上轨卖出 (超买). 适合震荡, 单边趋势会被反向打脸.",
        "param_cols":  ["window", "std_n"],
        "defaults": [
            {"window": 10, "std_n": 1.5},
            {"window": 20, "std_n": 2.0},
            {"window": 20, "std_n": 2.5},
            {"window": 30, "std_n": 2.0},
            {"window": 50, "std_n": 2.0},
        ],
        "bounds": {
            "window": {"type": "int",   "min": 5,   "max": 200},
            "std_n":  {"type": "float", "min": 0.5, "max": 5.0},
        },
    },
    "rsi": {
        "fn_name":     "rsi_reversion_strategy",
        "label":       "RSI 反转 (均值回归)",
        "description": "RSI 跌破 oversold 买入, 涨破 overbought 卖出. 经典超买超卖, 强趋势失效.",
        "param_cols":  ["rsi_period", "oversold", "overbought"],
        "defaults": [
            {"rsi_period": 6,  "oversold": 20, "overbought": 80},
            {"rsi_period": 14, "oversold": 30, "overbought": 70},
            {"rsi_period": 14, "oversold": 25, "overbought": 75},
            {"rsi_period": 21, "oversold": 30, "overbought": 70},
        ],
        "bounds": {
            "rsi_period": {"type": "int",   "min": 2,  "max": 100},
            "oversold":   {"type": "float", "min": 5,  "max": 50},
            "overbought": {"type": "float", "min": 50, "max": 95},
        },
        "extra_check": "oversold < overbought",
    },
    "donchian": {
        "fn_name":     "donchian_breakout_strategy",
        "label":       "唐奇安通道突破 (海龟)",
        "description": "突破 N 日新高买入, 跌破 M 日新低卖出 (M <= N). 趋势好, 横盘连续假突破.",
        "param_cols":  ["entry_lookback", "exit_lookback"],
        "defaults": [
            {"entry_lookback": 10, "exit_lookback": 5},
            {"entry_lookback": 20, "exit_lookback": 10},
            {"entry_lookback": 20, "exit_lookback": 5},
            {"entry_lookback": 55, "exit_lookback": 20},
        ],
        "bounds": {
            "entry_lookback": {"type": "int", "min": 3, "max": 250},
            "exit_lookback":  {"type": "int", "min": 2, "max": 250},
        },
        "extra_check": "exit_lookback <= entry_lookback",
    },
    "ml_prob": {
        "fn_name":     "ml_prob_strategy",
        "label":       "ML 概率因子 (XGBoost)",
        "description": "50+ 技术因子 + XGBoost 滚动训练, 涨概率 > buy_th 买 / < sell_th 卖. 比简单技术策略 OOS 衰减小.",
        "param_cols":  ["train_days", "retrain_interval", "buy_th", "sell_th"],
        "defaults": [
            {"train_days": 80,  "retrain_interval": 20, "buy_th": 0.55, "sell_th": 0.45},
            {"train_days": 120, "retrain_interval": 20, "buy_th": 0.60, "sell_th": 0.40},
            {"train_days": 120, "retrain_interval": 40, "buy_th": 0.55, "sell_th": 0.45},
            {"train_days": 180, "retrain_interval": 20, "buy_th": 0.60, "sell_th": 0.40},
        ],
        "bounds": {
            "train_days":       {"type": "int",   "min": 40,   "max": 500},
            "retrain_interval": {"type": "int",   "min": 1,    "max": 120},
            "buy_th":           {"type": "float", "min": 0.50, "max": 0.95},
            "sell_th":          {"type": "float", "min": 0.05, "max": 0.50},
        },
        "extra_check": "sell_th < buy_th",
    },
}


def _wf_indicator_warmup(strategy: str, param_grid: List[dict]) -> int:
    """训练/评估窗口前必须回看的 K 线数, 否则长周期指标在短窗里算不出来.

    按当前 param_grid 取最大回望, 不写死, 学员改候选后 warmup 跟着变.
    """
    if strategy == "double_ma":
        return max(int(p["ma_long"]) for p in param_grid)
    if strategy == "bollinger":
        return max(int(p["window"]) for p in param_grid)
    if strategy == "rsi":
        return max(int(p["rsi_period"]) for p in param_grid) + 1
    if strategy == "donchian":
        return max(max(int(p["entry_lookback"]), int(p["exit_lookback"])) for p in param_grid)
    if strategy == "ml_prob":
        return max(int(p["train_days"]) for p in param_grid) + 20
    raise KeyError(strategy)


def _wf_strategy_meta(strategy: str) -> Dict[str, Any]:
    """取策略 meta, 未知策略抛 KeyError"""
    if strategy not in _WF_STRATEGIES:
        raise KeyError(strategy)
    return _WF_STRATEGIES[strategy]


def _wf_param_grid_to_text(strategy: str, grid: List[dict]) -> str:
    """把 param_grid 列表序列化成 textarea 文本 (每行逗号分隔, 顺序按 param_cols)"""
    cols = _wf_strategy_meta(strategy)["param_cols"]
    lines = []
    for item in grid:
        vals = [item.get(c) for c in cols]
        lines.append(",".join(str(v) for v in vals))
    return "\n".join(lines)


def _coerce_param_value(col: str, raw_v: str, bounds: Dict[str, Any]) -> tuple:
    """按 bounds[col] 把字符串转成 int/float, 并校验范围. 返回 (value, err_msg)"""
    spec = bounds.get(col, {"type": "float"})
    typ = spec.get("type", "float")
    try:
        v = int(raw_v) if typ == "int" else float(raw_v)
    except ValueError:
        return None, f"{col} 应为 {typ} 类型, 当前 '{raw_v}'"
    if "min" in spec and v < spec["min"]:
        return None, f"{col}={v} 小于下限 {spec['min']}"
    if "max" in spec and v > spec["max"]:
        return None, f"{col}={v} 大于上限 {spec['max']}"
    return v, ""


def _check_extra(strategy: str, item: Dict[str, Any]) -> str:
    """跨字段约束 (e.g. ma_short < ma_long). 通过返回 '', 否则返回错误文本."""
    rule = _wf_strategy_meta(strategy).get("extra_check")
    if not rule:
        return ""
    if rule == "ma_short < ma_long":
        if not (item["ma_short"] < item["ma_long"]):
            return f"需满足 ma_short < ma_long, 当前 ({item['ma_short']},{item['ma_long']})"
    elif rule == "oversold < overbought":
        if not (item["oversold"] < item["overbought"]):
            return f"需满足 oversold < overbought, 当前 ({item['oversold']},{item['overbought']})"
    elif rule == "exit_lookback <= entry_lookback":
        if not (item["exit_lookback"] <= item["entry_lookback"]):
            return f"需满足 exit_lookback <= entry_lookback, 当前 ({item['entry_lookback']},{item['exit_lookback']})"
    elif rule == "sell_th < buy_th":
        if not (item["sell_th"] < item["buy_th"]):
            return f"需满足 sell_th < buy_th, 当前 (buy_th={item['buy_th']}, sell_th={item['sell_th']})"
    return ""


def _parse_wf_param_grid(strategy: str, raw) -> tuple:
    """把前端送来的 param_grid 规范化为 List[dict] (字段顺序按 strategy.param_cols).

    支持两种格式:
        1. 数组对象: [{"ma_short": 5, "ma_long": 20}, ...]
        2. 文本行 (textarea): "5,20\n10,30"  -- 每行用逗号/Tab 分隔, 顺序按 param_cols

    raw 为空/None 时返回该策略的默认 grid.
    返回 (param_grid, error_message); 成功时 error 为空串.
    """
    try:
        meta = _wf_strategy_meta(strategy)
    except KeyError:
        return [], f"未知策略: {strategy}"
    cols = meta["param_cols"]
    bounds = meta.get("bounds", {})

    if raw is None or (isinstance(raw, str) and raw.strip() == ""):
        return [dict(d) for d in meta["defaults"]], ""

    # 文本格式 (textarea)
    if isinstance(raw, str):
        items = []
        for ln_idx, ln in enumerate(raw.splitlines(), start=1):
            ln = ln.strip()
            if not ln or ln.startswith("#"):
                continue
            parts = [p.strip() for p in ln.replace("\t", ",").split(",") if p.strip()]
            if len(parts) != len(cols):
                return [], (f"第 {ln_idx} 行字段数 {len(parts)} != {len(cols)} "
                            f"(应为 '{','.join(cols)}'): {ln}")
            item: Dict[str, Any] = {}
            for col, raw_v in zip(cols, parts):
                v, err = _coerce_param_value(col, raw_v, bounds)
                if err:
                    return [], f"第 {ln_idx} 行 {err}"
                item[col] = v
            err = _check_extra(strategy, item)
            if err:
                return [], f"第 {ln_idx} 行 {err}"
            items.append(item)
        if not items:
            return [dict(d) for d in meta["defaults"]], ""
        raw = items

    # 数组对象格式
    if not isinstance(raw, list):
        return [], "param_grid 应为数组或多行文本"
    out = []
    for i, src in enumerate(raw):
        if not isinstance(src, dict):
            return [], f"param_grid[{i}] 不是 dict"
        item: Dict[str, Any] = {}
        for col in cols:
            if col not in src:
                return [], f"param_grid[{i}] 缺少字段 {col}"
            v, err = _coerce_param_value(col, str(src[col]), bounds)
            if err:
                return [], f"param_grid[{i}] {err}"
            item[col] = v
        err = _check_extra(strategy, item)
        if err:
            return [], f"param_grid[{i}] {err}"
        out.append(item)
    if not out:
        return [], "param_grid 不能为空"
    if len(out) > 30:
        return [], f"param_grid 最多 30 组 (当前 {len(out)} 组)"
    return out, ""


@router.get("/wf_strategies")
def wf_strategies():
    """列出 Walk-Forward 可选策略 + 默认参数 (供前端下拉初始化)"""
    out = []
    for key, meta in _WF_STRATEGIES.items():
        out.append({
            "key":            key,
            "label":          meta["label"],
            "description":    meta["description"],
            "param_cols":     list(meta["param_cols"]),
            "param_text_hint": ",".join(meta["param_cols"]),
            "defaults_text":  _wf_param_grid_to_text(key, meta["defaults"]),
            "defaults_count": len(meta["defaults"]),
        })
    return {"ok": True, "strategies": out}


@router.get("/sim_holdings")
@router.get("/sim_holdings/")
def sim_holdings():
    """Walk-Forward 快捷标的: 模拟盘账本里的全部持仓标的。"""
    if not OUTPUTS_LIVE_STATE.exists():
        return {"ok": False, "message": "模拟盘还没有 live_state.json", "holdings": []}
    try:
        state = json.loads(OUTPUTS_LIVE_STATE.read_text(encoding="utf-8"))
    except Exception as e:
        return {"ok": False, "message": f"读模拟盘失败: {type(e).__name__}: {e}", "holdings": []}
    holdings = []
    for pos in state.get("positions") or []:
        code = str(pos.get("code") or "").strip()
        volume = int(pos.get("volume") or 0)
        if not code:
            continue
        name = str(pos.get("name") or "").strip()
        ticker = code[:-3] if code.endswith((".US", ".SH", ".SZ", ".BJ")) else code
        holdings.append({
            "code": code,
            "name": name,
            "volume": volume,
            "label": f"{name} {ticker}" if name else code,
        })
    return {"ok": True, "holdings": holdings}


@router.post("/walk_forward")
@router.post("/walk_forward/")
def walk_forward_endpoint(payload: dict = Body({})):
    """Walk-Forward 滚动窗口验证 + 过拟合检测

    body: {
        "code":       "600519.SH" 或 "RKLB" / "RKLB.US",
        "count":      800           (回望多少根日 K, 默认 800),
        "train":      120           (训练窗口, 默认 120),
        "test":       60            (评估窗口, 默认 60),
        "strategy":   "double_ma" | "bollinger" | "rsi" | "donchian" (默认 double_ma),
        "param_grid": 数组或多行文本 (按所选策略的 param_cols 顺序, 留空用默认),
    }

    返回:
        ok / code / name / params / summary / windows / verdict / verdict_text
        - summary.overfit_score: < 0.3 鲁棒, 0.3~0.7 中度过拟合, > 0.7 严重过拟合
        - summary.is_oos_ratio:  接近 1.0 训练/评估表现一致, < 0.5 严重过拟合
    """
    import time as _time

    from parameter_tuning.walk_forward import normalize_market_code
    code = normalize_market_code(str(payload.get("code") or "600519.SH"))
    strategy = str(payload.get("strategy") or "double_ma").strip()
    if strategy not in _WF_STRATEGIES:
        return {"ok": False,
                "message": f"未知策略 {strategy}, 可选: {list(_WF_STRATEGIES.keys())}"}
    try:
        count = max(int(payload.get("count") or 800), 100)
        train_window = max(int(payload.get("train") or 120), 20)
        test_window = max(int(payload.get("test") or 60), 5)
    except (TypeError, ValueError):
        return {"ok": False, "message": "count/train/test 必须为整数"}
    if count < train_window + test_window:
        return {"ok": False,
                "message": f"count={count} 不足, 至少需要 {train_window + test_window}"}

    param_grid, pg_err = _parse_wf_param_grid(strategy, payload.get("param_grid"))
    if pg_err:
        return {"ok": False, "message": f"param_grid 错误: {pg_err}"}

    # 加载日 K (优先 MySQL, fallback xtdata)
    try:
        from lib.backtest_data import load_daily_kline, get_stock_name
        df_full = load_daily_kline(code)
    except Exception as e:
        return {"ok": False, "message": f"加载 {code} K 线失败: {type(e).__name__}: {e}"}
    if df_full is None or len(df_full) == 0:
        return {"ok": False, "message": f"加载 {code} K 线为空"}
    df = df_full.tail(count).copy()
    if len(df) < train_window + test_window:
        return {"ok": False,
                "message": (f"实际拿到 {len(df)} 行 < 训练 {train_window} + 评估 {test_window}, "
                            f"加大 count 或 减小 train/test")}

    # 解析策略函数
    try:
        import parameter_tuning.walk_forward as _wf_mod
        from parameter_tuning.walk_forward import walk_forward_analysis
        strategy_fn = getattr(_wf_mod, _WF_STRATEGIES[strategy]["fn_name"])
    except Exception as e:
        return {"ok": False, "message": f"无法 import walk_forward 模块/策略: {e}"}

    oos_warmup = _wf_indicator_warmup(strategy, param_grid)

    t0 = _time.time()
    try:
        report = walk_forward_analysis(
            df, strategy_fn, param_grid,
            train_window=train_window, test_window=test_window,
            oos_warmup=oos_warmup,
        )
    except Exception as e:
        return {"ok": False, "message": f"walk-forward 计算失败: {type(e).__name__}: {e}"}
    elapsed = round(_time.time() - t0, 2)

    # 汇总判断: 先看有没有正向信号, 再看过拟合. IS<=0 不再标成鲁棒
    overfit = float(report.overfit_score)
    avg_is = float(report.avg_train_sharpe)
    avg_oos = float(report.avg_test_sharpe)
    signal = report.signal
    if signal == "no_signal":
        verdict = "none"
        verdict_label = "无正向信号"
        verdict_text = ("训练期和评估期都没有正收益, 策略在这个标的上没找到可交易规律. "
                        "这是 Walk-Forward 在说「别做」, 不是过拟合, 也不是工具失效.")
    elif signal == "no_is":
        verdict = "warn"
        verdict_label = "训练期无信号"
        verdict_text = ("训练期 Sharpe <= 0, 评估期却为正 -- 更像运气, 不要当成鲁棒. "
                        "换标的或换策略再跑一组对照.")
    elif avg_oos <= 0 and overfit >= 0.3:
        verdict = "danger"
        verdict_label = "严重过拟合"
        verdict_text = "训练好看、评估亏钱. 不应直接实盘. 建议换策略风格 / 扩大样本 / 缩减参数空间"
    elif overfit < 0.3 and avg_oos > 0:
        verdict = "ok"
        verdict_label = "相对鲁棒"
        verdict_text = ("IS / OOS 接近且评估期仍为正, 这组策略+标的相对鲁棒. "
                        "仍需 paper trading, 并在更多标的上复核.")
    elif overfit < 0.7:
        verdict = "warn"
        verdict_label = "中度过拟合"
        verdict_text = ("中度过拟合, 建议: ① 减小参数空间; "
                        "② 加入交易成本; ③ 降低仓位; ④ 扩大样本 (多标的 / 更长历史)")
    else:
        verdict = "danger"
        verdict_label = "严重过拟合"
        verdict_text = "严重过拟合, 不应直接实盘. 建议换策略风格 / 扩大样本 / 缩减参数空间"

    windows = []
    for w in report.windows:
        windows.append({
            "window_id":    int(w.window_id),
            "train":        f"{w.train_start} ~ {w.train_end}",
            "test":         f"{w.test_start} ~ {w.test_end}",
            "best_params":  w.best_params,
            "train_sharpe": round(float(w.train_sharpe), 3),
            "test_sharpe":  round(float(w.test_sharpe), 3),
            "train_ret":    round(float(w.train_ret), 4),
            "test_ret":     round(float(w.test_ret), 4),
            "train_trades": int(w.train_trades),
            "test_trades":  int(w.test_trades),
        })

    try:
        name = get_stock_name(code)
    except Exception:
        name = code

    strategy_meta = _WF_STRATEGIES[strategy]
    return {
        "ok":      True,
        "code":    code,
        "name":    name,
        "params": {
            "count":            count,
            "train_window":     train_window,
            "test_window":      test_window,
            "data_rows":        len(df),
            "first_date":       str(df.index[0])[:10],
            "last_date":        str(df.index[-1])[:10],
            "elapsed_sec":      elapsed,
            "strategy":         strategy,
            "strategy_label":   strategy_meta["label"],
            "strategy_summary": f"{strategy_meta['label']} ({len(param_grid)} 组候选)",
            "param_cols":       list(strategy_meta["param_cols"]),
            "param_grid":       param_grid,
            "warmup":           oos_warmup,
        },
        "summary": {
            "windows":           len(report.windows),
            "avg_train_sharpe":  round(avg_is, 3),
            "avg_test_sharpe":   round(avg_oos, 3),
            "is_oos_ratio":      round(float(report.is_oos_ratio), 3),
            "overfit_score":     round(overfit, 3),
            "signal":            signal,
            "train_trades":      int(sum(w.train_trades for w in report.windows)),
            "test_trades":       int(sum(w.test_trades for w in report.windows)),
        },
        "windows":       windows,
        "verdict":       verdict,
        "verdict_label": verdict_label,
        "verdict_text":  verdict_text,
    }


# ------------------------------------------------------------
# 策略生命周期
# ------------------------------------------------------------

@router.get("/registry")
def get_registry():
    if not OUTPUTS_EVOLVE_REGISTRY.exists():
        return []
    try:
        data = json.loads(OUTPUTS_EVOLVE_REGISTRY.read_text(encoding="utf-8"))
    except Exception:
        return []
    rows = []
    for name, s in data.get("strategies", {}).items():
        kpi = s.get("kpi", {})
        rows.append({
            "name":         name,
            "description":  s.get("description", ""),
            "stage":        s.get("stage", "?"),
            "capital_pct":  s.get("capital_pct", 0),
            "sharpe":       kpi.get("rolling_30d_sharpe", 0),
            "return":       kpi.get("rolling_30d_return", 0),
            "maxdd":        kpi.get("rolling_30d_maxdd", 0),
        })
    return rows


@router.post("/lifecycle_eval")
def lifecycle_eval(payload: dict = Body({})):
    try:
        from strategy_lifecycle.registry import StrategyRegistry
        reg = StrategyRegistry(str(OUTPUTS_EVOLVE_REGISTRY), total_capital=1_000_000)
        migrations = reg.evaluate_and_migrate()
        if not migrations:
            return {
                "summary": "本轮无迁移 (KPI 未触发阶段变更)",
                "log": "registry.json 里的示例策略 KPI 是预设稳定态 (production 仍达标 / paper "
                       "的 days_since_promotion=0 不到升级条件), 所以重复跑都不会迁移. "
                       "想看真实迁移效果, 用上方「用真实数据评估」-- 那才是接 /live/sim 模拟盘的真实 P&L.",
            }
        lines = [f"本轮触发 {len(migrations)} 个迁移:"]
        for m in migrations:
            lines.append(f"  [{m['name']}] {m['from']} -> {m['to']}")
            lines.append(f"      理由: {m['reason']}")
        return {"summary": f"触发 {len(migrations)} 个迁移", "log": "\n".join(lines)}
    except Exception as e:
        return {"summary": f"[ERROR] {e}", "log": ""}


# ------------------------------------------------------------
# 策略生命周期 -- 真实数据评估 (从 /live/sim 持仓 P&L 算 KPI)
# ------------------------------------------------------------

@router.post("/real_pnl_snapshot")
@router.post("/real_pnl_snapshot/")
def real_pnl_snapshot_endpoint(payload: dict = Body({})):
    """手动触发: 拉一次实盘 total_asset, 写入 outputs/real_pnl_history.json
    body: {"force": bool, 默认 false (当日已有则跳过)}
    """
    force = bool(payload.get("force", False))
    return _record_real_pnl_snapshot(force=force)


@router.get("/real_pnl_history")
def real_pnl_history_get():
    """返回当前 real_pnl_history.json 的全部内容 (供前端展示)"""
    return _load_real_pnl_history()


def _lifecycle_gate(key: str, label: str, value: float, threshold: float,
                    op: str, kind: str, relevant: bool = True) -> dict:
    """一条门槛: 过 / 未过 / 本档不看。"""
    if op == "gt":
        passed = value > threshold
    elif op == "gte":
        passed = value >= threshold
    elif op == "lt":
        passed = value < threshold
    else:
        passed = True

    if kind == "pct":
        value_text = f"{value:.2%}" if key == "maxdd" else f"{value:+.2%}"
        if op == "lt":
            threshold_text = f"< {threshold:.0%}"
        elif op == "gt" and threshold == 0:
            threshold_text = "> 0%"
        else:
            threshold_text = f"> {threshold:.0%}"
    elif kind == "days":
        value_text = str(int(value))
        threshold_text = f">= {int(threshold)}"
    else:
        value_text = f"{value:.2f}"
        threshold_text = f"> {threshold:g}" if op == "gt" else f">= {threshold:g}"

    hint = ""
    if relevant and not passed:
        if key == "days":
            hint = f"还差 {int(threshold) - int(value)} 天"
        elif key == "return":
            hint = "收益还没转正"
        elif key == "sharpe":
            hint = f"Sharpe {value:.2f} 还不到 {threshold:g}"
        elif key == "maxdd":
            hint = f"回撤 {value:.1%} 已超过 {threshold:.0%}"

    return {
        "key":            key,
        "label":          label,
        "value":          value,
        "value_text":     value_text,
        "threshold":      threshold,
        "threshold_text": threshold_text,
        "passed":         passed,
        "relevant":       relevant,
        "hint":           hint,
    }


def _lifecycle_health_report(source_kind: str, as_production: bool,
                             trading_days: int, interval_return: float,
                             sharpe: float, maxdd: float) -> dict:
    """整户资金曲线体检: 阶段由数据源决定, 结论只有观察 / 加仓 / 停。"""
    force_stop = maxdd > 0.20

    if source_kind == "sim":
        current_id = "paper"
        current_label = "模拟盘"
        gates = [
            _lifecycle_gate("return", "区间累计收益", interval_return, 0.0, "gt", "pct", True),
            _lifecycle_gate("sharpe", "Sharpe (年化)", sharpe, 0.5, "gt", "num", False),
            _lifecycle_gate("maxdd", "最大回撤", maxdd, 0.05, "lt", "pct", True),
            _lifecycle_gate("days", "交易日数", float(trading_days), 20, "gte", "days", True),
        ]
        promote_ok = (trading_days >= 20 and interval_return > 0 and maxdd < 0.05)
        hold_capital = 0.0
        promote_capital = 0.05
        next_id, next_label = "probation", "小仓试用"
        promote_title = "可以上实盘"
        promote_text = (f"模拟盘 {trading_days} 天、收益 {interval_return:+.2%}、"
                        f"回撤 {maxdd:.2%}, 过线了。建议先拿约 5% 资金小仓试用, 不要一次加满。")
        hold_title = "继续观察"
        hold_lead = "还不能上实盘。"
    elif as_production:
        current_id = "production"
        current_label = "主力"
        sharpe_weak = sharpe < 0.5
        gates = [
            _lifecycle_gate("return", "区间累计收益", interval_return, 0.0, "gt", "pct", False),
            _lifecycle_gate("sharpe", "Sharpe (年化)", sharpe, 0.5, "gt", "num", True),
            _lifecycle_gate("maxdd", "最大回撤", maxdd, 0.20, "lt", "pct", True),
            _lifecycle_gate("days", "交易日数", float(trading_days), 14, "gte", "days", sharpe_weak),
        ]
        promote_ok = False
        hold_capital = 0.30
        promote_capital = 0.30
        next_id, next_label = "retired", "停用"
        promote_title = "维持主力"
        promote_text = ""
        hold_title = "继续观察"
        hold_lead = ("Sharpe 偏低, 但天数还不够, 先再看几天。"
                     if sharpe_weak else "主力这一档还站得住, 先维持约 30%。")
    else:
        current_id = "probation"
        current_label = "小仓试用"
        gates = [
            _lifecycle_gate("return", "区间累计收益", interval_return, 0.0, "gt", "pct", False),
            _lifecycle_gate("sharpe", "Sharpe (年化)", sharpe, 0.5, "gt", "num", True),
            _lifecycle_gate("maxdd", "最大回撤", maxdd, 0.20, "lt", "pct", True),
            _lifecycle_gate("days", "交易日数", float(trading_days), 20, "gte", "days", True),
        ]
        promote_ok = (trading_days >= 20 and sharpe > 0.5 and not force_stop)
        hold_capital = 0.05
        promote_capital = 0.30
        next_id, next_label = "production", "主力"
        promote_title = "可以加仓"
        promote_text = (f"小仓试用 {trading_days} 天、Sharpe {sharpe:.2f}, 过线了。"
                        f"建议加到约 30%。")
        hold_title = "继续观察"
        hold_lead = "先维持约 5% 小仓。"

    blockers = [g["hint"] for g in gates if g["relevant"] and not g["passed"] and g["hint"]]

    if force_stop or (source_kind == "real" and as_production and sharpe < 0.5 and trading_days >= 14):
        verdict = "stop"
        verdict_title = "建议停用"
        if force_stop:
            verdict_text = f"最大回撤 {maxdd:.1%} 超过 20%, 先停, 别加仓。"
        else:
            verdict_text = (f"Sharpe {sharpe:.2f} 已连续 {trading_days} 天低于 0.5, "
                            f"主力这一档该停了。")
        suggested_capital_pct = 0.0
        suggested_next_id = "retired"
        suggested_next_label = "停用"
    elif promote_ok and not as_production:
        verdict = "promote"
        verdict_title = promote_title
        verdict_text = promote_text
        suggested_capital_pct = promote_capital
        suggested_next_id = next_id
        suggested_next_label = next_label
    elif as_production and not force_stop and not (sharpe < 0.5 and trading_days >= 14):
        verdict = "keep"
        verdict_title = "维持主力"
        verdict_text = (f"Sharpe {sharpe:.2f}、回撤 {maxdd:.2%}, 这一档还站得住。"
                        f"建议仓位维持约 30%。")
        if sharpe < 0.5:
            verdict = "observe"
            verdict_title = hold_title
            leftover = 14 - trading_days
            verdict_text = (f"Sharpe {sharpe:.2f} 偏低, 但只看了 {trading_days} 天。"
                            f"再观察 {leftover} 天, 仍低于 0.5 再停。建议仓位先维持约 30%。")
        suggested_capital_pct = hold_capital
        suggested_next_id = current_id
        suggested_next_label = current_label
    else:
        verdict = "observe"
        verdict_title = hold_title
        extra = ("差: " + "; ".join(blockers) + "。") if blockers else ""
        verdict_text = (hold_lead + extra).strip()
        suggested_capital_pct = hold_capital
        suggested_next_id = current_id
        suggested_next_label = current_label

    pipeline = [
        {"id": "research",   "label": "研发",     "hint": "过拟合检测"},
        {"id": "paper",      "label": "模拟盘",   "hint": "零资金验证"},
        {"id": "probation",  "label": "小仓试用", "hint": "建议约 5%"},
        {"id": "production", "label": "主力",     "hint": "建议约 30%"},
        {"id": "retired",    "label": "停用",     "hint": "清仓"},
    ]
    for step in pipeline:
        sid = step["id"]
        if verdict == "stop" and sid == "retired":
            step["state"] = "stop"
        elif sid == current_id:
            step["state"] = "current"
        elif verdict == "promote" and sid == suggested_next_id:
            step["state"] = "next"
        elif sid == "research":
            step["state"] = "past"
        else:
            order = ["research", "paper", "probation", "production", "retired"]
            step["state"] = "past" if order.index(sid) < order.index(current_id) else "idle"

    if suggested_capital_pct <= 0:
        capital_text = "建议仓位 0%"
    else:
        capital_text = f"建议仓位约 {suggested_capital_pct:.0%}"

    return {
        "current_id":            current_id,
        "current_label":         current_label,
        "verdict":               verdict,
        "verdict_title":         verdict_title,
        "verdict_text":          verdict_text,
        "suggested_capital_pct": suggested_capital_pct,
        "suggested_capital_text": capital_text,
        "suggested_next_id":     suggested_next_id,
        "suggested_next_label":  suggested_next_label,
        "gates":                 gates,
        "blockers":              blockers,
        "pipeline":              pipeline,
    }


def _kpi_from_daily_cum(cum: List[float], first_ts: str, last_ts: str,
                         raw_points: int) -> dict:
    """累计收益率序列 -> 区间收益 / Sharpe / 最大回撤 / 交易日数。"""
    import math
    n = len(cum)
    daily: List[float] = [cum[0]]
    for i in range(1, n):
        prev = 1 + cum[i - 1]
        daily.append(0.0 if prev <= 0 else (1 + cum[i]) / prev - 1.0)
    mean_d = sum(daily) / n
    var_d = sum((x - mean_d) ** 2 for x in daily) / max(n - 1, 1)
    std_d = math.sqrt(var_d) if var_d > 0 else 0.0
    sharpe = (mean_d / std_d * math.sqrt(252)) if std_d > 0 else 0.0
    nav = [1 + c for c in cum]
    peak = nav[0]
    maxdd = 0.0
    for v in nav:
        if v > peak:
            peak = v
        dd = (peak - v) / peak if peak > 0 else 0.0
        if dd > maxdd:
            maxdd = dd
    return {
        "interval_return":    round(cum[-1], 4),
        "sharpe":             round(sharpe, 4),
        "max_drawdown":       round(maxdd, 4),
        "trading_days":       n,
        "data_points":        n,
        "first_ts":           first_ts,
        "last_ts":            last_ts,
        "raw_history_points": raw_points,
    }


def _strategy_label(name: str) -> str:
    from lib.strategy_registry import get_strategy
    meta = get_strategy(name)
    return meta.label if meta else name


def _sim_strategy_books(state: dict) -> List[dict]:
    """按「策略 + 股票」回放日净值, 再套同一套体检门槛。

    本系统是一票绑一套策略, 茅台上的 MACD 和纳指上的 MACD 不是同一条书.
    分母是这对本金 (峰值占用资金), 不是整户 100 万.
    成交直接走模拟盘同一套历史回放, 不读 live_state.orders
    (主循环每轮整文件覆盖, 回放成交会被冲掉).
    """
    import os
    from collections import defaultdict
    from datetime import date
    from lib.backtest_data import load_daily_kline
    from lib.live_simulator import (
        replay_sim_fills, load_mock_config, load_strategy_config, merge_watch_codes,
    )
    from lib.strategy_registry import StrategyRouter

    watch = [str(c) for c in (state.get("watch_stocks") or []) if c]
    if not watch:
        watch = merge_watch_codes([])
    if not watch:
        raise ValueError("模拟盘监控池为空, 无法按策略+股票体检")

    start = os.environ.get("SIM_HISTORY_START_DATE", "2026-04-01")
    end = date.today().strftime("%Y-%m-%d")
    strat_cfg = load_strategy_config()
    router = StrategyRouter(
        per_stock=strat_cfg.get("per_stock", {}) or {},
        default=strat_cfg.get("default", "macd_1d"),
    )
    book = load_mock_config()
    _, raw_orders, _ = replay_sim_fills(
        watch, router, start, end,
        book.get("positions") or [],
        float(book.get("capital") or state.get("capital") or 1_000_000),
    )
    orders = [
        o for o in raw_orders
        if o.get("status") in ("submitted", "dry_run")
        and o.get("strategy") and o.get("code")
        and int(o.get("quantity") or 0) > 0
        and float(o.get("price") or 0) > 0
    ]
    if not orders:
        raise ValueError("历史回放没有成交, 无法按策略+股票体检")

    name_map = {
        str(p.get("code")): (p.get("name") or "")
        for p in (state.get("positions") or [])
        if p.get("code")
    }

    by_pair: Dict[tuple, List[dict]] = defaultdict(list)
    for o in orders:
        key = (str(o["strategy"]).strip(), str(o["code"]).strip())
        by_pair[key].append(o)

    codes = sorted({code for _, code in by_pair})
    start = min(str(o.get("ts") or "")[:10] for o in orders)
    end = max(str(o.get("ts") or "")[:10] for o in orders)
    for p in state.get("pnl_history") or []:
        d = str(p.get("ts") or "")[:10]
        if d:
            end = max(end, d)

    closes: Dict[str, Dict[str, float]] = {}
    for code in codes:
        df = load_daily_kline(code, start_date=start, end_date=end)
        series: Dict[str, float] = {}
        for idx, row in df.iterrows():
            series[str(idx)[:10]] = float(row["close"])
        closes[code] = series

    books = []
    for (name, code), fills in sorted(by_pair.items()):
        dates = [d for d in sorted(closes[code]) if start <= d <= end]
        if len(dates) < 2:
            raise ValueError(f"{name} {code} 日K不足 2 天, 无法体检")
        fills_by_day: Dict[str, List[dict]] = defaultdict(list)
        for o in fills:
            fills_by_day[str(o.get("ts") or "")[:10]].append(o)
        qty = 0
        cash = 0.0
        last_px = None
        peak_cap = 0.0
        pnl_series: List[float] = []
        for d in dates:
            for o in fills_by_day.get(d, []):
                q = int(o["quantity"])
                px = float(o["price"])
                if o.get("side") == "buy":
                    qty += q
                    cash -= q * px
                elif o.get("side") == "sell":
                    qty -= q
                    cash += q * px
            px = closes[code].get(d, last_px)
            if px is None:
                continue
            last_px = px
            mtm = qty * px
            peak_cap = max(peak_cap, -cash, mtm)
            pnl_series.append(cash + mtm)
        if peak_cap <= 0:
            raise ValueError(f"{name} {code} 峰值资金为 0, 无法算收益率")
        cum = [p / peak_cap for p in pnl_series]
        kpi = _kpi_from_daily_cum(cum, dates[0], dates[-1], len(dates))
        report = _lifecycle_health_report(
            "sim", False, kpi["trading_days"],
            kpi["interval_return"], kpi["sharpe"], kpi["max_drawdown"],
        )
        if report["verdict"] == "stop":
            stage_id = "retired"
        elif report["verdict"] == "promote":
            stage_id = report["suggested_next_id"]
        else:
            stage_id = report["current_id"]
        books.append({
            "name":        name,
            "label":       _strategy_label(name),
            "code":        code,
            "stock_name":  name_map.get(code, ""),
            "orders":      len(fills),
            "holding":     qty > 0,
            "peak_capital": round(peak_cap, 2),
            "pnl":         round(pnl_series[-1], 2),
            "kpi":         kpi,
            "stage_id":    stage_id,
            **report,
        })
    return books


def _lifecycle_overview_pipeline(books: List[dict]) -> List[dict]:
    """总览图: 五个阶段, 每格挂上走到这里的策略+股票."""
    steps = [
        {"id": "research",   "label": "研发",     "hint": "过拟合检测"},
        {"id": "paper",      "label": "模拟盘",   "hint": "继续观察"},
        {"id": "probation",  "label": "小仓试用", "hint": "建议约 5%"},
        {"id": "production", "label": "主力",     "hint": "建议约 30%"},
        {"id": "retired",    "label": "停用",     "hint": "清仓"},
    ]
    by: Dict[str, List[dict]] = {s["id"]: [] for s in steps}
    for b in books:
        sid = b.get("stage_id") or "paper"
        by.setdefault(sid, []).append({
            "name":       b["name"],
            "label":      b["label"],
            "code":       b["code"],
            "stock_name": b.get("stock_name") or b["code"],
        })
    out = []
    for s in steps:
        items = by.get(s["id"]) or []
        if s["id"] == "retired" and items:
            state = "stop"
        elif s["id"] == "probation" and items:
            state = "next"
        elif items:
            state = "current"
        else:
            state = "idle"
        out.append({**s, "items": items, "state": state})
    return out


@router.post("/lifecycle_sim_eval")
@router.post("/lifecycle_sim_eval/")
def lifecycle_sim_eval(payload: dict = Body({})):
    """按「策略 + 股票」做上线体检. 不评整户净值.

    body: {"source": "sim"}  只接受模拟盘
    """
    from datetime import datetime as _dt

    source = str(payload.get("source") or "sim").strip().lower()
    if source == "real":
        return {"ok": False, "message": "这页只评模拟盘的策略+股票, 实盘没有按策略拆开的成交流水"}
    if source != "sim":
        return {"ok": False, "message": f"非法 source={source}, 必须是 sim"}

    if not OUTPUTS_LIVE_STATE.exists():
        return {"ok": False, "message": "live_state.json 不存在, 模拟盘可能未启动"}
    try:
        state = json.loads(OUTPUTS_LIVE_STATE.read_text(encoding="utf-8"))
    except Exception as e:
        return {"ok": False, "message": f"读 live_state.json 失败: {e}"}

    try:
        strategies = _sim_strategy_books(state)
    except Exception as e:
        return {"ok": False, "message": str(e)}

    return {
        "ok":          True,
        "source":      "sim_strategy_stock",
        "source_kind": "sim",
        "asof":        _dt.now().isoformat(timespec="seconds"),
        "pipeline":    _lifecycle_overview_pipeline(strategies),
        "strategies":  strategies,
    }


