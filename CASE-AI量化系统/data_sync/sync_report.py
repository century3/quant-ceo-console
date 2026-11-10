# -*- coding: utf-8 -*-
# Author：@跟陈博士学AI
"""
研报数据采集 - AkShare(东方财富+同花顺) -> MySQL
全量A股研报采集，多线程并行

数据源：
  1. 东方财富 - 机构评级明细 (ak.stock_institute_recommend_detail)
     提供每次券商发布的评级/目标价记录
  2. 同花顺 - 盈利预测一致预期 (ak.stock_profit_forecast_ths)
     提供EPS/净利润一致预期

去重策略（核心）：
  东方财富的原始数据包含券商每次发布研究笔记的记录（晨报/点评/行业周报等），
  同一券商对同一股票可能每隔几天发布一篇，但评级和目标价不变。
  去重方式：只保存"观点变化"，即同一券商对同一股票的评级或目标价发生变化时才写入。

采集范围：trade_stock_daily 中所有股票
跳过逻辑：7天内已采集的股票跳过

运行：python 5-研报数据采集.py
"""
import sys
import os
import time
import pandas as pd
import akshare as ak
import pymysql
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

from dragon_strategy.db_config import get_connection, execute_query

if sys.platform == 'win32' and hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

# ============================================================
# 配置
# ============================================================
TEST_MODE = False
TEST_STOCK = '600519.SH'
MAX_RECOMMEND_ROWS = 50
NUM_WORKERS = 4

_print_lock = threading.Lock()


def safe_print(msg):
    with _print_lock:
        print(msg)


# ============================================================
# 采集函数
# ============================================================

def fetch_institute_recommend(stock_code):
    """从东方财富获取机构推荐明细"""
    code_num = stock_code.split('.')[0]
    try:
        df = ak.stock_institute_recommend_detail(symbol=code_num)
        if df is None or len(df) == 0:
            return pd.DataFrame()

        df.columns = ['stock_code_raw', 'stock_name', 'target_price',
                       'rating', 'broker', 'analyst', 'industry', 'report_date']
        df = df.head(MAX_RECOMMEND_ROWS)
        df['target_price'] = pd.to_numeric(df['target_price'], errors='coerce')
        df['report_date'] = pd.to_datetime(df['report_date'], errors='coerce')
        df['stock_code'] = stock_code
        return df
    except Exception:
        return pd.DataFrame()


def deduplicate_recommend(stock_code, df):
    """
    智能去重：同一券商仅保留"观点变化"的记录
    按时间从旧到新遍历，当评级或目标价与该券商上一条不同时才保留
    """
    if df is None or len(df) == 0:
        return pd.DataFrame()

    df_sorted = df.sort_values('report_date', ascending=True).reset_index(drop=True)

    last_opinion = {}
    keep_indices = []

    for idx, row in df_sorted.iterrows():
        broker = str(row['broker'])
        rating = str(row['rating'])
        tp = row['target_price'] if pd.notna(row['target_price']) else None

        key = broker
        prev = last_opinion.get(key)

        if prev is None or prev['rating'] != rating or prev['target_price'] != tp:
            keep_indices.append(idx)
            last_opinion[key] = {'rating': rating, 'target_price': tp}

    return df_sorted.loc[keep_indices]


def fetch_profit_forecast(stock_code):
    """从同花顺获取盈利预测一致预期"""
    code_num = stock_code.split('.')[0]
    forecasts = {}
    for indicator in ['预测年报每股收益', '预测年报净利润']:
        try:
            df = ak.stock_profit_forecast_ths(symbol=code_num, indicator=indicator)
            if df is not None and len(df) > 0:
                df.columns = ['year', 'analyst_count', 'min_val', 'mean_val', 'max_val', 'industry_avg']
                forecasts[indicator] = df
        except Exception:
            pass
    return forecasts


