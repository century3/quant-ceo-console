# -*- coding: utf-8 -*-
# Author：@跟陈博士学AI
# 模拟盘 / 实盘 runner -- 后台线程跑 LiveTradingLoop
"""
设计:
    - 启动: 后台 daemon 线程跑 LiveTradingLoop.run_once() 循环 (默认每 60 秒)
    - 持仓: 从 config/mock_positions.yaml 读取
    - 模式: dry_run (默认, 模拟下单) / 实盘 (连真实 QMT, 慎用!)
    - 策略: 从 config/strategies.yaml 读路由表, 注入 StrategyRouter 到 loop
    - 行情: qmt/miniqmt 走盘中；tdx 用 MySQL 日线收盘价

数据写到 outputs/live_state.json, dashboard 5 秒轮询自动刷新
"""

from __future__ import annotations
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple


CONFIG_FILE = Path(__file__).resolve().parent.parent / "config" / "mock_positions.yaml"
STRATEGY_CONFIG_FILE = Path(__file__).resolve().parent.parent / "config" / "strategies.yaml"
WATCH_POOL_FILE = Path(__file__).resolve().parent.parent / "config" / "watch_pool.yaml"


# ============================================================
# 策略路由配置 -- 读 / 写 strategies.yaml
# ============================================================

def _normalize_dragon_version(version) -> str:
    v = str(version or "").strip().lower()
    if v not in ("v1", "v2"):
        raise ValueError(f"龙头版本只能是 v1 或 v2, 收到: {version!r}")
    return v


def load_strategy_config() -> dict:
    """读 config/strategies.yaml -- 返回 {default, per_stock, dragon_version}"""
    if not STRATEGY_CONFIG_FILE.exists():
        return {"default": "macd_1d", "per_stock": {}, "dragon_version": "v2"}
    try:
        import yaml
        cfg = yaml.safe_load(STRATEGY_CONFIG_FILE.read_text(encoding="utf-8")) or {}
        return {
            "default":         cfg.get("default", "macd_1d"),
            "per_stock":       cfg.get("per_stock", {}) or {},
            "dragon_version":  cfg.get("dragon_version") or "v2",
        }
    except Exception as e:
        print(f"[WARN] 读 strategies.yaml 失败: {e}")
        return {"default": "macd_1d", "per_stock": {}, "dragon_version": "v2"}


def save_strategy_config(default: str, per_stock: dict, dragon_version: str = "v2") -> None:
    """写 config/strategies.yaml -- 前端 '应用策略配置' / 龙头版本下拉 调"""
    import yaml
    dragon_version = _normalize_dragon_version(dragon_version)
    try:
        STRATEGY_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        header = (
            "# 实盘监控 -- 策略路由表 (由 /live 页面写入)\n"
            "# 修改后, 在页面点 '应用策略配置' 即可热加载, 无需重启\n"
            "# dragon_version: v1 对照 / v2 默认, 主循环 strat_dragon 入场跟这个走\n"
            "# MACD: macd_1d=日K(12/26/9); 另有 ma20_hold / multi_factor_top / dragon_picker / grid_classic / chan_buy1 / chan_buy2 / chan_buy3\n\n"
        )
        body = yaml.safe_dump(
            {
                "default": default,
                "per_stock": dict(per_stock or {}),
                "dragon_version": dragon_version,
            },
            allow_unicode=True, sort_keys=False, default_flow_style=False,
        )
        STRATEGY_CONFIG_FILE.write_text(header + body, encoding="utf-8")
    except OSError as e:
        raise RuntimeError(f"无法写入 {STRATEGY_CONFIG_FILE}: {e}") from e
    except Exception as e:
        raise RuntimeError(f"写入 strategies.yaml 失败: {e}") from e


def set_dragon_version(version: str) -> str:
    """只改主循环龙头入场版本, 不动 per_stock"""
    version = _normalize_dragon_version(version)
    cfg = load_strategy_config()
    save_strategy_config(cfg["default"], cfg.get("per_stock") or {}, version)
    return version


# ============================================================
# 自选监控池 watch_pool.yaml -- 无持仓也会拉行情、算信号、可触发买入
# ============================================================

def load_watch_pool() -> dict:
    """读 config/watch_pool.yaml -- 返回 {codes: [str,...]}"""
    if not WATCH_POOL_FILE.exists():
        return {"codes": []}
    try:
        import yaml
        data = yaml.safe_load(WATCH_POOL_FILE.read_text(encoding="utf-8")) or {}
        codes = data.get("codes", []) or []
        return {"codes": [str(c).strip() for c in codes if str(c).strip()]}
    except Exception as e:
        print(f"[WARN] 读 watch_pool.yaml 失败: {e}")
        return {"codes": []}


def save_watch_pool(codes: List[str]) -> None:
    """写 config/watch_pool.yaml"""
    import yaml
    try:
        WATCH_POOL_FILE.parent.mkdir(parents=True, exist_ok=True)
        header = (
            "# 监控代码列表 -- 与 mock 持仓、per_stock 合并为最终监控列表 (去重)\n"
            "# 也可通过页面「添加股票并绑定策略」写入\n\n"
        )
        body = yaml.safe_dump(
            {"codes": [c.strip() for c in (codes or []) if str(c).strip()]},
            allow_unicode=True, sort_keys=False, default_flow_style=False,
        )
        WATCH_POOL_FILE.write_text(header + body, encoding="utf-8")
    except OSError as e:
        raise RuntimeError(f"无法写入 {WATCH_POOL_FILE}: {e}") from e
    except Exception as e:
        raise RuntimeError(f"写入 watch_pool 失败: {e}") from e


def merge_watch_codes(ui_codes: Optional[List[str]] = None) -> List[str]:
    """
    合并监控代码 (去重, 顺序: 页面输入 -> 持仓 -> 自选池 -> 策略路由里的代码)

    ui_codes: 运行控制里「额外监控」逗号分隔解析后的列表, 可为空
    """
    ui_codes = ui_codes or []
    seen = set()
    out: List[str] = []

    def push(c: str) -> None:
        c = (c or "").strip()
        if not c or c in seen:
            return
        seen.add(c)
        out.append(c)

    for c in ui_codes:
        push(c)
    mock = load_mock_config()
    for p in mock.get("positions", []):
        push(str(p.get("code", "")))
    wp = load_watch_pool()
    for c in wp.get("codes", []):
        push(c)
    st = load_strategy_config()
    for c in (st.get("per_stock") or {}).keys():
        push(str(c).strip())
    return out


def _close_on_day(kline_rows: List[dict], day: str) -> float:
    """某日收盘价; 没有那天的 K 返回 0"""
    for b in kline_rows or []:
        if str(b.get("date") or "")[:10] == day:
            return float(b.get("close") or 0)
    return 0.0


