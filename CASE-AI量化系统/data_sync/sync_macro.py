# -*- coding: utf-8 -*-
# Author：@跟陈博士学AI
"""
宏观经济数据采集 - 采集宏观经济指标存入MySQL

指标分组:
  通胀指标: CPI同比、PPI同比
  景气指标: PMI(制造业)
  流动性:   M2同比增速、社融规模增量
  利率:     LPR(1年/5年)、10年期国债收益率(中/美)

数据源(AkShare):
  - CPI: macro_china_cpi()
  - PPI: macro_china_ppi()
  - PMI: macro_china_pmi()
  - M2:  macro_china_supply_of_money()
  - 社融: 商务部接口直接请求(同 macro_china_shrzgm 数据源)
  - LPR: macro_china_lpr()
  - 国债: bond_zh_us_rate()

常见问题 - 社融接口SSL握手失败(SSLV3_ALERT_HANDSHAKE_FAILURE):
  商务部 data.mofcom.gov.cn 服务器较旧, 仅支持 legacy 加密套件,
  OpenSSL 3 默认安全级别(SECLEVEL=2)会直接拒绝握手。
  本脚本自带 _TLSAdapter(SECLEVEL=1)直接请求, 不依赖 akshare 版本。
  若仍失败请检查: 1) 关闭代理/VPN全局模式后重试; 2) 升级 akshare。

运行: python 3-宏观数据采集.py
"""
import re
import ssl
import sys
import os
import time
import requests
import pandas as pd
import akshare as ak
from requests.adapters import HTTPAdapter
from urllib3.poolmanager import PoolManager

from dragon_strategy.db_config import get_connection, execute_query


class _TLSAdapter(HTTPAdapter):
    """兼容旧服务器的TLS适配器: 降低OpenSSL安全级别以支持legacy加密套件"""
    def init_poolmanager(self, connections, maxsize, block=False):
        ctx = ssl.create_default_context()
        ctx.set_ciphers('DEFAULT@SECLEVEL=1')
        self.poolmanager = PoolManager(
            num_pools=connections, maxsize=maxsize, block=block,
            ssl_context=ctx,
        )


def _post_with_retry(url, tries=3, timeout=20):
    """带TLS适配和重试的POST请求(用于商务部等旧服务器)"""
    session = requests.Session()
    session.mount('https://', _TLSAdapter())
    last_err = None
    for i in range(tries):
        try:
            return session.post(url, timeout=timeout)
        except Exception as e:
            last_err = e
            print(f"    第{i + 1}次请求失败({type(e).__name__}), 等待重试...")
            time.sleep(2 * (i + 1))
    raise last_err


def _parse_cn_date(series):
    """解析日期格式: '2026年01月份' / '202601' / '2026.01' -> datetime"""
    def _parse_one(s):
        if pd.isna(s):
            return pd.NaT
        s = str(s).strip()
        # 带分隔符: 2026年01月 / 2026.01
        m = re.match(r'(\d{4})\D+(\d{1,2})', s)
        if m:
            return pd.Timestamp(year=int(m.group(1)), month=int(m.group(2)), day=1)
        # 纯数字: 202601
        m = re.match(r'^(\d{4})(\d{2})$', s)
        if m:
            return pd.Timestamp(year=int(m.group(1)), month=int(m.group(2)), day=1)
        return pd.NaT
    return series.apply(_parse_one)


def _find_col(columns, keywords):
    """按关键词列表在列名中查找匹配列"""
    for kw in keywords:
        for col in columns:
            if kw in col:
                return col
    return None


def fetch_cpi():
    print("  采集CPI...")
    df = ak.macro_china_cpi()
    if df is None or len(df) == 0:
        return pd.DataFrame()
    date_col = df.columns[0]
    value_col = _find_col(df.columns, ['全国-同比增长', '同比增长', '同比'])
    if value_col is None:
        value_col = df.columns[2] if len(df.columns) > 2 else df.columns[1]
    result = pd.DataFrame({
        'date': _parse_cn_date(df[date_col]),
        'cpi_yoy': pd.to_numeric(df[value_col], errors='coerce')
    }).dropna()
    print(f"    CPI: {len(result)} 条")
    return result


def fetch_ppi():
    print("  采集PPI...")
    df = ak.macro_china_ppi()
    if df is None or len(df) == 0:
        return pd.DataFrame()
    date_col = df.columns[0]
    value_col = _find_col(df.columns, ['当月同比增长', '同比增长', '同比'])
    if value_col is None:
        value_col = df.columns[2] if len(df.columns) > 2 else df.columns[1]
    result = pd.DataFrame({
        'date': _parse_cn_date(df[date_col]),
        'ppi_yoy': pd.to_numeric(df[value_col], errors='coerce')
    }).dropna()
    print(f"    PPI: {len(result)} 条")
    return result


def fetch_pmi():
    print("  采集PMI...")
    df = ak.macro_china_pmi()
    if df is None or len(df) == 0:
        return pd.DataFrame()
    date_col = df.columns[0]
    value_col = _find_col(df.columns, ['制造业-指标', '制造业', 'PMI'])
    if value_col is None:
        value_col = df.columns[1]
    result = pd.DataFrame({
        'date': _parse_cn_date(df[date_col]),
        'pmi': pd.to_numeric(df[value_col], errors='coerce')
    }).dropna()
    print(f"    PMI: {len(result)} 条")
    return result