def save_recommend_to_mysql(stock_code, recommend_df):
    """将去重后的机构评级写入 trade_report_consensus"""
    if recommend_df is None or len(recommend_df) == 0:
        return 0

    conn = get_connection()
    cursor = conn.cursor(pymysql.cursors.DictCursor)

    sql_insert = """
        INSERT INTO trade_report_consensus
        (stock_code, broker, report_date, rating, target_price,
         eps_forecast_current, eps_forecast_next, revenue_forecast, source_file)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
        rating=VALUES(rating), target_price=VALUES(target_price)
    """

    # 查询该股票已有的各券商最新观点（用于增量去重）
    existing = {}
    rows = execute_query("""
        SELECT broker, rating, target_price
        FROM trade_report_consensus
        WHERE stock_code=%s AND source_file='eastmoney'
        ORDER BY report_date DESC
    """, (stock_code,))
    for r in rows:
        b = r['broker']
        if b not in existing:
            existing[b] = {
                'rating': r['rating'],
                'target_price': r['target_price']
            }

    count = 0
    for _, row in recommend_df.iterrows():
        broker = str(row['broker'])[:50]
        rating = str(row['rating'])[:20]
        report_date = row['report_date'].strftime('%Y-%m-%d') if pd.notna(row['report_date']) else None
        target_price = float(row['target_price']) if pd.notna(row['target_price']) else None

        # 增量去重：如果数据库中该券商已有相同评级和目标价，跳过
        prev = existing.get(broker)
        if prev:
            prev_tp = float(prev['target_price']) if prev['target_price'] is not None else None
            if prev['rating'] == rating and prev_tp == target_price:
                continue

        cursor.execute(sql_insert, (
            row['stock_code'], broker, report_date, rating, target_price,
            None, None, None, 'eastmoney'
        ))
        count += 1
        existing[broker] = {'rating': rating, 'target_price': target_price}

    conn.commit()
    cursor.close()
    conn.close()
    return count


def save_forecast_to_mysql(stock_code, forecasts):
    """将盈利预测一致预期写入MySQL"""
    if not forecasts:
        return 0

    eps_df = forecasts.get('预测年报每股收益')
    if eps_df is None or len(eps_df) == 0:
        return 0

    profit_df = forecasts.get('预测年报净利润')

    conn = get_connection()
    cursor = conn.cursor()

    sql = """
        INSERT INTO trade_report_consensus
        (stock_code, broker, report_date, rating, target_price,
         eps_forecast_current, eps_forecast_next, revenue_forecast, source_file)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
        eps_forecast_current=VALUES(eps_forecast_current),
        eps_forecast_next=VALUES(eps_forecast_next),
        revenue_forecast=VALUES(revenue_forecast)
    """

    today = datetime.now().strftime('%Y-%m-%d')
    eps_current = float(eps_df.iloc[0]['mean_val']) if len(eps_df) > 0 else None
    eps_next = float(eps_df.iloc[1]['mean_val']) if len(eps_df) > 1 else None
    profit_current = None
    if profit_df is not None and len(profit_df) > 0:
        profit_current = float(profit_df.iloc[0]['mean_val'])

    analyst_count = int(eps_df.iloc[0]['analyst_count']) if len(eps_df) > 0 else 0

    cursor.execute(sql, (
        stock_code,
        "一致预期({}家)".format(analyst_count),
        today, None, None,
        eps_current, eps_next, profit_current,
        'ths_consensus'
    ))

    conn.commit()
    cursor.close()
    conn.close()
    return 1


# ============================================================
# 主流程
# ============================================================

def get_all_stocks():
    """获取全量股票列表"""
    rows = execute_query("SELECT DISTINCT stock_code FROM trade_stock_daily")
    return [r['stock_code'] for r in rows]


def get_recently_collected():
    """获取7天内已采集研报的股票"""
    rows = execute_query("""
        SELECT DISTINCT stock_code FROM trade_report_consensus
        WHERE created_at >= DATE_SUB(CURDATE(), INTERVAL 7 DAY)
    """)
    return {r['stock_code'] for r in rows}


def process_one_stock(stock_code):
    """采集并保存单只股票的研报数据"""
    recommend_count = 0
    forecast_count = 0

    raw_df = fetch_institute_recommend(stock_code)
    if len(raw_df) > 0:
        deduped_df = deduplicate_recommend(stock_code, raw_df)
        recommend_count = save_recommend_to_mysql(stock_code, deduped_df)

    time.sleep(0.5)

    forecasts = fetch_profit_forecast(stock_code)
    if forecasts:
        forecast_count = save_forecast_to_mysql(stock_code, forecasts)

    return stock_code, recommend_count, forecast_count