def _close_on_or_after(kline_rows: List[dict], day: str) -> float:
    """起始日收盘价; 当天休市则取之后第一根"""
    px = _close_on_day(kline_rows, day)
    if px > 0:
        return px
    for b in kline_rows or []:
        if str(b.get("date") or "")[:10] >= day:
            return float(b.get("close") or 0)
    return 0.0


def _parse_start_date(raw: str) -> str:
    """校验起始日 YYYY-MM-DD, 不能晚于今天"""
    from datetime import date as date_cls
    text = str(raw or "").strip() or os.environ.get("SIM_HISTORY_START_DATE", "2026-04-01")
    day = datetime.strptime(text, "%Y-%m-%d").date()
    if day > date_cls.today():
        raise ValueError("起始日不能晚于今天")
    return day.strftime("%Y-%m-%d")


def _sim_watch_codes(extra: Optional[List[str]] = None) -> List[str]:
    """模拟盘监控列表: 合并池去掉实盘绑定"""
    src: Dict[str, str] = {}
    bind_file = CONFIG_FILE.parent / "binding_source.yaml"
    if bind_file.exists():
        import yaml
        data = yaml.safe_load(bind_file.read_text(encoding="utf-8")) or {}
        src = {str(k): str(v) for k, v in (data.get("sources") or {}).items()}
    seen = set()
    out: List[str] = []
    for c in list(extra or []) + merge_watch_codes():
        c = str(c or "").strip()
        if not c or c in seen or src.get(c) == "real":
            continue
        seen.add(c)
        out.append(c)
    return out


def collect_history_signals(watch_stocks: List[str], start_date: str,
                            end_date: str, router) -> Tuple[List[dict], Dict[str, List[dict]]]:
    """按各股当前策略回放 [start_date, end_date] 的 buy/sell"""
    from lib.backtest_engine import run_backtest
    all_sigs: List[dict] = []
    kline_map: Dict[str, List[dict]] = {}
    for code in watch_stocks:
        strat = (router.per_stock.get(code) if router else None) \
                or (router.default if router else "macd_1d")
        try:
            r = run_backtest(stock_code=code, strategy_name=strat,
                             start_date=start_date, end_date=end_date)
        except Exception as e:
            print(f"[WARN] 历史回放 {code}/{strat} 异常: {e}", flush=True)
            continue
        if not r or not r.get("ok"):
            continue
        kline_map[code] = r.get("kline") or []
        for sig in r.get("signals", []) or []:
            side = sig.get("side")
            if side not in ("buy", "sell"):
                continue
            d = sig.get("date")
            if not d:
                continue
            all_sigs.append({
                "ts":       str(d)[:10],
                "code":     code,
                "side":     side,
                "strategy": strat,
                "reason":   sig.get("reason", ""),
            })
    all_sigs.sort(key=lambda x: x["ts"])
    return all_sigs, kline_map


def build_opening_from_slots(slot_codes: List[str], kline_map: Dict[str, List[dict]],
                             start_date: str, capital: float, max_slots: int,
                             names: Dict[str, str]) -> List[dict]:
    """起始日按档位目标金额整手开仓; 额度不够的档留下空仓行"""
    from live_trading.live_loop import share_lot, size_buy_shares, slot_budget
    budget = slot_budget(capital, max_slots)
    cash = float(capital)
    out: List[dict] = []
    for code in slot_codes:
        price = _close_on_or_after(kline_map.get(code) or [], start_date)
        qty = size_buy_shares(price, budget, cash, lot=share_lot(code)) if price > 0 else 0
        notion = qty * price
        cash -= notion
        out.append({
            "code": code,
            "name": names.get(code) or code,
            "volume": qty,
            "cost": round(price, 4) if price > 0 else 0.0,
            "cur_price": round(price, 4) if price > 0 else 0.0,
            "market_value": round(notion, 2),
            "realized_pnl": 0.0,
            "buy_notional": round(notion, 2),
            "pnl": 0.0,
            "pnl_pct": 0.0,
        })
    return out


def buyhold_pnl_points(positions: List[dict], capital: float, start_date: str) -> List[dict]:
    """用开仓持仓做买持曲线, 从起始日到昨天"""
    from datetime import date as date_cls
    today_str = date_cls.today().strftime("%Y-%m-%d")
    held = [p for p in (positions or []) if int(p.get("volume") or 0) > 0]
    if not held:
        return []
    cost_used = sum(int(p.get("volume") or 0) * float(p.get("cost") or 0) for p in held)
    cash = float(capital) - cost_used
    try:
        from lib.backtest_data import load_daily_kline
        import pandas as pd
    except Exception as e:
        print(f"[WARN] 重置资金曲线: 依赖加载失败: {e}", flush=True)
        return []
    close_dfs = {}
    for p in held:
        code = p.get("code", "")
        try:
            df = load_daily_kline(code, start_date=start_date, end_date=today_str)
        except Exception as e:
            print(f"[WARN] 重置资金曲线: 拉 {code} 失败: {e}", flush=True)
            continue
        if df is None or df.empty:
            continue
        close_dfs[code] = df["close"]
    if not close_dfs:
        return []
    all_closes = pd.concat(close_dfs, axis=1).sort_index().ffill()
    points: List[dict] = []
    for ts, row in all_closes.iterrows():
        d_str = ts.strftime("%Y-%m-%d")
        if d_str >= today_str:
            continue
        mv = 0.0
        for p in held:
            code = p.get("code", "")
            vol = int(p.get("volume") or 0)
            px = row.get(code)
            if px is None or pd.isna(px):
                continue
            mv += vol * float(px)
        asset = cash + mv
        pnl = asset - capital
        pct = pnl / capital if capital > 0 else 0.0
        points.append({
            "ts": f"{d_str}T14:59:00",
            "pnl": round(pnl, 2),
            "pnl_pct": round(pct, 4),
        })
    return points


def opening_buy_orders(positions: List[dict], start_date: str, strategy_of) -> List[dict]:
    """把 mock 初始持仓记成 start_date 的买入, 后面的卖才有来源."""
    orders: List[dict] = []
    for p in positions or []:
        code = str(p.get("code") or "")
        qty = int(p.get("volume") or 0)
        price = float(p.get("cost") or 0)
        if not code or qty <= 0 or price <= 0:
            continue
        strat = strategy_of(code) if callable(strategy_of) else ""
        orders.append({
            "ts": start_date,
            "code": code,
            "side": "buy",
            "quantity": qty,
            "price": round(price, 4),
            "status": "dry_run",
            "strategy": strat or "",
            "reason": "初始建仓",
        })
    return orders