def fetch_m2():
    print("  采集M2...")
    df = ak.macro_china_supply_of_money()
    if df is None or len(df) == 0:
        return pd.DataFrame()
    date_col = df.columns[0]
    value_col = _find_col(df.columns, ['M2）同比增长', 'M2)同比', 'M2同比'])
    if value_col is None:
        value_col = df.columns[2] if len(df.columns) > 2 else df.columns[1]
    result = pd.DataFrame({
        'date': _parse_cn_date(df[date_col]),
        'm2_yoy': pd.to_numeric(df[value_col], errors='coerce')
    }).dropna()
    print(f"    M2: {len(result)} 条")
    return result


def fetch_shrzgm():
    """采集社会融资规模增量(亿元)

    直接请求商务部接口(与 ak.macro_china_shrzgm 同源),
    自带 _TLSAdapter 解决部分环境 SSL 握手失败问题
    """
    print("  采集社融...")
    url = "https://data.mofcom.gov.cn/datamofcom/front/gnmy/shrzgmQuery"
    r = _post_with_retry(url)
    df = pd.DataFrame(r.json())
    df.columns = [
        "月份",
        "其中-未贴现银行承兑汇票",
        "其中-委托贷款",
        "其中-委托贷款外币贷款",
        "其中-人民币贷款",
        "其中-企业债券",
        "社会融资规模增量",
        "其中-非金融企业境内股票融资",
        "其中-信托贷款",
    ]
    result = pd.DataFrame({
        'date': _parse_cn_date(df["月份"]),
        'shrzgm': pd.to_numeric(df["社会融资规模增量"], errors='coerce')
    }).dropna()
    print(f"    社融: {len(result)} 条")
    return result


def fetch_lpr():
    """采集LPR利率(1年/5年)，月频取每月最新值"""
    print("  采集LPR...")
    df = ak.macro_china_lpr()
    if df is None or len(df) == 0:
        return pd.DataFrame()
    df['date'] = pd.to_datetime(df['TRADE_DATE'])
    df['lpr_1y'] = pd.to_numeric(df['LPR1Y'], errors='coerce')
    df['lpr_5y'] = pd.to_numeric(df['LPR5Y'], errors='coerce')
    result = df[['date', 'lpr_1y', 'lpr_5y']].dropna()
    print(f"    LPR: {len(result)} 条")
    return result


def fetch_bond_yield():
    """采集中美10年期国债收益率(日频)，写入 trade_rate_daily"""
    print("  采集国债收益率...")
    # 近3年数据
    start = (pd.Timestamp.now() - pd.DateOffset(years=3)).strftime('%Y%m%d')
    df = ak.bond_zh_us_rate(start_date=start)
    if df is None or len(df) == 0:
        return 0

    cols = df.columns.tolist()
    date_col = cols[0]
    cn10_col = cols[3]   # 中国国债收益率10年
    us10_col = cols[9]   # 美国国债收益率10年

    conn = get_connection()
    cursor = conn.cursor()
    sql = """
        INSERT INTO trade_rate_daily (rate_date, cn_bond_10y, us_bond_10y, data_source)
        VALUES (%s, %s, %s, 'akshare')
        ON DUPLICATE KEY UPDATE
        cn_bond_10y=COALESCE(VALUES(cn_bond_10y), cn_bond_10y),
        us_bond_10y=COALESCE(VALUES(us_bond_10y), us_bond_10y)
    """
    count = 0
    for _, row in df.iterrows():
        d = row[date_col]
        if pd.isna(d):
            continue
        cn = float(row[cn10_col]) if pd.notna(row[cn10_col]) else None
        us = float(row[us10_col]) if pd.notna(row[us10_col]) else None
        if cn is None and us is None:
            continue
        cursor.execute(sql, (pd.Timestamp(d).strftime('%Y-%m-%d'), cn, us))
        count += 1

    conn.commit()
    cursor.close()
    conn.close()
    print(f"    国债收益率: {count} 条写入 trade_rate_daily")
    return count


# ==================== 月度数据合并写入 ====================

