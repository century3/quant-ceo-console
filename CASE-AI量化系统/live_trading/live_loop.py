# -*- coding: utf-8 -*-
# Author：@跟陈博士学AI
# 23-CASE-A: 盘中全自动交易闭环主循环
"""
LiveLoop -- 盘中全自动交易闭环主循环

每隔 N 分钟跑一遍, 完成: 拉行情 -> 评估持仓 -> 跑信号 -> 风控审批 -> 下单 -> 推送

核心架构 (LangGraph 风格, 但简化为顺序循环, 因为每分钟级延迟比 LangGraph 启动开销重要):

    每分钟循环:
        1. health_check()          检查 QMT 连接 + 行情数据完整性
        2. update_positions()      拉最新持仓 + 当日盈亏
        3. check_circuit_breaker() 当日亏损是否触发熔断
        4. evaluate_stop_loss()    持仓股是否触发止损
        5. evaluate_signals()      候选股是否出现新信号
        6. risk_check()            风控审批 (Kris 规则)
        7. place_orders()          下单 (本 CASE live_trading.qmt_trader_v2)
        8. push_summary()          推送告警 (alert_router)
        9. save_state()            落盘 state (供 CEO 控制台读)

异常处理金字塔:
    L1 数据层异常 -> 跳过本轮, 下轮继续, 不告警 (网络抖动)
    L2 风控否决   -> 不下单, INFO 推送
    L3 订单失败   -> WARN 推送, 重试 1 次
    L4 系统级异常 -> CRITICAL 推送 + 暂停所有交易 (state.trading_status = "HALTED")
    L5 不可恢复   -> FATAL 推送 + 进程退出 + 等人工

注意:
    - 真正的实盘需要接 QMT (HTTP 或 miniQMT), 在 dry-run 下用模拟数据 (适合教学/演示)
    - 信号评估这里可用 MACD/RSI 占位，实战可替换为自有选股 / 路由输出。
"""

from __future__ import annotations
import json
import math
import os
import random
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

THIS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = THIS_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT))

from alerting.alert_router import AlertRouter
from live_trading.state_store import StateStore


# ============================================================
# 数据来源 (QMT HTTP / miniQMT 行情 + dry-run 模拟下单)
# tdx 没有盘中 tick，用 MySQL 最近收盘价
# ============================================================

from lib.qmt_backend import is_miniqmt, is_tdx, label as tool_label


def _qmt_http(path, payload=None, method='POST', timeout=30):
    from lib.qmt_backend import current
    if current() == 'qmt_shared':
        from lib.qmt_shared import shared_http
        return shared_http(path, payload, method, timeout)
    import requests
    base = os.getenv("QMT_BASE_URL", "http://127.0.0.1:15588").rstrip("/")
    token = os.getenv("QMT_TOKEN", "learning_ai_with_dr_chen")
    resp = requests.request(method, f"{base}{path}",
                            headers={"Content-Type": "application/json", "X-Token": token},
                            json=payload or {}, timeout=timeout, proxies={"http": None, "https": None})
    resp.raise_for_status()
    return resp.json()


def _ticks_from_mysql(codes: List[str]) -> dict:
    """用库里最近收盘价冒充 tick，给通达信日线模拟盘用。"""
    from lib.backtest_data import load_daily_kline
    out = {}
    for code in codes:
        try:
            df = load_daily_kline(code)
            if df is None or df.empty:
                continue
            last = float(df.iloc[-1]["close"])
            out[code] = {"lastPrice": last, "lastClose": last}
        except Exception:
            continue
    return out