def main():
    print("=" * 60)
    print("研报数据采集（AkShare - 全量A股）")
    print("=" * 60)

    if TEST_MODE:
        stock_list = [TEST_STOCK]
        print("[测试模式] 只采集 {}".format(TEST_STOCK))
    else:
        stock_list = get_all_stocks()
        print("全量股票: {} 只".format(len(stock_list)))

        collected = get_recently_collected()
        if collected:
            stock_list = [c for c in stock_list if c not in collected]
            print("跳过近7天已采集: {} 只, 待采集: {} 只".format(len(collected), len(stock_list)))

    if not stock_list:
        print("无股票需要采集")
        return

    total = len(stock_list)
    total_recommend = 0
    total_forecast = 0
    done = 0
    start_time = time.time()

    print("\n开始采集 ({} 线程)...".format(NUM_WORKERS))

    with ThreadPoolExecutor(max_workers=NUM_WORKERS) as executor:
        futures = {
            executor.submit(process_one_stock, code): code
            for code in stock_list
        }

        for future in as_completed(futures):
            try:
                code, rec, fct = future.result()
                done += 1
                total_recommend += rec
                total_forecast += fct
            except Exception:
                done += 1

            if done % 100 == 0 or done == total:
                elapsed = time.time() - start_time
                speed = done / elapsed if elapsed > 0 else 0
                eta = (total - done) / speed if speed > 0 else 0
                sys.stdout.write(
                    "\r  [{}/{}] {:.1f}% | {:.1f}/s | ETA {:.0f}s | "
                    "recommend {} forecast {}    ".format(
                        done, total, done * 100 / total,
                        speed, eta, total_recommend, total_forecast
                    )
                )
                sys.stdout.flush()

    print()

    elapsed = time.time() - start_time
    result = execute_query("SELECT COUNT(*) as cnt FROM trade_report_consensus")
    total_db = result[0]['cnt'] if result else 0

    print("\n" + "=" * 60)
    print("研报采集完成! 耗时 {:.1f} 秒".format(elapsed))
    print("  处理: {}/{} 只股票".format(done, total))
    print("  评级: {} 条(去重后新增), 一致预期: {} 条".format(total_recommend, total_forecast))
    print("  trade_report_consensus 总计 {} 条".format(total_db))
    print("=" * 60)


def sync_report(on_progress=None, should_stop=None):
    """写 trade_report_consensus。近 7 天已采过的股票跳过。全市场较慢。"""
    def progress(**kw):
        if on_progress:
            on_progress(**kw)

    stock_list = get_all_stocks()
    collected = get_recently_collected()
    if collected:
        stock_list = [c for c in stock_list if c not in collected]

    total = len(stock_list)
    progress(
        current=0, total=total,
        message=f"研报待采 {total} 只，近7天已齐 {len(collected)} 只",
        success=0, failed=0, rows_written=0,
    )
    if total == 0:
        return {"success": 0, "failed": 0, "rows_written": 0, "total": 0}

    success = failed = rows_written = done = 0
    chunk = 20
    for i in range(0, total, chunk):
        if should_stop and should_stop():
            break
        batch = stock_list[i:i + chunk]
        with ThreadPoolExecutor(max_workers=NUM_WORKERS) as executor:
            futures = {executor.submit(process_one_stock, code): code for code in batch}
            for future in as_completed(futures):
                try:
                    _, rec, fct = future.result()
                    success += 1
                    rows_written += max(rec, 0) + max(fct, 0)
                except Exception:
                    failed += 1
                done += 1
                progress(
                    current=done, total=total,
                    success=success, failed=failed, rows_written=rows_written,
                    message=f"研报 {futures[future]} ({done}/{total})",
                )
    return {"success": success, "failed": failed, "rows_written": rows_written, "total": total}


if __name__ == '__main__':
    main()
