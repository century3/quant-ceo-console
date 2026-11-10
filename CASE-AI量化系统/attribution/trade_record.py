# -*- coding: utf-8 -*-
# Author：@跟陈博士学AI
# CASE-A: 交易记录导入与清洗
"""
TradeRecordLoader -- 交易记录加载与清洗

支持三种来源:
    1. miniQMT 实时查询 (xttrader.query_stock_trades)
    2. 模拟盘 / 实盘 live_state.json 的 orders 字段
    3. 券商导出的"历史成交"CSV 交割单 (中文表头, 含港股和美股)

输出标准化的 DataFrame, 字段:
    trade_date, ts, code, name, side, quantity, price, amount,
    commission, stamp_duty, total_cost, status

为什么要做这个?
    每家券商的交割单格式不一样, 标准化后的 DataFrame 才能喂给后续 Brinson / 归因模块.
"""

from __future__ import annotations
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple
import pandas as pd

THIS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = THIS_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT))


def _format_code(raw_code: str) -> str:
    """把券商导出的裸代码补全成 .SH / .SZ / .HK / .US。

    纯字母是美股 (COHR -> COHR.US)。5 位及以下纯数字是港股。
    """
    code = (raw_code or "").strip()
    if not code:
        return ""
    if "." in code:
        return code
    if code.isalpha():
        return f"{code.upper()}.US"
    if code.isdigit() and len(code) <= 5:
        return f"{code}.HK"
    if code.startswith(("5", "6")):
        return f"{code}.SH"
    if code.startswith(("0", "1", "3")):
        return f"{code}.SZ"
    return code


def _trade_date_from_ts(ts: str) -> str:
    """从订单 ts 取出成交日 YYYY-MM-DD.

    模拟盘历史回放写的是 '2026-04-01';
    也接受 '2026-04-01T14:59:00' / '2026-04-01 14:59:00'.
    """
    raw = (ts or "").strip()
    if not raw:
        raise ValueError("订单 ts 为空")
    if "T" in raw:
        return datetime.fromisoformat(raw.split("+")[0]).strftime("%Y-%m-%d")
    if len(raw) >= 10 and raw[4] == "-" and raw[7] == "-":
        return raw[:10]
    raise ValueError(f"订单 ts 不是日期: {raw}")


def load_from_state_json(state_file: str) -> pd.DataFrame:
    """
    从 live_state.json 加载交易记录

    state.orders 字段长这样:
        [{"ts", "code", "side", "quantity", "price", "status", ...}, ...]
    """
    p = Path(state_file)
    if not p.exists():
        return pd.DataFrame()
    state = json.loads(p.read_text(encoding="utf-8"))
    orders = state.get("orders", [])

    # 只保留成功的订单
    valid = [o for o in orders if o.get("status") in ("submitted", "dry_run")]
    if not valid:
        return pd.DataFrame()

    # 订单本身通常没有 name, 用持仓表补中文名
    name_map = {}
    for pos in state.get("positions") or []:
        code = pos.get("code")
        if code:
            name_map[code] = pos.get("name") or ""

    rows = []
    for o in valid:
        ts = o.get("ts", "")
        code = o.get("code")
        rows.append({
            "trade_date": _trade_date_from_ts(ts),
            "ts":         ts,
            "code":       code,
            "name":       o.get("name") or name_map.get(code, ""),
            "side":       o.get("side"),
            "quantity":   int(o.get("quantity", 0)),
            "price":      float(o.get("price", 0)),
            "amount":     float(o.get("amount", o.get("price", 0) * o.get("quantity", 0))),
            "status":     o.get("status"),
            "order_id":   o.get("order_id", ""),
        })

    df = pd.DataFrame(rows)
    return df


