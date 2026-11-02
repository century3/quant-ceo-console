# -*- coding: utf-8 -*-
# Author：@跟陈博士学AI
# 25-AI量化系统 回测数据加载层
"""
backtest_data -- 回测页 / score_strategies 用的日 K 加载工具

数据源 (唯一口径): 前复权日 K
    WUCAI_QMT=qmt      -> QMT HTTP
    WUCAI_QMT=miniqmt  -> xtquant.xtdata
    WUCAI_QMT=tdx      -> MySQL trade_stock_daily（通达信入库，开高低收已是前复权）

qmt/miniqmt 直拉券商；tdx 读已经灌好的 MySQL。

返回的 DataFrame 统一格式:
    - 索引: pandas DatetimeIndex (日)
    - 列:   open / high / low / close / volume (float)
    - 升序排序

设计:
    - 同一只股票在同一进程内做了缓存 (减少重复拉数)
"""

from __future__ import annotations
import os
import warnings
from functools import lru_cache
from pathlib import Path
from typing import Optional

import pandas as pd

from lib.qmt_backend import is_miniqmt, is_tdx, label as tool_label

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 回测统一前复权
DIVIDEND_TYPE = "front"


# ============================================================
# MySQL 配置 (仅股票简称查询用)
# ============================================================

def _db_config() -> dict:
    """读 .env 里的 MySQL 配置 -- 必须用 dotenv 提前 load (app.py 已做)"""
    return {
        "host":     os.environ.get("WUCAI_SQL_HOST", "localhost"),
        "user":     os.environ.get("WUCAI_SQL_USERNAME", "root"),
        "password": os.environ.get("WUCAI_SQL_PASSWORD", ""),
        "database": os.environ.get("WUCAI_SQL_DB", "wucai_trade"),
        "port":     int(os.environ.get("WUCAI_SQL_PORT", "3306")),
        "charset":  "utf8mb4",
    }


def mysql_available() -> bool:
    """快速检查 MySQL 是否可连 (回测页 ping 用; 简称查询依赖它)"""
    try:
        import pymysql
        cfg = _db_config()
        conn = pymysql.connect(connect_timeout=2, **cfg)
        conn.close()
        return True
    except Exception:
        return False


# ============================================================
# 加载日 K -- QMT / miniQMT (前复权)
# ============================================================

def _load_from_qmt(stock_code: str,
                   start_date: Optional[str] = None,
                   end_date: Optional[str] = None) -> pd.DataFrame:
    """按 WUCAI_QMT 从 QMT HTTP 或 miniQMT 拉前复权日 K。"""
    sd = (start_date or "20200101").replace("-", "")[:8]
    ed = (end_date or "20991231").replace("-", "")[:8]
    fields = ["open", "high", "low", "close", "volume"]

    if is_miniqmt():
        from xtquant import xtdata
        xtdata.connect()
        try:
            xtdata.download_history_data(stock_code, period="1d", start_time=sd, end_time=ed)
        except Exception:
            pass
        md = xtdata.get_market_data(
            field_list=fields, stock_list=[stock_code],
            period="1d", start_time=sd, end_time=ed,
            dividend_type=DIVIDEND_TYPE, fill_data=True,
        )
        if not md or "close" not in md or stock_code not in md["close"].index:
            raise ValueError(f"无数据: {stock_code}")
        out = pd.DataFrame({f: md[f].loc[stock_code] for f in fields})
    else:
        from lib.qmt_backend import current
        if current() == 'qmt_shared':
            from lib.qmt_shared import shared_http
            body = shared_http('/api/data/market_data', {
                'stock_code': stock_code, 'fields': ','.join(fields),
                'period': '1d', 'start_time': sd, 'end_time': ed,
                'count': -1, 'dividend_type': DIVIDEND_TYPE,
            })
            data = (body or {}).get('data') or {}
            close_map = data.get('close', {})
            if not close_map:
                raise ValueError(f"无数据: {stock_code}")
            dates = sorted(close_map.keys())
            out = pd.DataFrame(
                {f: [data.get(f, {}).get(d) for d in dates] for f in fields},
                index=dates,
            )
            out.index = pd.to_datetime(out.index, format="%Y%m%d", errors="coerce")
            out = out.dropna(subset=["close"])
            out = out[out["close"] > 0]
            if out.empty:
                raise ValueError(f"QMT 数据为空: {stock_code}")
            out.sort_index(inplace=True)
            return out
        import requests
        base = os.environ.get("QMT_BASE_URL", "http://127.0.0.1:15588").rstrip("/")
        headers = {
            "Content-Type": "application/json",
            "X-Token": os.environ.get("QMT_TOKEN", "learning_ai_with_dr_chen"),
        }
        r = requests.post(
            f"{base}/api/data/market_data",
            headers=headers,
            json={
                "stock_code": stock_code,
                "fields": ",".join(fields),
                "period": "1d",
                "start_time": sd,
                "end_time": ed,
                "count": -1,
                "dividend_type": DIVIDEND_TYPE,
            },
            timeout=120,
            proxies={"http": None, "https": None},
        )
        r.raise_for_status()
        data = (r.json() or {}).get("data") or {}
        close_map = data.get("close", {})
        if not close_map:
            raise ValueError(f"无数据: {stock_code}")
        dates = sorted(close_map.keys())
        out = pd.DataFrame(
            {f: [data.get(f, {}).get(d) for d in dates] for f in fields},
            index=dates,
        )

    out.index = pd.to_datetime(out.index, format="%Y%m%d", errors="coerce")
    out = out.dropna(subset=["close"])
    out = out[out["close"] > 0]
    if out.empty:
        raise ValueError(f"QMT 数据为空: {stock_code}")
    out.sort_index(inplace=True)
    return out


