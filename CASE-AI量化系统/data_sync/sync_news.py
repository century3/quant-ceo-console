# -*- coding: utf-8 -*-
# Author：@跟陈博士学AI
"""
新闻事件采集 - AkShare -> MySQL

采集范围：全量A股（trade_stock_daily 中所有股票）
数据源：AkShare stock_news_em() - 东方财富个股新闻
去重方式：按标题去重（批量预加载已有标题到内存，避免逐条查库）
跳过逻辑：当日已采集过的股票跳过

运行：python 4-新闻事件采集.py
"""
import sys
import os
import time
import pymysql
import akshare as ak
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from dragon_strategy.db_config import get_connection, execute_query

if sys.platform == 'win32' and hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

# ============================================================
# 配置
# ============================================================
NUM_WORKERS = 8

POSITIVE_WORDS = ['涨停', '大涨', '利好', '增长', '突破', '新高', '预增', '增持',
                  '盈利', '超预期', '重大突破', '战略合作', '中标']
NEGATIVE_WORDS = ['跌停', '大跌', '利空', '下降', '跌破', '新低', '预减', '减持',
                  '亏损', '违规', '处罚', '退市', '暴雷', '爆仓']
IMPORTANT_WORDS = ['资产重组', '业绩预增', '业绩预减', '高送转', '股权激励',
                   '定向增发', '股东减持', '股东增持', '重大合同', '中标',
                   '收购', '并购', '停牌', '复牌', '退市', '回购']

_print_lock = threading.Lock()


def safe_print(msg):
    with _print_lock:
        print(msg)


def analyze_sentiment(title):
    for word in POSITIVE_WORDS:
        if word in title:
            return 'positive'
    for word in NEGATIVE_WORDS:
        if word in title:
            return 'negative'
    return 'neutral'


def check_important(title):
    for word in IMPORTANT_WORDS:
        if word in title:
            return True
    return False


# ============================================================
# 获取需要采集的股票列表
# ============================================================

def get_all_stocks():
    """获取全量股票列表"""
    rows = execute_query("SELECT DISTINCT stock_code FROM trade_stock_daily")
    return [r['stock_code'] for r in rows]


def get_today_collected():
    """获取当日已采集过新闻的股票"""
    rows = execute_query("""
        SELECT DISTINCT stock_code FROM trade_stock_news
        WHERE DATE(created_at) = CURDATE()
    """)
    return {r['stock_code'] for r in rows}


def load_existing_titles():
    """一次性加载所有已有新闻标题，用于内存去重（避免逐条查库）"""
    rows = execute_query("SELECT title FROM trade_stock_news")
    return {r['title'] for r in rows}


# ============================================================
# 新闻采集
# ============================================================

def fetch_news_akshare(stock_code):
    """通过 AkShare 采集个股新闻"""
    code_num = stock_code.split('.')[0]
    news_list = []
    try:
        df = ak.stock_news_em(symbol=code_num)
    except Exception:
        return news_list
    if df is None or len(df) == 0:
        return news_list

    for _, row in df.iterrows():
        title = str(row.get('新闻标题', '')).strip()
        if not title:
            continue
        content = str(row.get('新闻内容', '')).strip()
        url = str(row.get('新闻链接', '')).strip()
        pub_time = str(row.get('发布时间', '')).strip()
        source = str(row.get('文章来源', '')).strip()

        news_list.append({
            'title': title,
            'content': content[:2000] if content else '',
            'link': url,
            'published_at': pub_time if pub_time else None,
            'sentiment': analyze_sentiment(title),
            'is_important': check_important(title),
            'source': source or 'eastmoney',
            'news_type': 'news',
        })

    return news_list


# ============================================================
# 写入数据库
# ============================================================

# 全局标题集合（线程安全：只做 in 检查和 add，Python GIL 保证原子性）
_existing_titles = set()


