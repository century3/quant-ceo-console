#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
新闻抓取器

功能：通过 akshare 获取东方财富个股新闻、上市公司公告等，
      支持关键词过滤和时间范围筛选。

用法：
    python news_fetcher.py --stock 002594 --days 7
    python news_fetcher.py --keywords 资产重组 --days 3
    python news_fetcher.py --stock 600519 --keywords 业绩,分红 --days 30
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from typing import List, Optional

import pandas as pd


def fetch_stock_news(stock_code: str, limit: int = 100) -> pd.DataFrame:
    """
    获取个股新闻（东方财富）。

    Args:
        stock_code: 股票代码（如 002594、600519）
        limit: 获取条数上限

    Returns:
        新闻 DataFrame
    """
    import akshare as ak

    try:
        df = ak.stock_news_em(symbol=stock_code)
        if df is not None and not df.empty:
            print(f"[获取] 个股新闻: {stock_code}，共 {len(df)} 条")
            return df.head(limit)
        else:
            print(f"[警告] 未获取到 {stock_code} 的新闻")
            return pd.DataFrame()
    except Exception as e:
        print(f"[错误] 获取个股新闻失败: {e}")
        return pd.DataFrame()


def fetch_stock_notices(stock_code: str) -> pd.DataFrame:
    """
    获取上市公司公告。

    Args:
        stock_code: 股票代码

    Returns:
        公告 DataFrame
    """
    import akshare as ak

    try:
        # 尝试获取个股公告
        df = ak.stock_notice_report(symbol=stock_code)
        if df is not None and not df.empty:
            print(f"[获取] 公司公告: {stock_code}，共 {len(df)} 条")
            return df
        else:
            print(f"[提示] 未获取到 {stock_code} 的公告")
            return pd.DataFrame()
    except Exception as e:
        print(f"[提示] 获取公司公告接口异常（可能需要登录或接口变更）: {e}")
        return pd.DataFrame()


def fetch_cctv_news(date_str: str = None) -> pd.DataFrame:
    """
    获取央视新闻（政策面参考）。

    Args:
        date_str: 日期字符串（格式 YYYYMMDD），默认今天

    Returns:
        新闻 DataFrame
    """
    import akshare as ak

    if date_str is None:
        date_str = datetime.now().strftime("%Y%m%d")

    try:
        df = ak.news_cctv(date=date_str)
        if df is not None and not df.empty:
            print(f"[获取] 央视新闻({date_str}): 共 {len(df)} 条")
            return df
        else:
            print(f"[提示] 未获取到 {date_str} 的央视新闻")
            return pd.DataFrame()
    except Exception as e:
        print(f"[提示] 获取央视新闻异常: {e}")
        return pd.DataFrame()


def filter_by_keywords(df: pd.DataFrame, keywords: List[str], text_columns: List[str] = None) -> pd.DataFrame:
    """
    按关键词过滤新闻。

    Args:
        df: 新闻 DataFrame
        keywords: 关键词列表
        text_columns: 搜索的列名列表（自动检测）

    Returns:
        过滤后的 DataFrame
    """
    if df.empty or not keywords:
        return df

    # 自动检测文本列
    if text_columns is None:
        possible_cols = ["title", "content", "新闻标题", "新闻内容", "标题", "内容"]
        text_columns = [c for c in possible_cols if c in df.columns]
        if not text_columns:
            text_columns = df.columns.tolist()

    # 构建过滤条件
    mask = pd.Series([False] * len(df), index=df.index)
    for col in text_columns:
        for kw in keywords:
            mask = mask | df[col].astype(str).str.contains(kw, na=False)

    filtered = df[mask]
    print(f"[过滤] 关键词 {keywords}，匹配 {len(filtered)}/{len(df)} 条")
    return filtered


def filter_by_date(df: pd.DataFrame, days: int, date_column: str = None) -> pd.DataFrame:
    """
    按时间范围过滤新闻。

    Args:
        df: 新闻 DataFrame
        days: 最近 N 天
        date_column: 日期列名（自动检测）

    Returns:
        过滤后的 DataFrame
    """
    if df.empty:
        return df

    # 自动检测日期列
    if date_column is None:
        possible_cols = ["发布时间", "日期", "date", "publish_date", "公告日期", "发布日期"]
        for col in possible_cols:
            if col in df.columns:
                date_column = col
                break

    if date_column is None:
        print("[提示] 未找到日期列，跳过时间过滤")
        return df

    try:
        df[date_column] = pd.to_datetime(df[date_column], errors="coerce")
        cutoff = datetime.now() - timedelta(days=days)
        filtered = df[df[date_column] >= cutoff]
        print(f"[时间] 最近 {days} 天，筛选 {len(filtered)}/{len(df)} 条")
        return filtered
    except Exception as e:
        print(f"[提示] 时间过滤异常: {e}")
        return df