def load_from_csv(csv_path: str) -> pd.DataFrame:
    """
    从券商导出的"历史成交"CSV 加载交易记录 (中文表头).

    支持的字段 (常见券商通用导出格式):
        证券名称, 证券代码, 成交日期, 成交时间, 买卖方向, 成交数量, 成交价格,
        成交金额, 佣金, 净佣金, 过户费, 印花税, 结算费, 成交编号, 股东代码, 备注

    特殊处理:
        - 字段值常带 \\t / 空格 缩进 (Excel 防数字串变科学计数), 全部 strip
        - 港股没有"成交时间", 用 "00:00:00" 占位
        - 代码自动补 .SH / .SZ / .HK / .US 后缀
        - 中文方向 (买入/卖出) 转英文 (buy/sell)
        - 真实手续费用券商导出的, 不重新估算 (跟 add_costs 不同)
    """
    p = Path(csv_path)
    if not p.exists():
        return pd.DataFrame()
    # 券商导出常见编码: utf-8-sig (带 BOM) / gbk
    try:
        df = pd.read_csv(p, dtype=str, encoding="utf-8-sig")
    except UnicodeDecodeError:
        df = pd.read_csv(p, dtype=str, encoding="gbk")
    if df.empty:
        return pd.DataFrame()

    # 全字段去 \t / 空格
    for col in df.columns:
        df[col] = df[col].astype(str).str.replace("\t", "", regex=False).str.strip()

    rows = []
    for _, r in df.iterrows():
        side_cn = r.get("买卖方向", "")
        side = "buy" if "买" in side_cn else ("sell" if "卖" in side_cn else side_cn)
        trade_date = r.get("成交日期", "")
        time_str = r.get("成交时间", "") or "00:00:00"
        ts = f"{trade_date}T{time_str}" if trade_date else time_str

        def _f(key, default=0.0):
            v = r.get(key, "")
            try:
                return float(v) if v else default
            except ValueError:
                return default

        commission   = _f("佣金")
        stamp_duty   = _f("印花税")
        transfer_fee = _f("过户费")
        settle_fee   = _f("结算费")

        rows.append({
            "trade_date": trade_date,
            "ts":         ts,
            "code":       _format_code(r.get("证券代码", "")),
            "name":       r.get("证券名称", ""),
            "side":       side,
            "quantity":   int(_f("成交数量")),
            "price":      _f("成交价格"),
            "amount":     _f("成交金额"),
            "commission": commission,
            "stamp_duty": stamp_duty,
            # 过户费 + 结算费 一并算进总成本, 大部分场景为 0
            "transfer_fee": transfer_fee,
            "settle_fee":   settle_fee,
            "total_cost":   commission + stamp_duty + transfer_fee + settle_fee,
            "status":     "filled",
            "order_id":   r.get("成交编号", ""),
            "remark":     r.get("备注", ""),
        })

    out = pd.DataFrame(rows)
    # 按时间倒序 -> 正序, 跟券商导出习惯反一下, 时间轴从早到晚
    if "trade_date" in out.columns and not out.empty:
        out = out.sort_values(["trade_date", "ts"]).reset_index(drop=True)
    return out


def load_from_miniqmt(qmt_path: str, account_id: str) -> pd.DataFrame:
    """
    从 miniQMT 实时拉当日成交 (用于真实实盘后归因)
    """
    from xtquant.xttrader import XtQuantTrader
    from xtquant.xttype import StockAccount
    import time

    trader = XtQuantTrader(qmt_path, int(time.time()))
    trader.start()
    if trader.connect() != 0:
        raise RuntimeError("miniQMT 连接失败")

    account = StockAccount(account_id)
    trader.subscribe(account)

    trades = trader.query_stock_trades(account)
    trader.stop()

    if not trades:
        return pd.DataFrame()

    rows = []
    for t in trades:
        rows.append({
            "trade_date": str(t.traded_time)[:10] if hasattr(t, "traded_time") else "",
            "ts":         str(t.traded_time),
            "code":       t.stock_code,
            "name":       "",
            "side":       "buy" if t.order_type == 23 else "sell",
            "quantity":   int(t.traded_volume),
            "price":      float(t.traded_price),
            "amount":     float(t.traded_amount),
            "status":     "filled",
            "order_id":   getattr(t, "order_id", ""),
        })
    return pd.DataFrame(rows)


def add_costs(df: pd.DataFrame,
              commission_rate: float = 0.00015,
              commission_min: float = 5.0,
              stamp_duty_rate: float = 0.0005) -> pd.DataFrame:
    """给交易记录加上估算的手续费 (复用 21 章的成本模型)"""
    if df.empty:
        return df
    df = df.copy()

    def _commission(row):
        # 这套费率是 A 股。美股不套最低 5 元佣金。
        if str(row.get("code") or "").endswith(".US"):
            return 0.0
        return max(row["amount"] * commission_rate, commission_min)

    def _stamp_duty(row):
        if row["side"] != "sell":
            return 0.0
        code = str(row.get("code") or "")
        if code.endswith(".US"):
            return 0.0
        # ETF 免征
        is_etf = (code.endswith(".SH") and code.startswith(("51", "58"))) \
                 or (code.endswith(".SZ") and code.startswith(("15", "16")))
        return 0.0 if is_etf else row["amount"] * stamp_duty_rate

    df["commission"] = df.apply(_commission, axis=1)
    df["stamp_duty"] = df.apply(_stamp_duty, axis=1)
    df["total_cost"] = df["commission"] + df["stamp_duty"]
    return df


# ============================================================
# Demo
# ============================================================

# 默认数据源 (按优先级排列):
#   1. 真实交割单 CSV  -- 课程内置示例 (用户自有交割单)
#   2. 模拟盘 state.json -- AI 量化系统跑出来的订单
DATA_DIR = PROJECT_ROOT / "data"
DEFAULT_SIM_STATE = PROJECT_ROOT / "outputs" / "live_state.json"


def parse_export_range(name: str) -> Tuple[str, str]:
    """历史成交_<账户>_YYMMDD-YYMMDD.csv -> (起始日, 截止日)

    这张对账单的评估窗口以文件名为准, 不是最后一笔成交日.
    未平仓按截止日收盘盯市; 截止日之后的买卖不进这张单.
    """
    stem = Path(name).stem
    tail = stem.rsplit("_", 1)[-1]
    a, b = tail.split("-")
    if len(a) != 6 or len(b) != 6 or not a.isdigit() or not b.isdigit():
        raise ValueError(
            f"交割单文件名必须是 历史成交_<账户>_YYMMDD-YYMMDD.csv, 收到 {name}"
        )
    start = f"20{a[0:2]}-{a[2:4]}-{a[4:6]}"
    end = f"20{b[0:2]}-{b[2:4]}-{b[4:6]}"
    return start, end