def replay_sim_fills(
    watch_stocks: List[str],
    router,
    start_date: str,
    end_date: str,
    start_positions: List[dict],
    capital: float,
) -> Tuple[List[dict], List[dict], List[dict]]:
    """按各股当前策略回放成交. 返回 (回放后持仓, 成交含初始建仓, 信号)."""
    all_sigs, kline_map = collect_history_signals(
        watch_stocks, start_date, end_date, router,
    )

    def _strat(code: str) -> str:
        if not router:
            return "macd_1d"
        return router.per_stock.get(code) or router.default or "macd_1d"

    opening = opening_buy_orders(
        load_mock_config().get("positions") or [], start_date, _strat,
    )
    if all_sigs:
        book = load_mock_config()
        positions, orders = apply_history_fills(
            start_positions, capital, all_sigs, kline_map,
            int(book["max_slots"]), list(book["slot_codes"]),
        )
    else:
        positions, orders = list(start_positions or []), []
    return positions, opening + orders, all_sigs


def apply_history_fills(
    positions: List[dict],
    capital: float,
    signals: List[dict],
    kline_map: Dict[str, List[dict]],
    max_slots: int,
    slot_codes: List[str],
) -> Tuple[List[dict], List[dict]]:
    """按时间顺序把历史 buy/sell 写进持仓.

    - sell: 有仓就卖光, 行留下 volume=0, 策略继续盯, 档还在
    - buy: 仅已分档且空仓时开, 金额 = 每档目标
    """
    from live_trading.live_loop import (
        apply_dry_run_fill, _held_volume, _available_cash,
        share_lot, size_buy_shares, slot_budget,
    )

    slotted = set(slot_codes or [])
    budget = slot_budget(capital, max_slots)
    positions = [dict(p) for p in (positions or [])]
    orders: List[dict] = []
    for sig in signals:
        code = str(sig.get("code") or "")
        side = sig.get("side")
        day = str(sig.get("ts") or "")[:10]
        if not code or side not in ("buy", "sell") or not day:
            continue
        price = _close_on_day(kline_map.get(code) or [], day)
        if price <= 0:
            continue
        held = _held_volume(positions, code)
        if side == "sell":
            if held <= 0:
                continue
            qty = held
            positions = apply_dry_run_fill(positions, code, "sell", qty, price)
        else:
            if held > 0 or code not in slotted:
                continue
            cash = _available_cash(positions, capital)
            qty = size_buy_shares(price, budget, cash, lot=share_lot(code))
            if qty <= 0:
                continue
            name = ""
            for p in positions:
                if p.get("code") == code:
                    name = str(p.get("name") or "")
                    break
            positions = apply_dry_run_fill(
                positions, code, "buy", qty, price, name=name,
            )
        orders.append({
            "ts": day,
            "code": code,
            "side": side,
            "quantity": qty,
            "price": round(price, 4),
            "status": "dry_run",
            "strategy": sig.get("strategy", ""),
            "reason": "历史回放成交",
        })
    return positions, orders


# ============================================================
# A 股交易时段判断 -- 9:30-11:30 + 13:00-15:00, 仅工作日
# 用于跳过非盘中循环 (避免在收盘后/周末仍触发信号)
# ============================================================

def is_a_share_trading_day(now: Optional[datetime] = None) -> bool:
    """A 股日历日: 周一到周五算交易日 (节假日未单独剔除)"""
    now = now or datetime.now()
    return now.weekday() < 5


def weekday_cn(now: Optional[datetime] = None) -> str:
    now = now or datetime.now()
    return ("周一", "周二", "周三", "周四", "周五", "周六", "周日")[now.weekday()]


def is_a_share_trading_hour(now: Optional[datetime] = None) -> bool:
    now = now or datetime.now()
    # 工作日 (周一到周五)
    if now.weekday() >= 5:
        return False
    t = now.time()
    from datetime import time as dt_time
    return ((dt_time(9, 30) <= t <= dt_time(11, 30))
            or (dt_time(13, 0) <= t <= dt_time(15, 0)))


def _nth_weekday(year: int, month: int, weekday: int, n: int):
    """某月的第 n 个 weekday。weekday: 周一=0 … 周日=6。"""
    from datetime import date
    first = date(year, month, 1)
    delta = (weekday - first.weekday()) % 7
    return date(year, month, 1 + delta + 7 * (n - 1))


def us_eastern_now(now: Optional[datetime] = None) -> datetime:
    """本机时间按北京时间理解，换算成美东当地时间（含夏令时）。"""
    from datetime import time as dt_time
    from datetime import timedelta, timezone
    cn = now or datetime.now()
    if cn.tzinfo is not None:
        cn = cn.astimezone(timezone(timedelta(hours=8))).replace(tzinfo=None)
    utc = cn - timedelta(hours=8)
    # 夏令时: 3 月第二个周日 02:00 EST 到 11 月第一个周日 02:00 EDT
    dst_start = datetime.combine(_nth_weekday(utc.year, 3, 6, 2), dt_time(7, 0))
    dst_end = datetime.combine(_nth_weekday(utc.year, 11, 6, 1), dt_time(6, 0))
    offset = -4 if dst_start <= utc < dst_end else -5
    return utc + timedelta(hours=offset)


def is_us_trading_hour(now: Optional[datetime] = None) -> bool:
    """美股常规交易时段: 美东工作日 9:30–16:00。"""
    from datetime import time as dt_time
    et = us_eastern_now(now)
    if et.weekday() >= 5:
        return False
    t = et.time()
    return dt_time(9, 30) <= t < dt_time(16, 0)


def market_is_open(code: str, now: Optional[datetime] = None) -> bool:
    if str(code).upper().endswith(".US"):
        return is_us_trading_hour(now)
    return is_a_share_trading_hour(now)


def load_mock_config() -> dict:
    """读 config/mock_positions.yaml -- 学员可改这个文件改持仓"""
    import yaml
    return yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8")) or {}


def save_slot_book(max_slots: int, slot_codes: List[str], capital: float,
                   positions: Optional[List[dict]] = None) -> None:
    """把本金 / 档位数 / 记名预留写回 mock_positions.yaml; 重置时可一并写入开仓持仓"""
    import yaml
    cfg = load_mock_config()
    cfg["capital"] = float(capital)
    cfg["max_slots"] = int(max_slots)
    cfg["slot_codes"] = list(slot_codes)
    if positions is not None:
        cfg["positions"] = [
            {
                "code": str(p.get("code") or ""),
                "name": str(p.get("name") or ""),
                "volume": int(p.get("volume") or 0),
                "cost": float(p.get("cost") or 0),
            }
            for p in positions
            if str(p.get("code") or "")
        ]
    body = yaml.safe_dump({
        "capital": cfg["capital"],
        "max_slots": cfg["max_slots"],
        "slot_codes": cfg["slot_codes"],
        "positions": cfg.get("positions") or [],
    }, allow_unicode=True, sort_keys=False)
    header = (
        "# 模拟盘资金与初始持仓\n"
        "# 启动 / 重置持仓时灌入; 本金和档位也可在页面「仓位管理」改\n"
        "# 最多 max_slots 档, 每档金额 = capital / max_slots\n"
        "# slot_codes 记名预留: 已分档才能买, 卖掉档还在; 未分档只监控\n\n"
    )
    CONFIG_FILE.write_text(header + body, encoding="utf-8")


