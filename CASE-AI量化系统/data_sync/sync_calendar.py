# -*- coding: utf-8 -*-
# Author：@跟陈博士学AI
"""
财经日历采集 - AkShare(百度财经日历) -> MySQL

采集全球重要经济事件（FOMC利率决议、CPI/PPI公布、PMI、非农就业等）
写入 trade_calendar_event 表

数据源：AkShare news_economic_baidu()
  返回列: 日期, 时间, 国家, 事件, 实际, 预期, 前值, 重要性

运行：python 6-财经日历采集.py
"""
import sys
import os
import math
import pandas as pd
import akshare as ak

from dragon_strategy.db_config import get_connection, execute_query

if sys.platform == 'win32' and hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

# 只保留这些国家的事件
COUNTRIES = {'中国', '美国', '欧元区', '日本', '英国'}

# 事件类型分类关键词
EVENT_TYPE_MAP = {
    'interest_rate': ['利率', 'FOMC', '加息', '降息', 'LPR', '基准利率', '联邦基金'],
    'inflation': ['CPI', 'PPI', '通胀', '物价'],
    'employment': ['就业', '非农', '失业率', 'ADP'],
    'pmi': ['PMI', '采购经理'],
    'gdp': ['GDP', '国内生产总值'],
    'trade': ['贸易', '进出口', '出口', '进口'],
    'monetary': ['M2', '货币供应', '社融', '信贷'],
    'housing': ['房价', '房屋'],
    'retail': ['零售', '消费'],
    'industry': ['工业', '产出', '产值'],
}


def classify_event(event_name):
    """根据事件名称分类"""
    for etype, keywords in EVENT_TYPE_MAP.items():
        for kw in keywords:
            if kw in event_name:
                return etype
    return 'other'


def _to_str(val):
    """将数值转为字符串，NaN 返回 None"""
    if val is None:
        return None
    if isinstance(val, float) and math.isnan(val):
        return None
    try:
        if pd.isna(val):
            return None
    except (TypeError, ValueError):
        pass
    s = str(val).strip()
    if not s or s.lower() == 'nan':
        return None
    return s


def _to_int(val, default=1):
    """将重要性转为整数；NaN/空值/无法解析时返回 default。

    注意：float('nan') 在 Python 中是真值，`int(x or 1)` 拦不住 NaN。
    """
    if val is None:
        return default
    if isinstance(val, float) and math.isnan(val):
        return default
    try:
        if pd.isna(val):
            return default
    except (TypeError, ValueError):
        pass
    if isinstance(val, str):
        s = val.strip()
        if not s or s.lower() == 'nan':
            return default
        mapping = {'高': 3, '中': 2, '低': 1, '重要': 3}
        if s in mapping:
            return mapping[s]
        val = s
    try:
        return int(float(val))
    except (TypeError, ValueError, OverflowError):
        return default


def fetch_and_save(on_progress=None, should_stop=None):
    """采集财经日历并写入数据库，覆盖前7天到后30天"""
    print("采集财经日历 (AkShare news_economic_baidu)...")

    today = pd.Timestamp.now().normalize()
    dates = pd.date_range(today - pd.Timedelta(days=7), today + pd.Timedelta(days=30))
    frames = []
    total = len(dates)
    for i, d in enumerate(dates):
        if should_stop and should_stop():
            break
        date_str = d.strftime('%Y%m%d')
        if on_progress:
            on_progress(current=i, total=total, message=f"日历 {date_str}")
        try:
            part = ak.news_economic_baidu(date=date_str)
            if part is not None and len(part) > 0:
                frames.append(part)
        except Exception:
            pass
    if not frames:
        print("  接口返回空")
        return 0

    df = pd.concat(frames, ignore_index=True)
    print("  原始数据: {} 条".format(len(df)))

    # 列名映射
    col_map = {}
    for col in df.columns:
        if '日期' in col:
            col_map['date'] = col
        elif '时间' in col:
            col_map['time'] = col
        elif '国家' in col or '地区' in col:
            col_map['country'] = col
        elif '事件' in col:
            col_map['event'] = col
        elif '实际' in col:
            col_map['actual'] = col
        elif '预期' in col:
            col_map['forecast'] = col
        elif '前值' in col:
            col_map['previous'] = col
        elif '重要' in col:
            col_map['importance'] = col

    if 'date' not in col_map or 'event' not in col_map:
        print("  列名无法识别: {}".format(list(df.columns)))
        return 0

    # 过滤国家
    if 'country' in col_map:
        df = df[df[col_map['country']].isin(COUNTRIES)]
        print("  过滤后({}): {} 条".format('/'.join(COUNTRIES), len(df)))

    conn = get_connection()
    cursor = conn.cursor()

    sql = """
        INSERT INTO trade_calendar_event
        (event_date, event_time, title, country, category,
         importance, forecast_value, actual_value, previous_value, source)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
        actual_value = COALESCE(VALUES(actual_value), actual_value),
        forecast_value = COALESCE(VALUES(forecast_value), forecast_value),
        previous_value = COALESCE(VALUES(previous_value), previous_value),
        importance = VALUES(importance)
    """

    count = 0
    for _, row in df.iterrows():
        event_date = row[col_map['date']]
        if pd.isna(event_date):
            continue
        if hasattr(event_date, 'strftime'):
            event_date_str = event_date.strftime('%Y-%m-%d')
        else:
            event_date_str = str(event_date)[:10]

        event_time = _to_str(row.get(col_map.get('time', ''), None))
        title = _to_str(row[col_map['event']])
        if not title:
            continue

        country = _to_str(row.get(col_map.get('country', ''), None)) or ''
        importance = _to_int(row.get(col_map.get('importance', ''), 1), default=1)
        category = classify_event(title)
        actual = _to_str(row.get(col_map.get('actual', ''), None))
        forecast = _to_str(row.get(col_map.get('forecast', ''), None))
        previous = _to_str(row.get(col_map.get('previous', ''), None))

        cursor.execute(sql, (
            event_date_str, event_time, title, country, category,
            importance, forecast, actual, previous, 'baidu_economic'
        ))
        count += 1

    conn.commit()
    cursor.close()
    conn.close()
    return count


def sync_calendar(on_progress=None, should_stop=None):
    """写 trade_calendar_event（前7后30天）。"""
    def progress(**kw):
        if on_progress:
            on_progress(**kw)

    count = fetch_and_save(on_progress=progress, should_stop=should_stop)
    progress(
        current=1, total=1, success=1 if count else 0, failed=0 if count else 1,
        rows_written=count, message=f"日历写入 {count} 行",
    )
    return {"success": 1 if count else 0, "failed": 0 if count else 1, "rows_written": count, "total": 1}


def main():
    print("=" * 60)
    print("财经日历采集 -> MySQL")
    print("=" * 60)

    count = fetch_and_save()
    print("\n写入/更新 {} 条事件".format(count))

    rows = execute_query("""
        SELECT country, COUNT(*) as cnt,
               MIN(event_date) as min_d, MAX(event_date) as max_d
        FROM trade_calendar_event
        GROUP BY country ORDER BY cnt DESC
    """)
    print("\ntrade_calendar_event 概况:")
    for r in rows:
        print("  {}: {} 条 ({} ~ {})".format(r['country'], r['cnt'], r['min_d'], r['max_d']))

    total = execute_query("SELECT COUNT(*) as c FROM trade_calendar_event")
    print("  总计: {} 条".format(total[0]['c']))

    print("\n" + "=" * 60)
    print("财经日历采集完成!")
    print("=" * 60)


if __name__ == '__main__':
    main()
