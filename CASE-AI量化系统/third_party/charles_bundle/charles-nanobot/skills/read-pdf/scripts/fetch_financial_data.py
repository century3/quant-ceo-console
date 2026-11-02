#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
财务数据获取工具

功能：
1. 通过 akshare 获取上市公司结构化财务报表数据（利润表、资产负债表、现金流量表）
2. 通过巨潮资讯网 API 搜索并下载上市公司年报/季报 PDF

用法：
    # 获取结构化财务数据（保存为CSV）
    python fetch_financial_data.py --stock 600519 --type financial

    # 搜索并下载年报PDF（从巨潮资讯网）
    python fetch_financial_data.py --stock 600519 --type pdf --keyword 年度报告

    # 同时获取财务数据和下载PDF
    python fetch_financial_data.py --stock 600519 --type all
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

import pandas as pd
import requests


# ============================================================
# Part 1: akshare 结构化财务数据
# ============================================================

def fetch_financial_statements(stock_code: str, output_dir: str) -> dict:
    """
    通过 akshare 获取三大财务报表 + 财务摘要数据。

    Args:
        stock_code: 股票代码（如 600519）
        output_dir: 输出目录

    Returns:
        获取结果摘要
    """
    import akshare as ak

    os.makedirs(output_dir, exist_ok=True)
    results = {}

    # 新浪财经三大报表
    report_types = {
        "资产负债表": "balance_sheet",
        "利润表": "income_statement",
        "现金流量表": "cash_flow",
    }

    for cn_name, en_name in report_types.items():
        print(f"[获取] {cn_name}: {stock_code}")
        try:
            df = ak.stock_financial_report_sina(stock=stock_code, symbol=cn_name)
            if df is not None and len(df) > 0:
                csv_path = os.path.join(output_dir, f"{stock_code}_{en_name}.csv")
                df.to_csv(csv_path, index=False, encoding="utf-8-sig")
                results[cn_name] = {
                    "status": "success",
                    "rows": len(df),
                    "file": csv_path,
                }
                print(f"  -> {len(df)} 条记录，已保存: {csv_path}")
            else:
                results[cn_name] = {"status": "empty"}
                print(f"  -> 无数据")
        except Exception as e:
            results[cn_name] = {"status": "error", "message": str(e)}
            print(f"  -> 错误: {e}")

    # 东方财富财务摘要（更丰富的字段）
    print(f"[获取] 财务摘要(东方财富): {stock_code}")
    try:
        symbol_prefix = "SH" if stock_code.startswith("6") else "SZ"
        em_symbol = f"{symbol_prefix}{stock_code}"
        df_abstract = ak.stock_financial_abstract(symbol=em_symbol)
        if df_abstract is not None and len(df_abstract) > 0:
            csv_path = os.path.join(output_dir, f"{stock_code}_financial_abstract.csv")
            df_abstract.to_csv(csv_path, index=False, encoding="utf-8-sig")
            results["财务摘要"] = {
                "status": "success",
                "rows": len(df_abstract),
                "file": csv_path,
            }
            print(f"  -> {len(df_abstract)} 条记录，已保存: {csv_path}")
    except Exception as e:
        results["财务摘要"] = {"status": "error", "message": str(e)}
        print(f"  -> 错误: {e}")

    return results


# ============================================================
# Part 2: 巨潮资讯网 PDF 年报下载
# ============================================================