def _latest_close_map(codes: List[str]) -> dict:
    """批量拉每只票的「最新一根日 K close」, 给 build_positions 当 cur_price.

    背景: 模拟盘 worker 在非交易时段会跳过 (不拉 tick), 所以持仓 cur_price 会卡
    在初始值; 之前直接拿 cost 当初始价 -> 表里浮盈/亏永远 0, 与「4-1 持有至今」
    的真实涨跌脱节. 现在启动 / 重置持仓时, 用 MySQL/xtdata 日 K 的最新 close
    刷新一次, 即便不在盘中, 用户也能看到真实的持有期浮盈.

    返回: {code: latest_close}; 拿不到的 code 不出现 -> 调用方回退到 cost
    """
    if not codes:
        return {}
    try:
        from lib.backtest_data import load_daily_kline
    except Exception as e:
        print(f"[WARN] _latest_close_map: import backtest_data 失败 -> {e}", flush=True)
        return {}
    out = {}
    # 拉近 30 天日 K 取最新一根 close, 区间宽一点容错节假日 / 周末
    from datetime import date, timedelta
    end_date = date.today().strftime("%Y-%m-%d")
    start_date = (date.today() - timedelta(days=45)).strftime("%Y-%m-%d")
    for code in codes:
        try:
            df = load_daily_kline(code, start_date=start_date, end_date=end_date)
        except Exception as e:
            print(f"[WARN] 拉 {code} 最新 close 失败: {e}", flush=True)
            continue
        if df is None or df.empty:
            continue
        out[code] = float(df.iloc[-1]["close"])
    return out


def build_positions_from_config() -> List[dict]:
    """从 config 构建标准化的 positions list (每只补齐当前价/市值/盈亏字段)

    cur_price 取自最新一根日 K close (容错: 拿不到回退到 cost), 这样:
      - 持仓表里的浮盈/亏 = (最新 close - cost) * volume, 反映真实持有期表现
      - calc_today_pnl 基于 position.pnl 求和 -> today_pnl 和持仓表对得上
      - 盘中真实 tick 来了, live_loop 会继续覆盖 cur_price (无副作用)
    """
    cfg = load_mock_config()
    raw_positions = cfg.get("positions", []) or []
    codes = [p.get("code", "") for p in raw_positions if p.get("code")]
    close_map = _latest_close_map(codes)

    positions = []
    for p in raw_positions:
        code = p.get("code", "")
        cost = float(p.get("cost", 0))
        volume = int(p.get("volume", 0))
        cur_price = float(close_map.get(code, cost))
        market_value = volume * cur_price
        pnl = (cur_price - cost) * volume
        pnl_pct = (cur_price / cost - 1) if cost > 0 else 0.0
        positions.append({
            "code":         code,
            "name":         p.get("name", ""),
            "volume":       volume,
            "cost":         cost,
            "cur_price":    round(cur_price, 4),
            "market_value": round(market_value, 2),
            "realized_pnl": 0.0,
            "buy_notional": round(volume * cost, 2),
            "pnl":          round(pnl, 2),
            "pnl_pct":      round(pnl_pct, 4),
        })
    return positions