def merge_and_save(dfs):
    """合并月度宏观数据并写入MySQL"""
    merged = None
    for df in dfs:
        if df is None or len(df) == 0:
            continue
        df = df.copy()
        df['month'] = df['date'].dt.to_period('M').dt.to_timestamp('M')
        df = df.drop(columns=['date']).groupby('month').last().reset_index()
        if merged is None:
            merged = df
        else:
            merged = pd.merge(merged, df, on='month', how='outer')

    if merged is None or len(merged) == 0:
        print("  无数据可保存")
        return 0

    merged = merged.sort_values('month').reset_index(drop=True)

    # 只保留近10年
    cutoff = pd.Timestamp.now() - pd.DateOffset(years=10)
    merged = merged[merged['month'] >= cutoff]

    conn = get_connection()
    cursor = conn.cursor()

    sql = """
        INSERT INTO trade_macro_indicator
        (indicator_date, cpi_yoy, ppi_yoy, pmi, m2_yoy, shrzgm, lpr_1y, lpr_5y, data_source)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
        cpi_yoy=COALESCE(VALUES(cpi_yoy), cpi_yoy),
        ppi_yoy=COALESCE(VALUES(ppi_yoy), ppi_yoy),
        pmi=COALESCE(VALUES(pmi), pmi),
        m2_yoy=COALESCE(VALUES(m2_yoy), m2_yoy),
        shrzgm=COALESCE(VALUES(shrzgm), shrzgm),
        lpr_1y=COALESCE(VALUES(lpr_1y), lpr_1y),
        lpr_5y=COALESCE(VALUES(lpr_5y), lpr_5y)
    """

    def _val(row, col):
        v = row.get(col)
        return float(v) if pd.notna(v) else None

    count = 0
    for _, row in merged.iterrows():
        cursor.execute(sql, (
            row['month'].strftime('%Y-%m-%d'),
            _val(row, 'cpi_yoy'), _val(row, 'ppi_yoy'),
            _val(row, 'pmi'), _val(row, 'm2_yoy'),
            _val(row, 'shrzgm'), _val(row, 'lpr_1y'), _val(row, 'lpr_5y'),
            'akshare'
        ))
        count += 1

    conn.commit()
    cursor.close()
    conn.close()
    return count


def sync_macro(on_progress=None, should_stop=None):
    """只写 trade_macro_indicator（CPI/PPI/PMI/M2/社融/LPR）。"""
    def progress(**kw):
        if on_progress:
            on_progress(**kw)

    steps = [
        ("CPI", fetch_cpi),
        ("PPI", fetch_ppi),
        ("PMI", fetch_pmi),
        ("M2", fetch_m2),
        ("社融", fetch_shrzgm),
        ("LPR", fetch_lpr),
    ]
    dfs = []
    failed = 0
    total = len(steps)
    for i, (name, fn) in enumerate(steps):
        if should_stop and should_stop():
            break
        progress(current=i, total=total, message=f"宏观 {name}", success=i - failed, failed=failed)
        try:
            dfs.append(fn())
        except Exception:
            dfs.append(pd.DataFrame())
            failed += 1
    count = merge_and_save(dfs)
    success = total - failed
    progress(
        current=total, total=total, success=success, failed=failed,
        rows_written=count, message=f"宏观写入 {count} 行",
    )
    return {"success": success, "failed": failed, "rows_written": count, "total": total}


def sync_rate(on_progress=None, should_stop=None):
    """只写 trade_rate_daily（中美十年国债）。"""
    def progress(**kw):
        if on_progress:
            on_progress(**kw)

    progress(current=0, total=1, message="国债收益率")
    if should_stop and should_stop():
        return {"success": 0, "failed": 0, "rows_written": 0, "total": 1}
    count = fetch_bond_yield()
    progress(
        current=1, total=1, success=1, failed=0,
        rows_written=count, message=f"利率写入 {count} 行",
    )
    return {"success": 1, "failed": 0, "rows_written": count, "total": 1}


def main():
    print("=" * 60)
    print("宏观经济数据采集 -> MySQL")
    print("=" * 60)

    # 月度指标
    print("\n[1/2] 采集月度宏观指标...")
    df_cpi = fetch_cpi()
    df_ppi = fetch_ppi()
    df_pmi = fetch_pmi()
    df_m2 = fetch_m2()
    df_shrzgm = fetch_shrzgm()
    df_lpr = fetch_lpr()

    all_dfs = [df_cpi, df_ppi, df_pmi, df_m2, df_shrzgm, df_lpr]
    names = ['CPI', 'PPI', 'PMI', 'M2', '社融', 'LPR']
    ok = sum(1 for df in all_dfs if len(df) > 0)
    print(f"\n采集结果: {ok}/{len(all_dfs)} 项成功")

    print("\n合并并写入MySQL...")
    count = merge_and_save(all_dfs)
    print(f"写入 {count} 条月度宏观指标")

    # 日频指标(国债收益率)
    print(f"\n[2/2] 采集日频利率指标...")
    fetch_bond_yield()

    # 概况
    summary = execute_query("""
        SELECT COUNT(*) as cnt,
               MIN(indicator_date) as min_date, MAX(indicator_date) as max_date
        FROM trade_macro_indicator
    """)
    if summary:
        r = summary[0]
        print(f"\ntrade_macro_indicator: {r['cnt']} 期 ({r['min_date']} ~ {r['max_date']})")

    rate_summary = execute_query("""
        SELECT COUNT(*) as cnt,
               MIN(rate_date) as min_date, MAX(rate_date) as max_date
        FROM trade_rate_daily
    """)
    if rate_summary:
        r = rate_summary[0]
        print(f"trade_rate_daily: {r['cnt']} 日 ({r['min_date']} ~ {r['max_date']})")

    print("\n" + "=" * 60)
    print("宏观数据采集完成!")
    print("=" * 60)


if __name__ == "__main__":
    main()
