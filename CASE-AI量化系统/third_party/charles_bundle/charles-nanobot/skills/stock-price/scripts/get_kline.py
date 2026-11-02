#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
K 线数据获取脚本。价格一律前复权（dividend_type=front），
与第 4 课 QMT / 通达信采集、系统回测同一口径。

通过环境变量 WUCAI_QMT 切换数据源（没设置就默认 QMT HTTP）：
  WUCAI_QMT=miniqmt → xtquant
  WUCAI_QMT=qmt     → QMT HTTP 桥
  WUCAI_QMT=tdx     → MySQL trade_stock_daily（通达信入库，入库时已前复权）

输出 JSON，供 LLM / 策略脚本消费。
Author：@跟陈博士学AI
"""

import os
import sys
import json
from pathlib import Path

import pandas as pd

# 与系统同一套开关，避免本脚本再拆一套 WUCAI_QMT 解析
_CASE_ROOT = Path(__file__).resolve().parents[6]
if str(_CASE_ROOT) not in sys.path:
    sys.path.insert(0, str(_CASE_ROOT))
from lib.qmt_backend import is_miniqmt, is_tdx


def _fetch_xtquant(stock_code, period, start_date, end_date, count):
    """miniQMT 后端：xtquant"""
    from xtquant import xtdata
    xtdata.connect()
    # 增量下载历史数据
    xtdata.download_history_data(
        stock_code,
        period=period,
        start_time=start_date if start_date else '19900101',
        end_time=end_date if end_date else '',
        incrementally=True
    )
    data = xtdata.get_market_data_ex(
        field_list=['open', 'high', 'low', 'close', 'volume', 'amount'],
        stock_list=[stock_code],
        period=period,
        start_time=start_date,
        end_time=end_date,
        count=count,
        dividend_type='front',
    )
    if not data or stock_code not in data:
        return None
    return data[stock_code]   # DataFrame，含 time(ms) 列


def _fetch_qmt_http(stock_code, period, start_date, end_date, count):
    """QMT HTTP 后端：直接 requests 调本机 QMT_Server_API.py 的 /api/data/market_data_ex"""
    import requests

    def _key_to_ms(key):
        """Server 返回的时间键可能是 'YYYYMMDD'/'YYYYMMDDHHmmSS' 字符串或 ms 整数，统一转 ms。"""
        s = str(key)
        if s.isdigit():
            if len(s) == 8:     ts = pd.to_datetime(s, format='%Y%m%d')        # 日线
            elif len(s) == 14:  ts = pd.to_datetime(s, format='%Y%m%d%H%M%S')  # 分钟线
            elif len(s) >= 13:  return int(s)                                  # 已是 ms
            else:               ts = pd.to_datetime(s, errors='coerce')
        else:
            ts = pd.to_datetime(s, errors='coerce')
        return int(ts.value // 10**6) if ts is not pd.NaT else 0

    from lib.qmt_backend import current
    payload = {
        "stock_code": stock_code,
        "fields": "open,high,low,close,volume,amount",
        "period": period,
        "start_time": start_date,
        "end_time": end_date,
        "count": count,
        "dividend_type": "front",
    }
    if current() == 'qmt_shared':
        from lib.qmt_shared import shared_http
        body = shared_http('/api/data/market_data_ex', payload, timeout=30)
    else:
        base_url = os.getenv("QMT_BASE_URL", "http://127.0.0.1:15588").rstrip("/")
        token = os.getenv("QMT_TOKEN", "learning_ai_with_dr_chen")
        resp = requests.post(
            f"{base_url}/api/data/market_data_ex",
            headers={"Content-Type": "application/json", "X-Token": token},
            json=payload,
            timeout=30,
            # QMT 在本机，绝不能走系统代理（学员开 VPN/Clash 时会被劫持）
            proxies={"http": None, "https": None},
        )
        resp.raise_for_status()
        body = resp.json()
    if isinstance(body, dict) and body.get("error"):
        raise RuntimeError(body["error"])
    # Server 返回 {data: {stock: {field: {'YYYYMMDD': value, ...}}}}（DataFrame 已 to_dict）
    per_stock = (body.get("data") or {}).get(stock_code, {})
    if not per_stock:
        return None
    keys = sorted({k for fld in per_stock.values() for k in fld.keys()})
    rows = []
    for k in keys:
        row = {"time": _key_to_ms(k)}
        for fld in ("open", "high", "low", "close", "volume", "amount"):
            if fld in per_stock and k in per_stock[fld]:
                row[fld] = per_stock[fld][k]
        rows.append(row)
    return pd.DataFrame(rows)   # 含 time(ms) 列，与 xtquant 分支一致


def get_kline_data(stock_code, period='1d', start_date='', end_date='', count=100):
    """
    获取 K 线数据（自动按 WUCAI_QMT 选后端）

    Args:
        stock_code: 股票代码，如 '000001.SZ'
        period: 周期，支持 '1m','5m','15m','30m','1h','1d','1w','1mon'
        start_date: 开始日期 'YYYYMMDD'
        end_date: 结束日期 'YYYYMMDD'
        count: 获取条数
    """
    try:
        if is_tdx():
            from lib.backtest_data import load_daily_kline
            df = load_daily_kline(stock_code, start_date or None, end_date or None)
            if count and len(df) > count:
                df = df.tail(count)
        elif is_miniqmt():
            df = _fetch_xtquant(stock_code, period, start_date, end_date, count)
        else:
            df = _fetch_qmt_http(stock_code, period, start_date, end_date, count)

        if df is None or len(df) == 0:
            return {"error": f"{stock_code} 无数据"}

        # 数据处理（两后端统一：都带 time(ms) 列）
        if 'time' in df.columns:
            df['time'] = pd.to_datetime(df['time'], unit='ms')
            df['date'] = df['time'].dt.strftime('%Y-%m-%d')
            df['datetime'] = df['time'].dt.strftime('%Y-%m-%d %H:%M:%S')
        else:
            df.index = pd.to_datetime(df.index)
            df['date'] = df.index.strftime('%Y-%m-%d')
            df['datetime'] = df.index.strftime('%Y-%m-%d %H:%M:%S')

        for col in ['open', 'high', 'low', 'close']:
            if col in df.columns:
                df[col] = df[col].round(3)

        records = [{"date": r.get("date", ""), "open": float(r.get("open", 0)),
                    "high": float(r.get("high", 0)), "low": float(r.get("low", 0)),
                    "close": float(r.get("close", 0)), "volume": int(r.get("volume", 0)),
                    "amount": float(r.get("amount", 0))}
                   for r in df.to_dict('records')]

        return {
            "stock_code": stock_code,
            "period": period,
            "data_count": len(records),
            "data": records[-50:] if len(records) > 50 else records
        }
    except Exception as e:
        return {"error": f"获取失败: {str(e)}"}


def main():
    if len(sys.argv) < 2:
        print(json.dumps({"error": "请提供股票代码"}, ensure_ascii=False))
        sys.exit(1)
    stock_code = sys.argv[1]
    period = sys.argv[2] if len(sys.argv) > 2 else '1d'
    count = int(sys.argv[3]) if len(sys.argv) > 3 else 100
    result = get_kline_data(stock_code, period=period, count=count)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