class LiveSimRunner:
    """单例模拟盘/实盘控制器"""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._init()
        return cls._instance

    def _init(self):
        self._thread: Optional[threading.Thread] = None
        self._stop_flag = False
        self._loop = None
        self._cycle_count = 0
        self._last_cycle_at: Optional[str] = None
        self._last_error: Optional[str] = None
        self._dry_run = True
        self._router = None        # StrategyRouter -- 启动后才创建
        self._last_watch_stocks: Optional[List[str]] = None  # 最近一次启动时的合并监控列表

    # ------------------------------------------------------------------
    def status(self) -> dict:
        running = self._thread is not None and self._thread.is_alive()
        return {
            "running":        running,
            "cycle_count":    self._cycle_count,
            "last_cycle_at":  self._last_cycle_at,
            "last_error":     self._last_error,
            "dry_run":        self._dry_run,
            "watch_stocks":   list(self._last_watch_stocks or []),
        }

    def apply_capital(self, capital: float) -> None:
        """热更新本金, 下一笔开仓按新额度; 已有持仓不动"""
        if self._loop is not None:
            self._loop.capital = float(capital)

    # ------------------------------------------------------------------
    def start(self, watch_stocks: List[str], cycle_seconds: int = 60,
              dry_run: bool = True, init_positions: bool = True) -> str:
        """启动后台循环

        Args:
            watch_stocks: 监控股票池
            cycle_seconds: 循环周期 (秒)
            dry_run: True=模拟下单, False=真实下单 (慎用!)
            init_positions: 是否用 config 初始化持仓
        """
        if self._thread is not None and self._thread.is_alive():
            return "[INFO] 模拟盘已在运行, 请先停止"

        # 回放信号前先补美股日K，否则金叉死叉停在库里的最后一根
        try:
            from lib.us_daily_sync import sync_watch_us_daily
            sync_watch_us_daily(watch_stocks)
        except Exception as e:
            print(f"[us] 启动补齐美股日K失败 (不影响模拟盘启动): {type(e).__name__}: {e}", flush=True)

        from lib.paths import OUTPUTS_LIVE_STATE, setup_sys_path
        setup_sys_path()
        from live_trading.live_loop import LiveTradingLoop
        from lib.strategy_registry import StrategyRouter

        cfg = load_mock_config()
        capital = float(cfg.get("capital", 1_000_000))

        # 创建策略路由器 (从 strategies.yaml 读路由表)
        strat_cfg = load_strategy_config()
        self._router = StrategyRouter(
            per_stock=strat_cfg.get("per_stock", {}),
            default=strat_cfg.get("default", "macd_1d"),
        )

        try:
            self._last_watch_stocks = list(watch_stocks)
            self._loop = LiveTradingLoop(
                watch_stocks=watch_stocks,
                capital=capital,
                state_file=str(OUTPUTS_LIVE_STATE),
                dry_run=dry_run,
                signal_evaluator=self._router,
                max_slots=int(cfg["max_slots"]),
                slot_codes=list(cfg["slot_codes"]),
            )
            self._dry_run = dry_run
        except Exception as e:
            return f"[ERROR] 创建 LiveTradingLoop 失败: {e}"

        # 初始化持仓 (从配置)
        s = self._loop.state_store.load()
        s["trading_status"] = "RUNNING"
        s.setdefault("control", {})
        s["control"]["max_slots"] = int(cfg["max_slots"])
        s["control"]["slot_codes"] = list(cfg["slot_codes"])
        if init_positions:
            s["positions"] = build_positions_from_config()
            # 换剧本: 清掉旧票曲线/信号, 后面 seed 从 SIM_HISTORY_START_DATE 重放
            s["signals"] = []
            s["events"] = []
            s["orders"] = []
            s["pnl_history"] = []
        # 持仓 cur_price 已用最新日 K close 刷新 -> 同步重算 today_pnl
        # (避免之前运行残留的 today_pnl 与 positions 表对不上)
        try:
            from live_trading.live_loop import calc_today_pnl
            today_pnl, today_pct = calc_today_pnl(s.get("positions", []), capital)
            s["today_pnl"] = today_pnl
            s["today_pnl_pct"] = today_pct
        except Exception as e:
            print(f"[WARN] 启动时重算 today_pnl 失败: {e}", flush=True)
        self._loop.state_store.save(s)

        # 历史信号回放: 信号表为空时, 把每只监控股票从 SIM_HISTORY_START_DATE
        # 至今的策略 buy/sell 信号回放进 state.signals, 用户首次打开就能看到历史
        # 失败不阻塞 start (回测引擎/MySQL/数据缺失都属于非致命)
        try:
            self._seed_historical_signals_if_empty(watch_stocks)
        except Exception as e:
            print(f"[WARN] 历史信号回放失败 (不影响主流程): {e}", flush=True)

        # 历史资金曲线回放: 资金曲线没"今天之前的点"时, 把 [start_date, 昨天] 每个交易日
        # 按 mock_positions buy-and-hold 算的总资产% 写进 state.pnl_history,
        # 让"资金曲线"图启动就能看到 4-1 至今的趋势, 而不是只有今天那一段
        try:
            self._seed_historical_pnl_curve_if_empty()
        except Exception as e:
            print(f"[WARN] 历史资金曲线回放失败 (不影响主流程): {e}", flush=True)
        try:
            self._seed_today_intraday_curve()
        except Exception as e:
            print(f"[WARN] 当日分时回放失败 (不影响主流程): {e}", flush=True)

        # 启动 worker
        self._stop_flag = False
        self._cycle_count = 0
        self._last_error = None

        def worker():
            while not self._stop_flag:
                # A 股白天、美股美东白天各自轮询。两边都收盘才整轮跳过。
                active = [c for c in self._loop.watch_stocks if market_is_open(c)]
                if not active:
                    self._last_cycle_at = datetime.now().strftime("%H:%M:%S") + " (非盘中, 跳过)"
                else:
                    try:
                        self._loop.run_once(active)
                        self._cycle_count += 1
                        if all(str(c).upper().endswith(".US") for c in active):
                            session = "美股盘中"
                        elif any(str(c).upper().endswith(".US") for c in active):
                            session = "A股+美股盘中"
                        else:
                            session = "A股盘中"
                        self._last_cycle_at = datetime.now().strftime("%H:%M:%S") + f" ({session})"
                        self._last_error = None
                    except Exception as e:
                        self._last_error = f"{type(e).__name__}: {e}"
                # 每 1 秒检查 stop_flag
                for _ in range(cycle_seconds):
                    if self._stop_flag:
                        break
                    time.sleep(1)

        self._thread = threading.Thread(target=worker, daemon=True,
                                         name="LiveSimRunner")
        self._thread.start()

        mode_str = "模拟模式 (dry-run, 不连券商)" if dry_run else "实盘模式 (真实下单!)"
        # 路由表摘要 (前 5 条)
        route_preview = ", ".join(
            f"{c}->{n}" for c, n in list(self._router.per_stock.items())[:5]
        ) or "(空)"
        return (f"[OK] 已启动 -- {mode_str}\n"
                f"     合并后监控 {len(watch_stocks)} 只: {watch_stocks}, 周期={cycle_seconds}s, "
                f"初始持仓 {len(s.get('positions', []))} 只, 总资金 {capital:,.0f}\n"
                f"     默认策略={self._router.default}, 路由表预览: {route_preview}\n"
                f"     dashboard 每 5 秒自动刷新")

    # ------------------------------------------------------------------
    def stop(self) -> str:
        if self._thread is None:
            return "[INFO] 模拟盘未启动"
        self._stop_flag = True
        self._thread.join(timeout=5)
        msg = f"[OK] 已停止 -- 共跑了 {self._cycle_count} 轮"
        self._thread = None
        return msg

    # ------------------------------------------------------------------
    def trial_run(self) -> dict:
        """非交易时段也强制跑一次, 给用户看「现在各策略给的方向」.

        返回值:
            {
                "ok":        True/False,
                "message":   总结 (用户提示用)
                "summary":   {"buy": n, "sell": n, "hold": n, "total": n},
                "diagnoses": [{"code", "strategy", "side", "reason"}, ...]
            }

        实现说明:
            - 调 loop.run_once() 走完整流程 (含风控/下单), 信号写进 state.signals 表
            - 同时单独再跑一遍 router 收集每只股票的方向 (buy/sell/hold), 含 hold 也返回
              这样用户能直观看到「为什么没有信号 == 6 只全 hold」
            - 必须先 start() 过, 否则 self._loop 还没创建
        """
        if self._loop is None:
            return {
                "ok": False,
                "message": "[ERROR] 模拟盘未启动 (self._loop is None), 请先点「启动循环」",
                "summary": {}, "diagnoses": [],
            }

        # 先跑完整 loop (会写 signals/orders, 触发风控)
        try:
            self._loop.run_once()
            self._cycle_count += 1
            self._last_cycle_at = datetime.now().strftime("%H:%M:%S") + " (试算)"
            self._last_error = None
            loop_msg = f"[OK] 已试算 1 轮 (累计循环 {self._cycle_count})"
        except Exception as e:
            self._last_error = f"{type(e).__name__}: {e}"
            return {
                "ok": False,
                "message": f"[ERROR] 试算失败: {self._last_error}",
                "summary": {}, "diagnoses": [],
            }

        # 再跑一次 router (仅诊断, 不写盘) 用于把 hold 也告诉用户
        diagnoses: List[dict] = []
        summary = {"buy": 0, "sell": 0, "hold": 0, "error": 0, "total": 0}
        try:
            cfg = load_mock_config()
            capital = float(cfg.get("capital", 1_000_000))
            watch = list(self._last_watch_stocks or merge_watch_codes())
            for code in watch:
                try:
                    r = self._router(code, self._loop.market, capital) if self._router else \
                        {"side": "hold", "strategy": "?", "reason": "router 未初始化"}
                except Exception as e:
                    r = {"side": "error", "strategy": "?",
                         "reason": f"{type(e).__name__}: {e}"}
                side = r.get("side", "hold")
                summary[side] = summary.get(side, 0) + 1
                summary["total"] += 1
                diagnoses.append({
                    "code":     code,
                    "strategy": r.get("strategy", "?"),
                    "side":     side,
                    "reason":   r.get("reason", ""),
                })
        except Exception as e:
            # 诊断失败不影响主流程
            print(f"[WARN] trial_run diagnose error: {e}", flush=True)

        msg = (f"{loop_msg} -- 监控 {summary['total']} 只: "
               f"buy={summary['buy']}, sell={summary['sell']}, "
               f"hold={summary['hold']}"
               + (f", error={summary['error']}" if summary.get("error") else ""))
        return {
            "ok": True,
            "message": msg,
            "summary": summary,
            "diagnoses": diagnoses,
        }

    # ------------------------------------------------------------------
    def apply_strategy_config(self, default: str, per_stock: dict,
                             dragon_version: str = None, replay: bool = True) -> str:
        """写盘 + 热加载路由表; 默认按新策略从开仓日重放信号/成交"""
        if dragon_version is None:
            dragon_version = load_strategy_config().get("dragon_version") or "v2"
        save_strategy_config(default, per_stock, dragon_version)
        if self._router is not None:
            self._router.update(per_stock=per_stock, default=default)
            msg = (f"[OK] 路由表已热加载 -- 默认={default}, "
                   f"per_stock={len(per_stock or {})} 条, 龙头={dragon_version} (已写盘)")
        else:
            msg = (f"[OK] 路由表已写盘 -- 默认={default}, per_stock={len(per_stock or {})} 条, "
                   f"龙头={dragon_version} (模拟盘未启动, 下次启动生效)")
        if replay:
            msg = f"{msg}; {self.replay_book()}"
        return msg

    def replay_book(self, watch_stocks: Optional[List[str]] = None) -> str:
        """按当前路由从开仓日重放, 覆盖 signals / orders / positions.

        绑策略只改 yaml 不够: 模拟盘账本是启动时按当时策略回放的,
        不重放的话持仓表/信号表仍显示旧策略 (例如 macd_1d).
        """
        from datetime import date as date_cls
        router = self._ensure_router()
        watch = [str(c).strip() for c in (watch_stocks or []) if str(c).strip()]
        if not watch:
            watch = _sim_watch_codes(self._last_watch_stocks)
        if not watch:
            return "监控池为空, 未重放"
        try:
            s = self._read_live_state()
        except FileNotFoundError:
            return "live_state 尚未生成, 下次启动按新策略回放"

        start_date = str((s.get("control") or {}).get("history_start_date") or "").strip() \
            or os.environ.get("SIM_HISTORY_START_DATE", "2026-04-01")
        end_date = date_cls.today().strftime("%Y-%m-%d")
        capital = float(s.get("capital") or load_mock_config().get("capital") or 1_000_000)
        opening = build_positions_from_config()
        positions, orders, all_sigs = replay_sim_fills(
            watch, router, start_date, end_date, opening, capital,
        )
        s.setdefault("control", {})["history_start_date"] = start_date
        s["positions"] = positions
        s["signals"] = all_sigs[-100:]
        s["orders"] = orders[-500:]
        from live_trading.live_loop import calc_today_pnl
        today_pnl, today_pct = calc_today_pnl(positions, capital)
        s["today_pnl"] = today_pnl
        s["today_pnl_pct"] = today_pct
        self._write_live_state(s)
        filled = sum(1 for o in orders if o.get("status") == "dry_run")
        return f"已按新策略重放 {len(all_sigs)} 条信号 / {filled} 笔成交"

    # ------------------------------------------------------------------
    def _seed_historical_pnl_curve_if_empty(self) -> None:
        """启动时把 [SIM_HISTORY_START_DATE, 昨天] 的每日资金曲线点回放进 state.pnl_history.

        - 仅在 pnl_history 里没有"早于今天"的点时跑 (今天的盘中分时数据保留不动)
        - 用 mock_positions (vol/cost) + 每日 close 算每天的"总资产 vs 初始资金"百分比:
            asset_d = sum(vol_i * close_i[d]) + cash
            pnl_pct = (asset_d - capital) / capital
            cash = capital - sum(vol_i * cost_i)   (历史回放用 buy-and-hold; 盘中 dry-run 会真实改持仓)
        - ts 取当日 15:00:00 (与历史信号 ts 同格), 与今天的盘中分时点拼成完整曲线
        - 失败不影响启动
        """
        s = self._loop.state_store.load()
        hist = s.get("pnl_history") or []
        from datetime import date
        today_str = date.today().strftime("%Y-%m-%d")
        # 是否已有"今天之前"的点 -> 有就不再补 (避免重启重复 seed)
        has_pre_today = any((p.get("ts", "") < today_str) for p in hist)
        if has_pre_today:
            return

        cfg = load_mock_config()
        capital = float(cfg.get("capital", 1_000_000))
        positions_cfg = cfg.get("positions", []) or []
        if not positions_cfg:
            return

        # 现金 = 初始资金 - 持仓 cost 占用 (假定区间内无成交, sim 引擎非盘中没跑)
        cost_used = sum(int(p.get("volume", 0)) * float(p.get("cost", 0))
                        for p in positions_cfg)
        cash = capital - cost_used

        try:
            from lib.backtest_data import load_daily_kline
        except Exception as e:
            print(f"[WARN] pnl_history seed: import backtest_data 失败 -> {e}", flush=True)
            return

        start_date = os.environ.get("SIM_HISTORY_START_DATE", "2026-04-01")

        # 1) 拉每只票从 start_date 至今的日 K close
        # 2) 取所有日期的并集 (按交易日齐对齐), 没数据的日 forward fill
        import pandas as pd
        close_dfs = {}
        for p in positions_cfg:
            code = p.get("code", "")
            try:
                df = load_daily_kline(code, start_date=start_date, end_date=today_str)
            except Exception as e:
                print(f"[WARN] pnl_history seed: 拉 {code} 失败: {e}", flush=True)
                continue
            if df is None or df.empty:
                continue
            close_dfs[code] = df["close"]

        if not close_dfs:
            return

        # 合并, ffill 防节假日空档 (虽然交易日都有, 容错)
        all_closes = pd.concat(close_dfs, axis=1).sort_index().ffill()

        new_points: List[dict] = []
        for ts, row in all_closes.iterrows():
            d_str = ts.strftime("%Y-%m-%d")
            # 只回放 < today (今天用真实盘中数据)
            if d_str >= today_str:
                continue
            mv = 0.0
            for p in positions_cfg:
                code = p.get("code", "")
                vol = int(p.get("volume", 0))
                px = row.get(code)
                if px is None or pd.isna(px):
                    continue
                mv += vol * float(px)
            asset = cash + mv
            pnl = asset - capital
            pct = pnl / capital if capital > 0 else 0.0
            # ts 用 14:59:00 而非 15:00:00 -- 避免踩在 plotly rangebreak [15, 9.5] 边界
            # 上被当作非交易时段过滤掉
            new_points.append({
                "ts":      f"{d_str}T14:59:00",
                "pnl":     round(pnl, 2),
                "pnl_pct": round(pct, 4),
            })

        if not new_points:
            return

        # 把回放点拼到 pnl_history 前面 (今天的分时点保持原顺序)
        merged = new_points + hist
        # 与 update_pnl 一致, 末位保留 500 条
        s = self._loop.state_store.load()
        s["pnl_history"] = merged[-500:]
        self._loop.state_store.save(s)
        print(f"[OK] 历史资金曲线回放: 写入 {len(new_points)} 个交易日点 "
              f"({start_date} ~ 昨天)", flush=True)

    # ------------------------------------------------------------------
    def _seed_today_intraday_curve(self) -> None:
        """用今日 5 分钟 K 补 9:30 到「已有最早今天点」之间的分时.

        中午才启动时 worker 只有启动后的分钟点, 当日图会只剩一个点贴在轴上.
        已有今天的实时点不覆盖.
        """
        from datetime import date
        s = self._loop.state_store.load()
        hist = s.get("pnl_history") or []
        today_str = date.today().strftime("%Y-%m-%d")
        today_pts = [p for p in hist if (p.get("ts") or "")[:10] == today_str]
        cutoff = min((p.get("ts") or "") for p in today_pts) if today_pts \
            else f"{today_str}T23:59:59"

        cfg = load_mock_config()
        capital = float(cfg.get("capital", 1_000_000))
        positions_cfg = cfg.get("positions", []) or []
        if not positions_cfg:
            return
        cost_used = sum(int(p.get("volume", 0)) * float(p.get("cost", 0))
                        for p in positions_cfg)
        cash = capital - cost_used

        market = getattr(self._loop, "market", None)
        if market is None:
            return
        import pandas as pd
        close_dfs = {}
        for p in positions_cfg:
            code = p.get("code", "")
            try:
                df = market.get_recent_kline(code, period="5m", count=80)
            except Exception as e:
                print(f"[WARN] 当日分时 seed: 拉 {code} 失败: {e}", flush=True)
                continue
            if df is None or df.empty or "close" not in df.columns:
                continue
            ser = df["close"].copy()
            ser.index = pd.to_datetime(ser.index)
            ser = ser.loc[ser.index.strftime("%Y-%m-%d") == today_str]
            if ser.empty:
                continue
            close_dfs[code] = ser
        if not close_dfs:
            return

        all_closes = pd.concat(close_dfs, axis=1).sort_index().ffill()
        new_points: List[dict] = []
        for ts, row in all_closes.iterrows():
            hhmm = ts.strftime("%H:%M")
            if hhmm < "09:30" or hhmm > "15:00":
                continue
            if "11:30" < hhmm < "13:00":
                continue
            d_str = ts.strftime("%Y-%m-%dT%H:%M:%S")
            if d_str >= cutoff:
                continue
            mv = 0.0
            for p in positions_cfg:
                code = p.get("code", "")
                vol = int(p.get("volume", 0))
                px = row.get(code)
                if px is None or pd.isna(px):
                    continue
                mv += vol * float(px)
            asset = cash + mv
            pnl = asset - capital
            pct = pnl / capital if capital > 0 else 0.0
            new_points.append({
                "ts": d_str,
                "pnl": round(pnl, 2),
                "pnl_pct": round(pct, 4),
            })
        if not new_points:
            return

        pre = [p for p in hist if (p.get("ts") or "")[:10] != today_str]
        merged = pre + new_points + today_pts
        s = self._loop.state_store.load()
        s["pnl_history"] = merged[-500:]
        self._loop.state_store.save(s)
        print(f"[OK] 当日分时回放: 写入 {len(new_points)} 个 5 分钟点", flush=True)

    # ------------------------------------------------------------------
    def _seed_historical_signals_if_empty(self, watch_stocks: List[str]) -> None:
        """启动时把 [SIM_HISTORY_START_DATE, today] 的策略信号回放进 state.signals

        - 仅在 state.signals 当前为空时跑 (避免重启后重复回放)
        - 调 backtest_engine.run_backtest 拿每只股票按其路由策略跑出的所有 buy/sell
        - 同时按时间顺序把成交写进 positions: 卖出平掉该股, 买入仅在空仓时开
          (策略继续盯 volume=0 的行)
        - 先把 mock_positions 记成 start_date 的买入, 再回放策略成交
        - 起始日期: 环境变量 SIM_HISTORY_START_DATE (默认 2026-04-01, 与 mock_positions
          初始持仓的 cost 日对齐)
        """
        # 已有信号 -> 不重放, 避免每次重启都重复
        s = self._loop.state_store.load()
        if s.get("signals"):
            return
        if not watch_stocks:
            return
        msg = self.replay_book(watch_stocks)
        print(f"[OK] 历史信号回放: {msg}", flush=True)

    # ------------------------------------------------------------------
    def clear_history(self) -> str:
        """清空 events / signals / orders / pnl_history (持仓不动)
        用于清掉之前测试遗留的非盘中脏数据
        """
        from lib.paths import OUTPUTS_LIVE_STATE
        import json, os
        if not OUTPUTS_LIVE_STATE.exists():
            return "[ERROR] live_state.json 不存在"
        try:
            s = json.loads(OUTPUTS_LIVE_STATE.read_text(encoding="utf-8"))
        except Exception:
            s = {}
        cleared = (len(s.get("events", [])) + len(s.get("signals", []))
                   + len(s.get("orders", [])) + len(s.get("pnl_history", [])))
        s["events"]      = []
        s["signals"]     = []
        s["orders"]      = []
        s["pnl_history"] = []
        s["today_pnl"]   = 0
        s["today_pnl_pct"] = 0
        s["_updated_at"] = datetime.now().isoformat(timespec="seconds")
        tmp = OUTPUTS_LIVE_STATE.with_suffix(".tmp")
        tmp.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, OUTPUTS_LIVE_STATE)
        return f"[OK] 已清空 {cleared} 条历史 (events/signals/orders/pnl), 持仓保留"

    # ------------------------------------------------------------------
    def _read_live_state(self) -> dict:
        if self._loop is not None:
            return self._loop.state_store.load()
        from lib.paths import OUTPUTS_LIVE_STATE
        import json
        if not OUTPUTS_LIVE_STATE.exists():
            raise FileNotFoundError("live_state.json 不存在, 先启动一次模拟盘")
        return json.loads(OUTPUTS_LIVE_STATE.read_text(encoding="utf-8"))

    def _write_live_state(self, s: dict) -> None:
        s["_updated_at"] = datetime.now().isoformat(timespec="seconds")
        if self._loop is not None:
            self._loop.state_store.save(s)
            return
        from lib.paths import OUTPUTS_LIVE_STATE
        import json, os
        tmp = OUTPUTS_LIVE_STATE.with_suffix(".tmp")
        tmp.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, OUTPUTS_LIVE_STATE)

    def _ensure_router(self):
        if self._router is None:
            from lib.strategy_registry import StrategyRouter
            cfg = load_strategy_config()
            self._router = StrategyRouter(
                per_stock=cfg.get("per_stock", {}),
                default=cfg.get("default", "macd_1d"),
            )
        return self._router

    def _parse_reset_rows(self, rows: List[dict], max_slots: int) -> Tuple[List[str], dict]:
        """解析重置草稿: 每档一只股票 + 策略, 回写前先算完"""
        from lib.backtest_data import resolve_stock_query
        if len(rows) > max_slots:
            raise ValueError(f"超过最多 {max_slots} 档")
        if not rows:
            raise ValueError("至少一档要有股票")
        cfg = load_strategy_config()
        per_stock = dict(cfg.get("per_stock") or {})
        slot_codes: List[str] = []
        seen = set()
        for row in rows:
            raw = str((row or {}).get("code") or "").strip()
            strat = str((row or {}).get("strategy") or "").strip()
            if not raw or not strat:
                raise ValueError("每一档都要有股票和策略")
            code, _name = resolve_stock_query(raw)
            if not code:
                raise ValueError(f"无法识别股票: {raw}")
            if code in seen:
                raise ValueError(f"{code} 重复")
            seen.add(code)
            slot_codes.append(code)
            per_stock[code] = strat
        return slot_codes, per_stock

    def reset_positions(self, start_date: str = "", rows: Optional[List[dict]] = None,
                        capital=None, max_slots=None) -> str:
        """按草稿(或当前档位) 从指定起始日重新开仓并回放; 成功后才写盘"""
        from datetime import date as date_cls
        from lib.paths import setup_sys_path
        setup_sys_path()
        try:
            start_date = _parse_start_date(start_date)
        except ValueError as e:
            return f"[ERROR] {e}"
        try:
            s = self._read_live_state()
        except FileNotFoundError as e:
            return f"[ERROR] {e}"
        except Exception:
            s = {}

        ctrl = s.setdefault("control", {})
        if capital is None:
            capital = s.get("capital")
        try:
            capital = float(capital)
        except (TypeError, ValueError):
            return "[ERROR] 本金必须是数字"
        if capital < 1:
            return "[ERROR] 本金必须大于 0"
        if max_slots is None:
            max_slots = ctrl["max_slots"]
        try:
            max_slots = int(max_slots)
        except (TypeError, ValueError):
            return "[ERROR] 档数必须是整数"
        if max_slots < 1 or max_slots > 10:
            return "[ERROR] 档数须在 1～10"
        cfg = load_strategy_config()
        if rows is not None:
            try:
                slot_codes, per_stock = self._parse_reset_rows(rows, max_slots)
            except ValueError as e:
                return f"[ERROR] {e}"
        else:
            slot_codes = [str(c) for c in ctrl["slot_codes"]]
            per_stock = dict(cfg.get("per_stock") or {})
        replay_router = type("ReplayRouter", (), {
            "per_stock": per_stock,
            "default": cfg.get("default") or "macd_1d",
        })()
        watch = list(slot_codes)
        end_date = date_cls.today().strftime("%Y-%m-%d")

        try:
            all_sigs, kline_map = collect_history_signals(
                watch, start_date, end_date, replay_router,
            )
        except Exception as e:
            return f"[ERROR] 策略回放失败: {e}"

        from lib.backtest_data import get_stock_names, load_daily_kline
        names = get_stock_names(watch)
        for code in slot_codes:
            if code in kline_map:
                continue
            try:
                df = load_daily_kline(code, start_date=start_date, end_date=end_date)
            except Exception as e:
                print(f"[WARN] 重置开仓: 拉 {code} 失败: {e}", flush=True)
                continue
            if df is None or df.empty:
                continue
            bars = []
            for ts, row in df.iterrows():
                bars.append({
                    "date": ts.strftime("%Y-%m-%d") if hasattr(ts, "strftime") else str(ts)[:10],
                    "close": float(row["close"]),
                })
            kline_map[code] = bars

        opening = build_opening_from_slots(
            slot_codes, kline_map, start_date, capital, max_slots, names,
        )

        def _strat(code: str) -> str:
            return per_stock.get(code) or replay_router.default

        ctrl["max_slots"] = max_slots
        ctrl["slot_codes"] = slot_codes

        opening_orders = opening_buy_orders(opening, start_date, _strat)
        if all_sigs:
            positions, fill_orders = apply_history_fills(
                opening, capital, all_sigs, kline_map, max_slots, slot_codes,
            )
        else:
            positions, fill_orders = opening, []

        from live_trading.live_loop import calc_today_pnl
        today_pnl, today_pct = calc_today_pnl(positions, capital)
        ctrl["history_start_date"] = start_date
        s["capital"] = capital
        s["positions"] = positions
        s["signals"] = all_sigs[-100:]
        s["orders"] = (opening_orders + fill_orders)[-500:]
        s["events"] = []
        s["pnl_history"] = buyhold_pnl_points(opening, capital, start_date)
        s["today_pnl"] = today_pnl
        s["today_pnl_pct"] = today_pct
        wp = load_watch_pool()
        watch_codes = list(wp.get("codes") or [])
        for c in slot_codes:
            if c not in watch_codes:
                watch_codes.append(c)
        save_watch_pool(watch_codes)
        self.apply_strategy_config(cfg.get("default") or "macd_1d", per_stock, replay=False)
        self.apply_capital(capital)
        self._write_live_state(s)
        save_slot_book(max_slots, slot_codes, capital, opening)
        filled = sum(1 for o in (opening_orders + fill_orders) if o.get("status") == "dry_run")
        return (
            f"[OK] 已从 {start_date} 重置 -- {len(slot_codes)} 档开仓, "
            f"回放 {len(all_sigs)} 条信号 / {filled} 笔成交"
        )