CNINFO_QUERY_URL = "http://www.cninfo.com.cn/new/hisAnnouncement/query"
CNINFO_DOWNLOAD_BASE = "http://static.cninfo.com.cn/"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json, text/javascript, */*",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "Referer": "http://www.cninfo.com.cn/new/commonUrl?url=disclosure/list/notice",
}

# 巨潮公告类别编码
CATEGORY_MAP = {
    "年度报告": "category_ndbg_szsh",
    "半年度报告": "category_bndbg_szsh",
    "一季度报告": "category_yjdbg_szsh",
    "三季度报告": "category_sjdbg_szsh",
    "业绩预告": "category_yjygjxz_szsh",
}


def _get_cninfo_orgid(stock_code: str) -> str:
    """通过巨潮 API 查询股票对应的机构ID"""
    url = "http://www.cninfo.com.cn/new/information/topSearch/query"
    data = {"keyWord": stock_code, "maxSecNum": 10, "maxListNum": 5}
    try:
        resp = requests.post(url, data=data, headers=HEADERS, timeout=10)
        result = resp.json()
        # 返回格式可能是列表或带 keyBoardList 的字典
        items = result if isinstance(result, list) else result.get("keyBoardList", [])
        for item in items:
            if item.get("code") == stock_code:
                return item.get("orgId", "")
        if items:
            return items[0].get("orgId", "")
    except Exception:
        pass
    return ""


def search_cninfo_reports(
    stock_code: str,
    category: str = "年度报告",
    start_date: str = "",
    end_date: str = "",
    max_results: int = 10,
) -> list:
    """
    搜索巨潮资讯网上的公告/报告。

    Args:
        stock_code: 股票代码
        category: 报告类别（年度报告/半年度报告/一季度报告/三季度报告/业绩预告）
        start_date: 起始日期（如 2024-01-01）
        end_date: 结束日期（如 2025-12-31）
        max_results: 最大返回数量

    Returns:
        公告列表
    """
    org_id = _get_cninfo_orgid(stock_code)

    category_code = CATEGORY_MAP.get(category, "category_ndbg_szsh")

    se_date = ""
    if start_date and end_date:
        se_date = f"{start_date}~{end_date}"

    data = {
        "pageNum": 1,
        "pageSize": max_results,
        "column": "szse",
        "tabName": "fulltext",
        "plate": "",
        "stock": f"{stock_code},{org_id}" if org_id else stock_code,
        "searchkey": "",
        "secid": "",
        "category": category_code,
        "trade": "",
        "seDate": se_date,
        "sortName": "",
        "sortType": "",
        "isHLtitle": "true",
    }

    print(f"[搜索] 巨潮资讯网: {stock_code} - {category}")
    try:
        resp = requests.post(CNINFO_QUERY_URL, data=data, headers=HEADERS, timeout=15)
        result = resp.json()
        announcements = result.get("announcements", [])
        if not announcements:
            print("  -> 未找到相关公告")
            return []

        reports = []
        for ann in announcements:
            title = ann.get("announcementTitle", "").replace("<em>", "").replace("</em>", "")
            reports.append({
                "title": title,
                "date": ann.get("announcementTime", ""),
                "url": CNINFO_DOWNLOAD_BASE + ann.get("adjunctUrl", ""),
                "type": ann.get("announcementType", ""),
                "sec_name": ann.get("secName", ""),
                "sec_code": ann.get("secCode", ""),
            })

        # 将时间戳转为日期
        for r in reports:
            if r["date"] and isinstance(r["date"], (int, float)):
                r["date"] = pd.Timestamp(r["date"], unit="ms").strftime("%Y-%m-%d")

        print(f"  -> 找到 {len(reports)} 份报告")
        return reports

    except Exception as e:
        print(f"  -> 搜索失败: {e}")
        return []


def download_pdf_report(url: str, save_path: str) -> bool:
    """下载单个 PDF 报告"""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=60, stream=True)
        if resp.status_code == 200:
            with open(save_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)
            size_mb = os.path.getsize(save_path) / (1024 * 1024)
            print(f"  -> 已下载: {os.path.basename(save_path)} ({size_mb:.1f}MB)")
            return True
        else:
            print(f"  -> 下载失败: HTTP {resp.status_code}")
            return False
    except Exception as e:
        print(f"  -> 下载异常: {e}")
        return False


def fetch_pdf_reports(
    stock_code: str,
    output_dir: str,
    category: str = "年度报告",
    keyword: str = "",
    max_download: int = 3,
    start_date: str = "",
    end_date: str = "",
) -> dict:
    """
    从巨潮资讯网搜索并下载 PDF 报告。

    Args:
        stock_code: 股票代码
        output_dir: 下载目录
        category: 报告类别
        keyword: 标题过滤关键词（如"年度报告"会跳过"摘要"）
        max_download: 最大下载数量
        start_date: 起始日期
        end_date: 结束日期

    Returns:
        下载结果摘要
    """
    os.makedirs(output_dir, exist_ok=True)

    reports = search_cninfo_reports(
        stock_code, category=category,
        start_date=start_date, end_date=end_date,
        max_results=max_download * 3,
    )

    if not reports:
        return {"status": "no_reports", "downloaded": 0}

    # 按关键词过滤（排除摘要等）
    if keyword:
        reports = [r for r in reports if keyword in r["title"]]
    # 默认排除"摘要"、"已取消"
    reports = [r for r in reports if "摘要" not in r["title"] and "取消" not in r["title"]]

    downloaded = []
    for i, report in enumerate(reports[:max_download]):
        safe_title = report["title"].replace("/", "_").replace("\\", "_")
        safe_title = safe_title.replace(":", "").replace("*", "").replace("?", "")
        safe_title = safe_title.replace('"', "").replace("<", "").replace(">", "").replace("|", "")
        filename = f"{report['sec_code']}_{safe_title}.pdf"
        save_path = os.path.join(output_dir, filename)

        if os.path.exists(save_path):
            print(f"  -> 已存在，跳过: {filename}")
            downloaded.append(save_path)
            continue

        print(f"[下载] ({i+1}/{min(len(reports), max_download)}) {report['title']}")
        if download_pdf_report(report["url"], save_path):
            downloaded.append(save_path)

        time.sleep(1)

    # 保存搜索结果元数据
    meta_path = os.path.join(output_dir, f"{stock_code}_report_list.json")
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(reports, f, ensure_ascii=False, indent=2)

    return {
        "status": "success",
        "total_found": len(reports),
        "downloaded": len(downloaded),
        "files": downloaded,
        "meta_file": meta_path,
    }


# ============================================================
# Main
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="财务数据获取工具")
    parser.add_argument("--stock", required=True, help="股票代码（如 600519）")
    parser.add_argument(
        "--type",
        choices=["financial", "pdf", "all"],
        default="all",
        help="获取类型：financial=结构化数据, pdf=PDF年报, all=全部",
    )
    parser.add_argument("--output_dir", default="./data", help="输出根目录")
    parser.add_argument(
        "--category",
        default="年度报告",
        choices=["年度报告", "半年度报告", "一季度报告", "三季度报告", "业绩预告"],
        help="PDF报告类别",
    )
    parser.add_argument("--keyword", default="", help="PDF标题过滤关键词")
    parser.add_argument("--max_download", type=int, default=3, help="最大PDF下载数量")
    parser.add_argument("--start_date", default="", help="起始日期（如 2024-01-01）")
    parser.add_argument("--end_date", default="", help="结束日期（如 2025-12-31）")
    args = parser.parse_args()

    results = {}

    if args.type in ("financial", "all"):
        fin_dir = os.path.join(args.output_dir, "financial_data")
        results["financial"] = fetch_financial_statements(args.stock, fin_dir)

    if args.type in ("pdf", "all"):
        pdf_dir = os.path.join(args.output_dir, "financial_reports")
        results["pdf"] = fetch_pdf_reports(
            args.stock,
            pdf_dir,
            category=args.category,
            keyword=args.keyword,
            max_download=args.max_download,
            start_date=args.start_date,
            end_date=args.end_date,
        )

    print(f"\n[完成] {json.dumps(results, ensure_ascii=False, indent=2, default=str)}")
    return results


if __name__ == "__main__":
    main()