def _load_from_mysql(stock_code: str,
                     start_date: Optional[str] = None,
                     end_date: Optional[str] = None,
                     table: str = "trade_stock_daily") -> pd.DataFrame:
    """从日 K 表读行情。A 股 trade_stock_daily，美股 trade_stock_us_daily。"""
    if table not in ("trade_stock_daily", "trade_stock_us_daily"):
        raise ValueError(f"不支持的日K表: {table}")
    import pymysql
    sd = (start_date or "20200101").replace("-", "")[:8]
    ed = (end_date or "20991231").replace("-", "")[:8]
    start = f"{sd[:4]}-{sd[4:6]}-{sd[6:8]}"
    end = f"{ed[:4]}-{ed[4:6]}-{ed[6:8]}"
    conn = pymysql.connect(connect_timeout=5, **_db_config())
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            df = pd.read_sql(
                "SELECT trade_date, open_price AS open, high_price AS high, "
                "low_price AS low, close_price AS close, volume "
                f"FROM {table} "
                "WHERE stock_code=%s AND trade_date>=%s AND trade_date<=%s "
                "ORDER BY trade_date",
                conn, params=(stock_code, start, end),
            )
    finally:
        conn.close()
    if df is None or df.empty:
        raise ValueError(f"MySQL 无数据: {stock_code} {start}~{end}")
    df.index = pd.to_datetime(df["trade_date"])
    df = df.drop(columns=["trade_date"])
    df = df.dropna(subset=["close"])
    df = df[df["close"] > 0]
    if df.empty:
        raise ValueError(f"MySQL 数据为空: {stock_code}")
    df.sort_index(inplace=True)
    return df


# ============================================================
# 对外: 统一加载入口 (带进程级缓存)
# ============================================================

def _cache_key(stock_code: str, start: Optional[str], end: Optional[str]) -> tuple:
    # 含复权口径, 避免改口径后命中旧缓存
    return (stock_code, start or "", end or "", DIVIDEND_TYPE)


_kline_cache: dict = {}


def load_daily_kline(stock_code: str,
                     start_date: Optional[str] = None,
                     end_date: Optional[str] = None,
                     use_cache: bool = True) -> pd.DataFrame:
    """加载日 K (统一入口, 前复权)

    Args:
        stock_code:  '600519.SH' / '002432.SZ'
        start_date:  'YYYY-MM-DD' 或 'YYYYMMDD' 或 None
        end_date:    同上
        use_cache:   False 时每次重拉 (弹窗看现价)

    Returns:
        DataFrame, 索引日期, 列 open/high/low/close/volume
    """
    code = (stock_code or "").strip()
    if not code:
        raise ValueError("stock_code 不能为空")
    s = (start_date or "").replace("/", "-")
    e = (end_date or "").replace("/", "-")
    key = _cache_key(code, s, e)
    if use_cache and key in _kline_cache:
        return _kline_cache[key].copy()

    try:
        if code.upper().endswith(".US"):
            df = _load_from_mysql(code, s or None, e or None, table="trade_stock_us_daily")
        elif is_tdx():
            df = _load_from_mysql(code, s or None, e or None)
        else:
            df = _load_from_qmt(code, s or None, e or None)
    except Exception as err:
        raise RuntimeError(
            f"加载 {code} 失败 ({tool_label()}, dividend_type={DIVIDEND_TYPE}): {err}"
        ) from err

    _kline_cache[key] = df
    return df.copy()


def clear_kline_cache():
    """手动清缓存 (一般不用调; 进程级常驻)"""
    _kline_cache.clear()


# ============================================================
# 简单的"股票名"查询 (回测页 / 实盘待入场展示)
# 映射表: wucai_trade.trade_stock_status (stock_code -> stock_name)
# ============================================================

@lru_cache(maxsize=512)
def get_stock_name(stock_code: str) -> str:
    """从 trade_stock_status 取股票中文简称, 取不到返 code"""
    try:
        import pymysql
        conn = pymysql.connect(connect_timeout=2, **_db_config())
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT stock_name FROM trade_stock_status WHERE stock_code=%s LIMIT 1",
                (stock_code,)
            )
            row = cursor.fetchone()
            cursor.close()
        finally:
            conn.close()
        if row and row[0]:
            return str(row[0]).strip()
    except Exception:
        pass
    return stock_code