def _key_to_ms(key):
    import pandas as pd
    s = str(key)
    if s.isdigit():
        if len(s) == 8:     ts = pd.to_datetime(s, format='%Y%m%d')
        elif len(s) == 14:  ts = pd.to_datetime(s, format='%Y%m%d%H%M%S')
        elif len(s) >= 13:  return int(s)
        else:               ts = pd.to_datetime(s, errors='coerce')
    else:
        ts = pd.to_datetime(s, errors='coerce')
    return int(ts.value // 10**6) if ts is not pd.NaT else 0


class MarketDataProvider:
    """行情：qmt / miniqmt 走盘中 tick；tdx 用 MySQL 最新日线收盘价。"""

    def __init__(self):
        self._connected = False

    def connect(self):
        if is_tdx():
            from lib.qmt_backend import probe
            ready = probe()
            if not ready["ok"]:
                raise RuntimeError(ready["message"])
        elif is_miniqmt():
            from xtquant import xtdata
            xtdata.connect()
        else:
            _qmt_http('/api/sys/python_version', method='GET')
        self._connected = True

    def get_latest_ticks(self, stock_codes: List[str]) -> dict:
        """批量最新价。tdx 没有盘中 tick，用库里最近收盘。"""
        codes = [str(c).strip() for c in (stock_codes or []) if str(c).strip()]
        if not codes:
            return {}
        us_codes = [c for c in codes if c.upper().endswith(".US")]
        a_codes = [c for c in codes if not c.upper().endswith(".US")]
        out = _ticks_from_mysql(us_codes) if us_codes else {}
        if not a_codes:
            return out
        try:
            if not self._connected:
                self.connect()
        except Exception:
            return out
        if is_tdx():
            out.update(_ticks_from_mysql(a_codes))
            return out
        if is_miniqmt():
            from xtquant import xtdata
            out.update(xtdata.get_full_tick(a_codes) or {})
            return out
        body = _qmt_http('/api/data/full_tick', {"stocks": ",".join(a_codes)}, timeout=3)
        if isinstance(body, dict) and not body.get("error"):
            out.update({k: v for k, v in body.items() if k != "error" and isinstance(v, dict)})
        return out

    def get_latest_tick(self, stock_code: str) -> dict:
        """拉最新 tick (含 5 档盘口)"""
        return self.get_latest_ticks([stock_code]).get(stock_code, {})

    def get_recent_kline(self, stock_code: str, period: str = "1d",
                        count: int = 50) -> Optional[Any]:
        """拉最近 N 根 K 线 (用于算指标)，返回 DatetimeIndex + OHLCV 的 DataFrame"""
        import pandas as pd
        if str(stock_code).upper().endswith(".US"):
            if period not in ("1d", "1day", "day", "daily"):
                return None
            try:
                from lib.backtest_data import load_daily_kline
                df = load_daily_kline(stock_code)
            except Exception:
                return None
            if df is None or len(df) == 0:
                return None
            return df.tail(count)
        if not self._connected:
            self.connect()
        try:
            fields = ["open", "high", "low", "close", "volume"]
            if is_tdx():
                from lib.backtest_data import load_daily_kline
                df = load_daily_kline(stock_code)
                if df is None or len(df) == 0:
                    return None
                return df.tail(count)
            if is_miniqmt():
                from xtquant import xtdata
                xtdata.download_history_data(stock_code, period=period,
                                             start_time="20250101", incrementally=True)
                data = xtdata.get_market_data_ex(
                    field_list=fields, stock_list=[stock_code],
                    period=period, count=count, dividend_type="front")
                df = data.get(stock_code)
                if df is None or len(df) == 0:
                    return None
                df = df.copy()
                df.index = pd.to_datetime(df.index)
                return df

            # QMT HTTP：/api/data/market_data_ex（Server 按需取数，无需 download）
            body = _qmt_http('/api/data/market_data_ex', {
                "stock_code": stock_code, "fields": ",".join(fields),
                "period": period, "start_time": "", "end_time": "",
                "count": count, "dividend_type": "front"})
            per_stock = (body.get("data") or {}).get(stock_code, {})
            if not per_stock:
                return None
            keys = sorted({k for fld in per_stock.values() for k in fld.keys()})
            rows = []
            for k in keys:
                row = {"time": _key_to_ms(k)}
                for fld in fields:
                    if fld in per_stock and k in per_stock[fld]:
                        row[fld] = per_stock[fld][k]
                rows.append(row)
            df = pd.DataFrame(rows)
            df["time"] = pd.to_datetime(df["time"], unit="ms")
            df = df.set_index("time")
            return df
        except Exception:
            return None


# ============================================================
# 信号评估 (简化版 MACD)
# ============================================================

def evaluate_macd_signal(df) -> str:
    """
    评估 MACD 信号
    返回: "buy" / "sell" / "hold"
    """
    import pandas as pd
    if df is None or len(df) < 30:
        return "hold"
    close = df["close"].astype(float)
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    dif = ema12 - ema26
    dea = dif.ewm(span=9, adjust=False).mean()

    # 最新两根: 看是否金叉/死叉
    if len(dif) < 2:
        return "hold"
    prev = dif.iloc[-2] - dea.iloc[-2]
    curr = dif.iloc[-1] - dea.iloc[-1]
    if prev <= 0 and curr > 0:
        return "buy"
    if prev >= 0 and curr < 0:
        return "sell"
    return "hold"


# ============================================================
# 持仓与盈亏更新
# ============================================================

def _refresh_position_pnl(pos: dict) -> dict:
    """pnl = 已实现 + 浮动; 百分比相对累计买入金额 buy_notional"""
    vol = int(pos.get("volume") or 0)
    cost = float(pos.get("cost") or 0)
    cur = float(pos.get("cur_price") or 0)
    realized = float(pos.get("realized_pnl") or 0)
    notion = float(pos.get("buy_notional") or 0)
    unreal = (cur - cost) * vol if vol > 0 else 0.0
    total = realized + unreal
    pos["pnl"] = round(total, 2)
    pos["pnl_pct"] = round(total / notion, 4) if notion > 0 else 0.0
    pos["market_value"] = round(vol * cur, 2)
    pos["realized_pnl"] = round(realized, 2)
    pos["buy_notional"] = round(notion, 2)
    return pos


def tick_last_price(tick: dict) -> float:
    """full_tick 的最新价; 没有或非法则 0"""
    if not tick:
        return 0.0
    raw = tick.get("lastPrice")
    if raw in (None, ""):
        return 0.0
    px = float(raw)
    return px if px > 0 else 0.0


def update_positions_from_market(positions: List[dict],
                                 market: MarketDataProvider) -> List[dict]:
    """用 QMT 最新 tick 刷新现价 / 市值 / 盈亏"""
    codes = [p.get("code") for p in (positions or []) if p.get("code")]
    ticks = market.get_latest_ticks(codes)
    updated = []
    for pos in positions:
        code = pos["code"]
        last = tick_last_price(ticks.get(code) or {})
        cur_price = last if last > 0 else float(pos.get("cur_price") or 0)
        row = {**pos, "cur_price": round(cur_price, 3)}
        updated.append(_refresh_position_pnl(row))
    return updated


def calc_today_pnl(positions: List[dict], capital: float) -> tuple:
    """看板 vs 初始: 各股累计盈亏之和"""
    total_pnl = sum(p.get("pnl", 0) for p in positions)
    total_pct = total_pnl / capital if capital > 0 else 0
    return round(total_pnl, 2), round(total_pct, 4)

def _held_volume(positions: List[dict], code: str) -> int:
    """当前持仓股数 (找不到或为 0 返回 0)"""
    for p in positions or []:
        if p.get("code") == code:
            return int(p.get("volume") or 0)
    return 0


def _available_cash(positions: List[dict], capital: float) -> float:
    """可用现金 = 初始资金 - 持仓成本占用 (开仓与仓位管理同一口径)"""
    cost_used = sum(
        float(p.get("cost") or 0) * int(p.get("volume") or 0)
        for p in (positions or [])
    )
    return max(0.0, float(capital) - cost_used)


def read_max_slots(state: dict) -> int:
    """最多同时几档, 来自 control.max_slots"""
    return int((state.get("control") or {})["max_slots"])


def read_slot_codes(state: dict) -> List[str]:
    """已分档代码, 来自 control.slot_codes"""
    return [str(c) for c in (state.get("control") or {})["slot_codes"]]


def slot_budget(capital: float, max_slots: int) -> float:
    """每档目标金额 = 总资金 / 档数"""
    return float(capital) / int(max_slots)


def share_lot(code: str) -> int:
    """A 股 100 股一手。美股按 1 股成交。"""
    return 1 if str(code).upper().endswith(".US") else 100


def is_us_code(code: str) -> bool:
    return str(code).upper().endswith(".US")


def session_market(codes: List[str]) -> str:
    """本轮代码属于哪边市场: us / cn / both / 空。"""
    us = any(is_us_code(c) for c in codes)
    cn = any(c and not is_us_code(c) for c in codes)
    if us and cn:
        return "both"
    if us:
        return "us"
    if cn:
        return "cn"
    return ""


def unrealized_by_market(positions: List[dict]) -> tuple:
    """持仓浮动盈亏拆成 A 股(人民币) 和美股(美元面额)。"""
    cn = 0.0
    us = 0.0
    for pos in positions or []:
        vol = int(pos.get("volume") or 0)
        if vol <= 0:
            continue
        pnl = (float(pos.get("cur_price") or 0) - float(pos.get("cost") or 0)) * vol
        if is_us_code(pos.get("code") or ""):
            us += pnl
        else:
            cn += pnl
    return cn, us


def flatten_open_positions(positions: List[dict], price_of) -> tuple:
    """把股数大于 0 的持仓全部卖掉，A 股和美股都平。

    price_of(code) 返回最新价。没有价格的留下，并在 notes 里说明。
    返回 (新持仓, 成交, 事件说明)。
    """
    positions = [dict(p) for p in (positions or [])]
    orders = []
    notes = []
    for pos in list(positions):
        code = str(pos.get("code") or "")
        vol = int(pos.get("volume") or 0)
        if not code or vol <= 0:
            continue
        price = float(price_of(code) or 0)
        if price <= 0:
            notes.append({
                "level": "WARN",
                "title": f"清仓跳过 {code}",
                "message": "没有最新价",
            })
            continue
        positions = apply_dry_run_fill(
            positions, code, "sell", vol, price, name=str(pos.get("name") or ""),
        )
        orders.append({
            "code": code,
            "side": "sell",
            "quantity": vol,
            "price": round(price, 4),
            "amount": round(vol * price, 2),
            "status": "dry_run",
            "reason": "一键清仓",
            "strategy": "",
        })
        unit = "美元" if is_us_code(code) else "元"
        notes.append({
            "level": "CRITICAL",
            "title": f"一键清仓 {code} {vol}股 @ {price:.2f}{unit}",
            "message": str(pos.get("name") or ""),
        })
    return positions, orders, notes


def size_buy_shares(price: float, budget: float, cash: float, lot: int = 100) -> int:
    """开仓股数: min(该档目标金额, 可用现金), 按手数取整; 额度不到 1 手则试探 1 手."""
    price = float(price)
    lot = int(lot)
    if price <= 0 or lot <= 0:
        return 0
    qty = int(float(budget) / price / lot) * lot
    if qty <= 0:
        qty = lot
    max_by_cash = int(float(cash) / price / lot) * lot
    if max_by_cash <= 0:
        return 0
    return min(qty, max_by_cash)


def apply_dry_run_fill(positions: List[dict], code: str, side: str,
                       quantity: int, price: float,
                       name: str = "") -> List[dict]:
    """模拟撮合: 把一笔 buy/sell 写进 positions.

    - buy: 无持仓则新建; 有持仓则加权平均成本加仓; buy_notional 累加
    - sell: 减仓并累加 realized_pnl; 卖光后 volume=0 保留行
    """
    positions = [dict(p) for p in (positions or [])]
    price = float(price)
    quantity = int(quantity)
    if quantity <= 0 or price <= 0:
        return positions

    idx = next((i for i, p in enumerate(positions) if p.get("code") == code), -1)

    if side == "buy":
        if idx < 0:
            row = {
                "code": code,
                "name": name or code,
                "volume": quantity,
                "cost": round(price, 4),
                "cur_price": round(price, 3),
                "realized_pnl": 0.0,
                "buy_notional": round(quantity * price, 2),
            }
            positions.append(_refresh_position_pnl(row))
        else:
            pos = positions[idx]
            old_vol = int(pos.get("volume") or 0)
            old_cost = float(pos.get("cost") or 0)
            notion = float(pos.get("buy_notional") or 0)
            new_vol = old_vol + quantity
            if old_vol > 0:
                new_cost = (old_vol * old_cost + quantity * price) / new_vol
            else:
                new_cost = price
            pos["volume"] = new_vol
            pos["cost"] = round(new_cost, 4)
            pos["cur_price"] = round(price, 3)
            pos["buy_notional"] = round(notion + quantity * price, 2)
            _refresh_position_pnl(pos)
        return positions

    if side == "sell":
        if idx < 0:
            return positions
        pos = positions[idx]
        old_vol = int(pos.get("volume") or 0)
        sell_qty = min(quantity, old_vol)
        if sell_qty <= 0:
            return positions
        cost = float(pos.get("cost") or 0)
        realized = float(pos.get("realized_pnl") or 0)
        pos["realized_pnl"] = realized + (price - cost) * sell_qty
        pos["volume"] = old_vol - sell_qty
        pos["cur_price"] = round(price, 3)
        _refresh_position_pnl(pos)
        return positions

    return positions



# ============================================================
# 主循环
# ============================================================

class LiveTradingLoop:
    """
    实盘主循环 (默认 dry-run)

    用法:
        loop = LiveTradingLoop(watch_stocks=["600519.SH", "513100.SH"])
        loop.run_once()         # 跑一次
        loop.run_forever(60)    # 每 60 秒跑一次, 直到 Ctrl+C
    """

    def __init__(self,
                 watch_stocks: List[str],
                 capital: float = 1_000_000,
                 state_file: str = "outputs/live_state.json",
                 max_daily_loss_pct: float = -0.02,
                 dry_run: bool = True,
                 signal_evaluator: Optional[Callable[[str, "MarketDataProvider", float], dict]] = None,
                 max_slots: int = 5,
                 slot_codes: Optional[List[str]] = None):
        """
        signal_evaluator: 可选的信号评估器, 签名 (code, market, capital) -> dict
            返回字典 {"side": "buy"/"sell"/"hold", "strategy": str, "reason": str (可选)}
            不传 = 日 K MACD 金叉/死叉
        """
        self.watch_stocks = watch_stocks
        self.capital = capital
        self.dry_run = dry_run
        self.max_daily_loss_pct = max_daily_loss_pct
        self.signal_evaluator = signal_evaluator

        self.state_store = StateStore(state_file)
        self.market = MarketDataProvider()
        self.alert = AlertRouter(info_aggregate_seconds=300)  # INFO 聚合 5 分钟; WARN/FATAL 主循环未用

        # 初始化 state
        s = self.state_store.load()
        s["capital"] = capital
        s["watch_stocks"] = watch_stocks
        s["control"]["dry_run"] = dry_run
        s["control"]["max_daily_loss"] = max_daily_loss_pct
        s["control"]["max_slots"] = int(max_slots)
        s["control"]["slot_codes"] = list(slot_codes or [])
        self.state_store.save(s)

    # ------------------------------------------------------------------
    # 单次循环
    # ------------------------------------------------------------------
    def run_once(self, active_codes: Optional[List[str]] = None) -> dict:
        """跑一次完整循环。active_codes 只处理当前开盘的代码，空则用全部监控。"""
        cycle_start = time.time()
        s = self.state_store.load()
        codes = list(self.watch_stocks if active_codes is None else active_codes)

        # 0) 检查 control.trading_status
        if s.get("trading_status") == "HALTED":
            self._note(s, "WARN", "交易已熔断, 跳过本轮", source="loop")
            self.state_store.save(s)
            return {"action": "halted_skip"}
        if s.get("trading_status") == "PAUSED":
            self._note(s, "INFO", "交易已暂停 (CEO 控制台暂停)", source="loop")
            self.state_store.save(s)
            return {"action": "paused_skip"}

        # 1) health_check。这一轮只有美股时不连 QMT，行情在 trade_stock_us_daily。
        need_qmt = any(not str(c).upper().endswith(".US") for c in codes)
        if need_qmt:
            try:
                self.market.connect()
                s["health"]["qmt_connected"] = True
                s["health"]["last_heartbeat"] = datetime.now().isoformat(timespec="seconds")
            except Exception as e:
                s["health"]["qmt_connected"] = False
                s["health"]["errors_24h"] = s["health"].get("errors_24h", 0) + 1
                self._note(s, "CRITICAL", f"{tool_label()} 连接失败",
                           message=str(e), source="health")
                self.state_store.save(s)
                return {"action": "health_fail"}
        else:
            s["health"]["last_heartbeat"] = datetime.now().isoformat(timespec="seconds")

        # 2) 更新持仓 + 当日盈亏。只刷新本轮开盘的代码，收盘的市场保留上次价格。
        positions = s.get("positions", [])
        if positions:
            active = {str(c) for c in codes}
            refreshing = [p for p in positions if p.get("code") in active]
            if refreshing:
                refreshed = {p["code"]: p for p in update_positions_from_market(refreshing, self.market)}
                positions = [refreshed.get(p.get("code"), p) for p in positions]
            today_pnl, today_pnl_pct = calc_today_pnl(positions, self.capital)
            s["positions"] = positions
            s["today_pnl"] = today_pnl
            s["today_pnl_pct"] = today_pnl_pct
            s["pnl_history"] = s.get("pnl_history", [])
            s["pnl_history"].append({
                "ts": datetime.now().isoformat(timespec="seconds"),
                "pnl": today_pnl, "pnl_pct": today_pnl_pct,
            })
            s["pnl_history"] = s["pnl_history"][-500:]

        # 3) 熔断检查
        # 熔断看当前持仓浮动, 不含 4-1 以来已实现 (累计亏会一启动就熔断)
        # A 股按人民币、美股按美元面额，加进同一条线
        cn_unreal, us_unreal = unrealized_by_market(s.get("positions") or [])
        unreal = cn_unreal + us_unreal
        unreal_pct = unreal / self.capital if self.capital else 0
        if unreal_pct <= self.max_daily_loss_pct:
            s["trading_status"] = "HALTED"
            self._note(
                s, "CRITICAL", "触发当日亏损熔断",
                message=(f"浮动盈亏 {unreal_pct:.2%}, "
                         f"已跌破熔断线 {self.max_daily_loss_pct:.2%}; "
                         f"A股 {cn_unreal:,.0f} 元, 美股 {us_unreal:,.0f} 美元"),
                source="circuit_breaker",
            )
            self.state_store.save(s)
            return {"action": "circuit_breaker"}

        # 4) 评估信号 (默认对 watch 池每只算 MACD; 注入了 signal_evaluator 则按 evaluator 派发)
        new_signals = []
        for code in codes:
            if self.signal_evaluator is not None:
                # 外部注入的信号路由器: 由 evaluator 自己决定用哪个策略
                try:
                    result = self.signal_evaluator(code, self.market, self.capital)
                except Exception as e:
                    self._note(s, "WARN", f"signal_evaluator 异常 {code}",
                               message=str(e), source="zoe")
                    continue
                if not result:
                    continue
                side = result.get("side", "hold")
                if side == "hold":
                    continue
                sig = {
                    "code":     code,
                    "side":     side,
                    "strategy": result.get("strategy", "unknown"),
                    "reason":   result.get("reason", ""),
                }
            else:
                df = self.market.get_recent_kline(code, period="1d", count=80)
                side = evaluate_macd_signal(df)
                if side == "hold":
                    continue
                sig = {"code": code, "side": side, "strategy": "macd_1d"}
            new_signals.append(sig)

        # 注意: signals / orders / events 都先攒在内存 s 里, 第 6 步统一一次 save 落盘
        # -- 不能用 append_signal / append_order, 否则它们 load->改->save, 会被第 6 步的
        # save(s) 用旧快照整体覆盖回去, 导致 signals / orders 写完即丢
        if new_signals:
            s["signals"] = s.get("signals", [])
            for sig in new_signals:
                s["signals"].append({
                    **sig,
                    "ts": datetime.now().isoformat(timespec="seconds"),
                })
                self._note(
                    s, "INFO", f"信号触发 -> {sig['side']} {sig['code']} [{sig.get('strategy','')}]",
                    source="zoe",
                )
            s["signals"] = s["signals"][-100:]

        # 5) 风控 + 下单
        # skipped / 无仓可卖 不写流水 (水平策略每轮都会给同向信号, 写了会刷屏)
        s["orders"] = s.get("orders", [])
        for sig in new_signals:
            order_result = self._handle_signal(s, sig)
            # 把触发该订单的策略名一起记录, 便于复盘
            if "strategy" not in order_result and sig.get("strategy"):
                order_result["strategy"] = sig["strategy"]
            status = order_result.get("status")
            # rejected 里「拿不到价格 / 现金不足」仍记录; 仅过滤持仓门禁类
            reason = str(order_result.get("reason") or "")
            if status == "skipped" or (
                status == "rejected"
                and ("无持仓" in reason or "可卖数量" in reason or "同向不重复" in reason)
            ):
                continue
            s["orders"].append({
                **order_result,
                "ts": datetime.now().isoformat(timespec="seconds"),
            })
        s["orders"] = s["orders"][-500:]

        # 6) 落盘 state (本轮所有改动: positions/today_pnl/pnl_history/health/signals/orders/events 一锅端)
        self._note_cycle(s, codes, new_signals, int((time.time() - cycle_start) * 1000))
        self.state_store.save(s)

        return {
            "action":      "cycle_done",
            "duration_ms": int((time.time() - cycle_start) * 1000),
            "new_signals": len(new_signals),
        }

    def _handle_signal(self, state: dict, signal: dict) -> dict:
        """处理一个信号: 风控 -> 下单(dry-run 会真正改持仓) -> 推送"""
        code = signal["code"]
        side = signal["side"]
        tick = self.market.get_latest_tick(code)
        price = float(tick.get("lastPrice", 0))
        if price <= 0:
            return {"code": code, "side": side, "status": "rejected",
                    "reason": "拿不到价格", "ts": datetime.now().isoformat()}

        positions = state.get("positions") or []
        held = _held_volume(positions, code)

        # 持仓状态门禁 (水平型策略如 boll/dragon 每轮都可能给同向信号):
        #   已持仓不再买 / 空仓不卖 -- 否则「成交流水」每分钟刷一条但语义是假成交
        if side == "buy" and held > 0:
            return {"code": code, "side": side, "quantity": 0, "price": price,
                    "status": "skipped", "reason": f"已持仓 {held} 股, 同向不重复开仓",
                    "ts": datetime.now().isoformat()}
        if side == "sell" and held <= 0:
            return {"code": code, "side": side, "quantity": 0, "price": price,
                    "status": "rejected", "reason": "无持仓可卖",
                    "ts": datetime.now().isoformat()}

        # 记名档位: 未分档不买; 已分档按每档目标金额开; 卖出一口卖光, 档还在
        if side == "sell":
            quantity = held
            if quantity <= 0:
                return {"code": code, "side": side, "quantity": 0, "price": price,
                        "status": "rejected", "reason": "可卖数量为 0",
                        "ts": datetime.now().isoformat()}
        else:
            if code not in read_slot_codes(state):
                return {"code": code, "side": side, "quantity": 0, "price": price,
                        "status": "skipped", "reason": "未分档, 只监控不开仓",
                        "ts": datetime.now().isoformat()}
            cash = _available_cash(positions, self.capital)
            budget = slot_budget(self.capital, read_max_slots(state))
            quantity = size_buy_shares(price, budget, cash, lot=share_lot(code))
            if quantity <= 0:
                return {"code": code, "side": side, "quantity": 0, "price": price,
                        "status": "rejected", "reason": f"可用现金不足 ({cash:,.0f})",
                        "ts": datetime.now().isoformat()}

        amount = quantity * price

        # control.pause_buying 拦截
        if side == "buy" and state.get("control", {}).get("pause_buying"):
            self._note(state, "INFO", "买入被 CEO 控制台暂停",
                       message=f"{code} {quantity}股 @ {price:.2f}",
                       source="control")
            return {"code": code, "side": side, "quantity": quantity,
                    "price": price, "status": "paused_by_ceo"}

        # 下单 (dry-run = 模拟撮合写持仓; real = 真连券商)
        if self.dry_run:
            name = ""
            for p in positions:
                if p.get("code") == code:
                    name = str(p.get("name") or "")
                    break
            state["positions"] = apply_dry_run_fill(
                positions, code, side, quantity, price, name=name,
            )
            unit = "美元" if is_us_code(code) else "元"
            self._note(
                state, "INFO",
                f"[DRY-RUN] 成交 {side} {code} {quantity}股 @ {price:.2f}{unit}",
                source="trader",
            )
            return {"code": code, "side": side, "quantity": quantity,
                    "price": price, "amount": amount, "status": "dry_run",
                    "ts": datetime.now().isoformat()}

        # 真实下单 (本 CASE 内 live_trading/qmt_trader_v2)
        if is_tdx():
            return {"code": code, "side": side, "quantity": quantity,
                    "price": price, "status": "blocked",
                    "message": "通达信模式不能下单"}
        try:
            from live_trading.qmt_trader_v2 import QMTTraderV2
            trader = QMTTraderV2(enable_heartbeat=False)
            trader.connect()
            if side == "buy":
                order_id = trader.buy(code, quantity, price=price,
                                      strategy_name="live_loop")
            else:
                order_id = trader.sell(code, quantity, price=price,
                                       strategy_name="live_loop")
            trader.disconnect()

            if order_id:
                self._note(
                    state, "INFO", f"实盘下单成功 {side} {code}",
                    message=f"委托编号 {order_id}, {quantity}股 @ {price:.2f}",
                    source="trader",
                )
                return {"code": code, "side": side, "quantity": quantity,
                        "price": price, "amount": amount, "status": "submitted",
                        "order_id": order_id, "ts": datetime.now().isoformat()}
            else:
                self._note(state, "WARN", f"实盘下单失败 {code}", source="trader")
                return {"code": code, "side": side, "quantity": quantity,
                        "price": price, "status": "failed",
                        "ts": datetime.now().isoformat()}
        except Exception as e:
            self._note(state, "CRITICAL", f"下单异常 {code}",
                       message=str(e), source="trader")
            return {"code": code, "side": side, "status": "exception",
                    "reason": str(e), "ts": datetime.now().isoformat()}

    def _note(self, state: dict, level: str, title: str,
              message: str = "", source: str = "") -> None:
        """写进事件流，并走告警路由。连续相同标题不重复落盘。"""
        events = state.setdefault("events", [])
        last = events[-1] if events else {}
        if last.get("title") != title or last.get("level") != level:
            events.append({
                "ts": datetime.now().isoformat(timespec="seconds"),
                "level": level,
                "title": title,
                "message": message,
                "source": source,
            })
            state["events"] = events[-200:]
        self.alert.alert(level, title, message=message, source=source)

    def _note_cycle(self, state: dict, codes: List[str], signals: List[dict],
                    duration_ms: int) -> None:
        state.setdefault("events", []).append({
            "ts": datetime.now().isoformat(timespec="seconds"),
            "type": "loop_cycle",
            "market": session_market(codes),
            "signal_count": len(signals),
            "codes": [sig.get("code") for sig in signals if sig.get("code")],
            "duration_ms": duration_ms,
        })
        state["events"] = state["events"][-200:]

    def consume_force_clear(self) -> bool:
        """一键清仓: 股数大于 0 的 A 股和美股都按最新价卖掉。收盘也能清。"""
        state = self.state_store.load()
        if not (state.get("control") or {}).get("force_clear_all"):
            return False

        def price_of(code: str) -> float:
            tick = self.market.get_latest_tick(code)
            last = tick_last_price(tick)
            if last > 0:
                return last
            for pos in state.get("positions") or []:
                if pos.get("code") == code:
                    return float(pos.get("cur_price") or 0)
            return 0.0

        positions, orders, notes = flatten_open_positions(state.get("positions") or [], price_of)
        state["positions"] = positions
        state["orders"] = (state.get("orders") or []) + [
            {**order, "ts": datetime.now().isoformat(timespec="seconds")}
            for order in orders
        ]
        state["orders"] = state["orders"][-500:]
        for note in notes:
            self._note(state, note["level"], note["title"],
                       message=note.get("message") or "", source="control")
        if not notes:
            self._note(state, "INFO", "一键清仓: 没有股数大于 0 的持仓", source="control")
        state.setdefault("control", {})["force_clear_all"] = False
        today_pnl, today_pct = calc_today_pnl(positions, self.capital)
        state["today_pnl"] = today_pnl
        state["today_pnl_pct"] = today_pct
        self.state_store.save(state)
        return True

    # ------------------------------------------------------------------
    # 长跑模式
    # ------------------------------------------------------------------
    def run_forever(self, interval_seconds: int = 60):
        """每隔 N 秒跑一次, 直到 Ctrl+C"""
        self.alert.alert("INFO", "实盘主循环启动",
                         message=f"watch={self.watch_stocks}, "
                                 f"interval={interval_seconds}s, dry_run={self.dry_run}",
                         source="loop")
        try:
            while True:
                t0 = time.time()
                result = self.run_once()
                # 等到下一次触发
                elapsed = time.time() - t0
                if elapsed < interval_seconds:
                    time.sleep(interval_seconds - elapsed)
        except KeyboardInterrupt:
            self.alert.alert("INFO", "实盘主循环退出 (Ctrl+C)", source="loop")
            self.alert.shutdown()


# ============================================================
# CLI
# ============================================================

def main():
    import argparse
    parser = argparse.ArgumentParser(description="盘中全自动交易闭环")
    parser.add_argument("--stocks", default="600519.SH,513100.SH",
                        help="监控股票池, 逗号分隔")
    parser.add_argument("--capital", type=float, default=1_000_000)
    parser.add_argument("--interval", type=int, default=60,
                        help="循环间隔秒, 默认 60")
    parser.add_argument("--once", action="store_true", help="只跑一次")
    parser.add_argument("--state-file", default="outputs/live_state.json")
    args = parser.parse_args()

    stocks = [s.strip() for s in args.stocks.split(",") if s.strip()]
    loop = LiveTradingLoop(
        watch_stocks=stocks,
        capital=args.capital,
        state_file=args.state_file,
        dry_run=os.environ.get("TRADER_DRY_RUN", "1") == "1",
    )

    if args.once:
        result = loop.run_once()
        print(f"\n[完成] {result}")
        print(f"\nstate 落盘: {args.state_file}")
    else:
        loop.run_forever(args.interval)


if __name__ == "__main__":
    main()