def save_news_to_db(stock_code, news_list):
    """新闻去重后写入MySQL，利用内存中的标题集合避免逐条查库"""
    global _existing_titles
    if not news_list:
        return 0

    new_items = [n for n in news_list if n['title'] not in _existing_titles]
    if not new_items:
        return 0

    conn = get_connection()
    cursor = conn.cursor()
    saved = 0

    for news in new_items:
        try:
            cursor.execute("""
                INSERT INTO trade_stock_news
                (stock_code, news_type, title, content, source, source_url,
                 sentiment, is_important, published_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                stock_code, news['news_type'], news['title'],
                news['content'], news['source'], news['link'],
                news['sentiment'], 1 if news['is_important'] else 0,
                news['published_at']
            ))
            _existing_titles.add(news['title'])
            saved += 1
        except pymysql.err.IntegrityError:
            _existing_titles.add(news['title'])

    conn.commit()
    cursor.close()
    conn.close()
    return saved


# ============================================================
# 单只股票处理
# ============================================================

def process_one_stock(stock_code):
    """采集单只股票新闻"""
    news = fetch_news_akshare(stock_code)
    saved = save_news_to_db(stock_code, news)
    return stock_code, len(news), saved


# ============================================================
# 主流程
# ============================================================

def main():
    global _existing_titles

    print("=" * 60)
    print("新闻事件采集（全量A股）")
    print("=" * 60)

    stock_list = get_all_stocks()
    print("全量股票: {} 只".format(len(stock_list)))

    # 跳过当日已采集过的股票
    collected = get_today_collected()
    if collected:
        stock_list = [c for c in stock_list if c not in collected]
        print("跳过当日已采集: {} 只, 待采集: {} 只".format(len(collected), len(stock_list)))

    if not stock_list:
        print("全部股票当日已采集，无需再跑")
        return

    # 预加载已有标题到内存（一次查询，后续不再逐条查库）
    print("加载已有标题用于去重...")
    _existing_titles = load_existing_titles()
    print("  已有 {} 条标题".format(len(_existing_titles)))

    total = len(stock_list)
    total_fetched = 0
    total_saved = 0
    done = 0
    start_time = time.time()

    print("\n开始采集 ({} 线程)...".format(NUM_WORKERS))

    with ThreadPoolExecutor(max_workers=NUM_WORKERS) as executor:
        futures = {
            executor.submit(process_one_stock, code): code
            for code in stock_list
        }

        for future in as_completed(futures):
            done += 1
            code, fetched, saved = None, 0, 0
            try:
                code, fetched, saved = future.result()
                total_fetched += fetched
                total_saved += saved
            except Exception:
                pass

            if done % 200 == 0 or done == total:
                elapsed = time.time() - start_time
                speed = done / elapsed if elapsed > 0 else 0
                eta = (total - done) / speed if speed > 0 else 0
                sys.stdout.write(
                    "\r  [{}/{}] {:.1f}% | {:.1f}/s | ETA {:.0f}s | "
                    "fetched {} saved {}    ".format(
                        done, total, done * 100 / total,
                        speed, eta, total_fetched, total_saved
                    )
                )
                sys.stdout.flush()

    print()

    elapsed = time.time() - start_time
    result = execute_query("SELECT COUNT(*) as cnt FROM trade_stock_news")
    total_db = result[0]['cnt'] if result else 0

    print("\n" + "=" * 60)
    print("新闻采集完成! 耗时 {:.1f} 秒".format(elapsed))
    print("  处理: {} 只股票".format(done))
    print("  采集: {} 条, 新增: {} 条".format(total_fetched, total_saved))
    print("  trade_stock_news 总计 {} 条".format(total_db))
    print("=" * 60)


def sync_news(on_progress=None, should_stop=None):
    """写 trade_stock_news。当日已采过的股票跳过。全市场较慢。"""
    global _existing_titles

    def progress(**kw):
        if on_progress:
            on_progress(**kw)

    stock_list = get_all_stocks()
    collected = get_today_collected()
    if collected:
        stock_list = [c for c in stock_list if c not in collected]

    total = len(stock_list)
    progress(
        current=0, total=total,
        message=f"新闻待采 {total} 只，当日已齐 {len(collected)} 只",
        success=0, failed=0, rows_written=0,
    )
    if total == 0:
        return {"success": 0, "failed": 0, "rows_written": 0, "total": 0}

    _existing_titles = load_existing_titles()
    success = failed = rows_written = done = 0
    chunk = 40
    for i in range(0, total, chunk):
        if should_stop and should_stop():
            break
        batch = stock_list[i:i + chunk]
        with ThreadPoolExecutor(max_workers=NUM_WORKERS) as executor:
            futures = {executor.submit(process_one_stock, code): code for code in batch}
            for future in as_completed(futures):
                try:
                    _, _, saved = future.result()
                    success += 1
                    rows_written += max(saved, 0)
                except Exception:
                    failed += 1
                done += 1
                progress(
                    current=done, total=total,
                    success=success, failed=failed, rows_written=rows_written,
                    message=f"新闻 {futures[future]} ({done}/{total})",
                )
    return {"success": success, "failed": failed, "rows_written": rows_written, "total": total}


if __name__ == '__main__':
    main()