def get_stock_names(stock_codes: list[str]) -> dict[str, str]:
    """批量取中文简称; 查不到的回落为 code 本身"""
    codes = list(dict.fromkeys(c for c in stock_codes if c))
    if not codes:
        return {}
    out: dict[str, str] = {c: c for c in codes}
    try:
        import pymysql
        conn = pymysql.connect(connect_timeout=2, **_db_config())
        try:
            cursor = conn.cursor()
            placeholders = ",".join(["%s"] * len(codes))
            cursor.execute(
                f"SELECT stock_code, stock_name FROM trade_stock_status "
                f"WHERE stock_code IN ({placeholders})",
                codes,
            )
            for code, name in cursor.fetchall() or []:
                if code and name:
                    out[str(code)] = str(name).strip()
            cursor.close()
        finally:
            conn.close()
    except Exception:
        return {c: get_stock_name(c) for c in codes}
    return out


def search_stocks(keyword: str, limit: int = 20) -> list[dict]:
    """按代码或中文名模糊搜索 trade_stock_status.

    返回: [{"code": "603986.SH", "name": "兆易创新"}, ...]
    匹配优先级: 代码精确 > 名称精确 > 代码前缀 > 名称包含.
    """
    q = (keyword or "").strip()
    if not q:
        return []
    limit = max(1, min(int(limit or 20), 50))
    like = f"%{q}%"
    prefix = f"{q}%"
    try:
        import pymysql
        conn = pymysql.connect(connect_timeout=2, **_db_config())
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT stock_code, stock_name,
                       CASE
                         WHEN UPPER(stock_code) = UPPER(%s) THEN 0
                         WHEN stock_name = %s THEN 1
                         WHEN UPPER(stock_code) LIKE UPPER(%s) THEN 2
                         WHEN stock_name LIKE %s THEN 3
                         ELSE 4
                       END AS rank_score
                FROM trade_stock_status
                WHERE UPPER(stock_code) LIKE UPPER(%s) OR stock_name LIKE %s
                ORDER BY rank_score ASC, stock_code ASC
                LIMIT %s
                """,
                (q, q, prefix, like, like, like, limit),
            )
            rows = cursor.fetchall() or []
            cursor.close()
        finally:
            conn.close()
        out = []
        for code, name, *_ in rows:
            if not code:
                continue
            out.append({"code": str(code), "name": str(name or "").strip()})
        return out
    except Exception:
        return []


def format_stock_label(code: str, name: str = "") -> str:
    """展示用: 名称(代码); 没有名称时只返回代码"""
    c = (code or "").strip()
    n = (name or "").strip()
    if not c:
        return n
    if n and n != c:
        return f"{n}({c})"
    return c


def resolve_stock_query(raw: str) -> tuple[str, str]:
    """把用户输入解析成 (code, name).

    - 展示格式 名称(代码) / 名称（代码）-> 直接取括号内代码
    - 已是代码 (含 .SH/.SZ/.BJ 或 6 位数字) -> 直接规范化并查名
    - 中文名 / 模糊词 -> search_stocks, 唯一命中或精确名称命中则采用
    - 解析失败 -> ("", "")
    """
    import re

    s = (raw or "").strip().replace(" ", "")
    if not s:
        return "", ""

    # 名称(600519.SH) / 名称(COHR.US)
    m = re.fullmatch(
        r"(.+?)[\(（]([0-9]{6}\.(?:SH|SZ|BJ)|[A-Z][A-Z0-9]*(-[A-Z0-9]+)?\.US)[\)）]",
        s, flags=re.IGNORECASE,
    )
    if m:
        name_part = m.group(1).strip()
        code = m.group(2).upper()
        db_name = get_stock_name(code)
        return code, (db_name if db_name != code else name_part)

    upper = s.upper()
    if re.fullmatch(r"[A-Z][A-Z0-9]*(-[A-Z0-9]+)?(\.US)?", upper) and not upper.endswith((".SH", ".SZ", ".BJ")):
        code = upper if upper.endswith(".US") else f"{upper}.US"
        name = get_stock_name(code)
        return code, ("" if name == code else name)

    looks_like_code = (
        upper.endswith((".SH", ".SZ", ".BJ"))
        or (s.isdigit() and len(s) == 6)
    )
    if looks_like_code:
        if s.isdigit() and len(s) == 6:
            if s.startswith(("5", "6", "9")):
                code = f"{s}.SH"
            elif s.startswith(("0", "1", "2", "3")):
                code = f"{s}.SZ"
            else:
                code = f"{s}.SH"
        else:
            code = upper
        name = get_stock_name(code)
        return code, ("" if name == code else name)

    hits = search_stocks(s, limit=10)
    if not hits:
        return "", ""
    exact = [h for h in hits if h.get("name") == s]
    if len(exact) == 1:
        return exact[0]["code"], exact[0]["name"]
    if len(hits) == 1:
        return hits[0]["code"], hits[0]["name"]
    return "", ""