def deduplicate(df: pd.DataFrame) -> pd.DataFrame:
    """去重"""
    if df.empty:
        return df

    title_cols = [c for c in df.columns if "标题" in c or "title" in c.lower()]
    if title_cols:
        before = len(df)
        df = df.drop_duplicates(subset=title_cols, keep="first")
        print(f"[去重] {before} -> {len(df)} 条")
    return df


def save_news(df: pd.DataFrame, output_file: str) -> str:
    """将新闻保存为 JSON 文件"""
    os.makedirs(os.path.dirname(output_file) or ".", exist_ok=True)

    # 将 DataFrame 转为可序列化的字典列表
    records = df.to_dict(orient="records")
    for record in records:
        for k, v in record.items():
            if isinstance(v, pd.Timestamp):
                record[k] = v.strftime("%Y-%m-%d %H:%M:%S")
            elif pd.isna(v):
                record[k] = None

    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2, default=str)

    return output_file


def main():
    parser = argparse.ArgumentParser(description="新闻抓取器")
    parser.add_argument("--stock", default=None, help="股票代码（如 002594、600519）")
    parser.add_argument("--keywords", default=None, help="关键词，逗号分隔（如 资产重组,回购）")
    parser.add_argument("--days", type=int, default=7, help="最近 N 天（默认 7）")
    parser.add_argument("--output_dir", default="./data", help="输出目录")
    parser.add_argument("--include_cctv", action="store_true", help="是否包含央视新闻")
    args = parser.parse_args()

    if not args.stock and not args.keywords:
        print("[错误] 请指定 --stock（股票代码）或 --keywords（关键词）")
        sys.exit(1)

    keywords = []
    if args.keywords:
        keywords = [k.strip() for k in args.keywords.split(",") if k.strip()]

    all_news = pd.DataFrame()

    # 获取个股新闻
    if args.stock:
        print(f"\n[任务] 获取 {args.stock} 的新闻（最近 {args.days} 天）")
        stock_news = fetch_stock_news(args.stock)
        if not stock_news.empty:
            stock_news = filter_by_date(stock_news, args.days)
            if keywords:
                stock_news = filter_by_keywords(stock_news, keywords)
            all_news = pd.concat([all_news, stock_news], ignore_index=True)

        # 获取公司公告
        notices = fetch_stock_notices(args.stock)
        if not notices.empty:
            notices = filter_by_date(notices, args.days)
            if keywords:
                notices = filter_by_keywords(notices, keywords)
            all_news = pd.concat([all_news, notices], ignore_index=True)

    # 获取央视新闻（如果要求）
    if args.include_cctv:
        for i in range(min(args.days, 3)):
            date = (datetime.now() - timedelta(days=i)).strftime("%Y%m%d")
            cctv = fetch_cctv_news(date)
            if not cctv.empty:
                if keywords:
                    cctv = filter_by_keywords(cctv, keywords)
                all_news = pd.concat([all_news, cctv], ignore_index=True)

    # 如果只有关键词没有股票代码，尝试搜索通用新闻
    if not args.stock and keywords:
        print(f"\n[任务] 搜索关键词: {keywords}")
        # 使用关键词搜索最近的新闻
        for kw in keywords:
            try:
                import akshare as ak
                kw_news = ak.stock_news_em(symbol=kw)
                if kw_news is not None and not kw_news.empty:
                    kw_news = filter_by_date(kw_news, args.days)
                    all_news = pd.concat([all_news, kw_news], ignore_index=True)
            except Exception:
                pass

    # 去重
    all_news = deduplicate(all_news)

    if all_news.empty:
        print("\n[结果] 未获取到符合条件的新闻")
        result = {"status": "no_data", "message": "未获取到新闻"}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        sys.exit(0)

    # 保存结果
    if args.stock:
        output_file = os.path.join(args.output_dir, f"{args.stock}_news.json")
    else:
        kw_tag = "_".join(keywords[:3])
        output_file = os.path.join(args.output_dir, f"{kw_tag}_news.json")

    save_news(all_news, output_file)

    result = {
        "status": "success",
        "total_news": len(all_news),
        "stock": args.stock,
        "keywords": keywords,
        "days": args.days,
        "output_file": output_file,
        "columns": list(all_news.columns),
    }
    print(f"\n[结果] {json.dumps(result, ensure_ascii=False, indent=2)}")


if __name__ == "__main__":
    main()