def list_available_csvs() -> list:
    """列出 data 目录下所有"历史成交_*.csv"交割单 (按文件名倒序, 最新在前)

    Returns:
        [{
            "name":   "历史成交_cy_260301-260507.csv",
            "path":   "<absolute path>",
            "label":  "2026-03-01 ~ 2026-05-07",
            "start":  "2026-03-01",
            "end":    "2026-05-07",
        }, ...]
    """
    if not DATA_DIR.exists():
        return []
    out = []
    for p in sorted(DATA_DIR.glob("历史成交_*.csv"), reverse=True):
        start, end = parse_export_range(p.name)
        out.append({
            "name": p.name,
            "path": str(p),
            "label": f"{start} ~ {end}",
            "start": start,
            "end": end,
        })
    return out


def _resolve_default_csv() -> Path:
    """挑选默认 CSV: data 目录下"历史成交_*.csv"中文件名最大的 (最新区间)"""
    candidates = list_available_csvs()
    if candidates:
        return Path(candidates[0]["path"])
    # 兜底 (data 目录为空) -- 返回一个不存在的路径, 上层 demo_csv 会报 [ERR] 而不是崩
    return DATA_DIR / "历史成交_cy_260101-260325.csv"


DEFAULT_CSV_PATH = _resolve_default_csv()


def _print_report(df: pd.DataFrame, source_label: str) -> None:
    """统一的展示格式: 明细 + 成本汇总. 兼容 CSV (含真实手续费) / state.json (估算)."""
    print(f"[原始交易记录] {len(df)} 笔  来源: {source_label}")

    show_cols = ["trade_date", "code", "name", "side", "quantity",
                 "price", "amount", "commission", "stamp_duty"]
    show_cols = [c for c in show_cols if c in df.columns]
    print(df[show_cols].to_string(index=False))

    if "total_cost" not in df.columns:
        df = add_costs(df)
    print(f"\n[成本汇总]")
    print(f"  佣金合计    : {df['commission'].sum():>10,.2f} 元")
    print(f"  印花税合计  : {df['stamp_duty'].sum():>10,.2f} 元")
    if "transfer_fee" in df.columns:
        print(f"  过户费合计  : {df['transfer_fee'].sum():>10,.2f} 元")
    print(f"  总成本     : {df['total_cost'].sum():>10,.2f} 元")
    print(f"  成交金额    : {df['amount'].sum():>14,.2f} 元")
    if df['amount'].sum() > 0:
        print(f"  总成本/成交 : {df['total_cost'].sum() / df['amount'].sum() * 10000:.1f} bps")


def demo_csv(csv_path: Optional[Path] = None) -> None:
    """从 CSV 交割单加载 (真实手续费, 用于教学演示真实数据接入)."""
    p = Path(csv_path) if csv_path else DEFAULT_CSV_PATH
    print(f"\n{'='*70}")
    print(f"  CASE-A 交易记录加载 demo  -- 数据源: CSV 真实交割单")
    print(f"  文件路径: {p}")
    print(f"{'='*70}\n")

    if not p.exists():
        print(f"  [ERR] CSV 不存在: {p}")
        return

    df = load_from_csv(str(p))
    if df.empty:
        print("  [WARN] CSV 解析后没有数据")
        return
    _print_report(df, source_label=f"CSV ({p.name})")


def demo_sim(state_file: Optional[Path] = None) -> None:
    """从模拟盘 / 实盘 state.json 加载 (券商佣金按 21 章成本模型估算)."""
    p = Path(state_file) if state_file else DEFAULT_SIM_STATE
    print(f"\n{'='*70}")
    print(f"  CASE-A 交易记录加载 demo  -- 数据源: 模拟盘 state.json")
    print(f"  文件路径: {p}")
    print(f"{'='*70}\n")

    df = load_from_state_json(str(p))
    if df.empty:
        print("  [WARN] state.json 不存在或没有 orders 字段, 跳过")
        return
    df = add_costs(df)
    _print_report(df, source_label=f"模拟盘 ({p.name})")


def demo() -> None:
    """同时演示两个数据源, 让学员对比 mock / 真实交割单 / 模拟盘三种来源."""
    demo_csv()
    demo_sim()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="交易记录加载 demo")
    parser.add_argument("--source", choices=["csv", "sim", "all"], default="all",
                        help="数据源: csv (真实交割单) / sim (模拟盘) / all (默认, 都跑)")
    parser.add_argument("--csv", type=str, default=None, help="自定义 CSV 路径")
    parser.add_argument("--state", type=str, default=None, help="自定义 state.json 路径")
    args = parser.parse_args()

    if args.source in ("csv", "all"):
        demo_csv(Path(args.csv) if args.csv else None)
    if args.source in ("sim", "all"):
        demo_sim(Path(args.state) if args.state else None)
