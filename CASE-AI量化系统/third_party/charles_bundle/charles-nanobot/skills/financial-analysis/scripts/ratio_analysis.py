#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
核心财务指标分析工具

功能: 从 akshare 获取的财务摘要 CSV 中提取核心指标，
     分析多年趋势，生成结构化的财务健康状况报告。

用法:
    python ratio_analysis.py --stock 600519
    python ratio_analysis.py --stock 600519 --years 3
    python ratio_analysis.py --stock 688981 --data_dir data/financial_data
"""

import argparse
import json
import os
import sys

import pandas as pd


# 需要提取的核心指标及其中文名
CORE_METRICS = {
    "营业总收入": {"unit": "亿元", "scale": 1e8, "category": "规模"},
    "归母净利润": {"unit": "亿元", "scale": 1e8, "category": "规模"},
    "毛利率": {"unit": "%", "scale": 1, "category": "盈利"},
    "销售净利率": {"unit": "%", "scale": 1, "category": "盈利"},
    "净资产收益率(ROE)": {"unit": "%", "scale": 1, "category": "盈利"},
    "总资产报酬率(ROA)": {"unit": "%", "scale": 1, "category": "盈利"},
    "资产负债率": {"unit": "%", "scale": 1, "category": "风险"},
    "基本每股收益": {"unit": "元", "scale": 1, "category": "每股"},
    "每股净资产": {"unit": "元", "scale": 1, "category": "每股"},
    "每股经营现金流": {"unit": "元", "scale": 1, "category": "每股"},
    "经营现金流量净额": {"unit": "亿元", "scale": 1e8, "category": "现金流"},
    "期间费用率": {"unit": "%", "scale": 1, "category": "效率"},
}


def load_financial_abstract(stock_code: str, data_dir: str) -> pd.DataFrame:
    """加载财务摘要 CSV"""
    csv_path = os.path.join(data_dir, f"{stock_code}_financial_abstract.csv")
    if not os.path.exists(csv_path):
        print(f"[错误] 财务摘要文件不存在: {csv_path}")
        print(f"[提示] 请先执行: python skills/read-pdf/scripts/fetch_financial_data.py --stock {stock_code} --type financial")
        sys.exit(1)

    df = pd.read_csv(csv_path, encoding="utf-8-sig")
    return df


def extract_annual_metrics(df: pd.DataFrame, years: int = 5) -> dict:
    """
    从财务摘要中提取年度核心指标

    财务摘要 CSV 格式: 行为指标，列为日期(如 20241231, 20231231...)
    """
    # 获取所有日期列(格式为 YYYYMMDD 的数字列)
    date_cols = [c for c in df.columns if c not in ("选项", "指标") and str(c).isdigit()]

    # 筛选年报数据(12月31日结尾)
    annual_cols = [c for c in date_cols if str(c).endswith("1231")]
    annual_cols = sorted(annual_cols, reverse=True)[:years]

    if not annual_cols:
        print("[警告] 未找到年度数据，尝试使用最近的季度数据")
        annual_cols = sorted(date_cols, reverse=True)[:years]

    result = {}
    for metric_name, config in CORE_METRICS.items():
        row = df[df["指标"] == metric_name]
        if row.empty:
            continue

        values = {}
        for col in annual_cols:
            val = row[col].values[0] if col in row.columns else None
            if pd.notna(val) and val != "":
                try:
                    num = float(val)
                    values[str(col)] = round(num / config["scale"], 2) if config["scale"] != 1 else round(num, 2)
                except (ValueError, TypeError):
                    pass

        if values:
            sorted_dates = sorted(values.keys(), reverse=True)
            vals_list = [values[d] for d in sorted_dates]

            # 计算同比增长率
            yoy_growth = None
            if len(vals_list) >= 2 and vals_list[1] != 0:
                yoy_growth = round((vals_list[0] - vals_list[1]) / abs(vals_list[1]) * 100, 2)

            # 计算趋势方向
            trend = "stable"
            if len(vals_list) >= 3:
                increases = sum(1 for i in range(len(vals_list) - 1) if vals_list[i] > vals_list[i + 1])
                if increases >= len(vals_list) - 1:
                    trend = "up"
                elif increases <= 0:
                    trend = "down"

            result[metric_name] = {
                "category": config["category"],
                "unit": config["unit"],
                "values": {d: values[d] for d in sorted_dates},
                "latest": vals_list[0] if vals_list else None,
                "yoy_growth": yoy_growth,
                "trend": trend,
            }

    return result


def generate_analysis(stock_code: str, metrics: dict) -> str:
    """生成财务分析报告文本"""
    lines = []
    lines.append(f"{'=' * 60}")
    lines.append(f"  {stock_code} 核心财务指标分析")
    lines.append(f"{'=' * 60}")

    categories = {"规模": [], "盈利": [], "风险": [], "每股": [], "现金流": [], "效率": []}
    for name, data in metrics.items():
        cat = data["category"]
        if cat in categories:
            categories[cat].append((name, data))

    for cat_name, items in categories.items():
        if not items:
            continue
        lines.append(f"\n--- {cat_name}指标 ---")
        for name, data in items:
            latest = data["latest"]
            unit = data["unit"]
            yoy = data["yoy_growth"]
            trend = {"up": "上升", "down": "下降", "stable": "平稳"}.get(data["trend"], "")

            yoy_str = f" (同比 {'+' if yoy > 0 else ''}{yoy}%)" if yoy is not None else ""
            trend_str = f" [{trend}]" if trend else ""

            lines.append(f"  {name}: {latest} {unit}{yoy_str}{trend_str}")

            # 显示历史数据
            vals = data["values"]
            if len(vals) > 1:
                hist = " | ".join(f"{d[:4]}: {v}" for d, v in vals.items())
                lines.append(f"    历史: {hist}")

    # 综合评价
    lines.append(f"\n--- 综合评价 ---")

    roe = metrics.get("净资产收益率(ROE)", {})
    margin = metrics.get("毛利率", {})
    debt = metrics.get("资产负债率", {})
    revenue = metrics.get("营业总收入", {})

    if roe.get("latest"):
        roe_val = roe["latest"]
        if roe_val > 20:
            lines.append(f"  盈利能力: 优秀 (ROE {roe_val}% > 20%)")
        elif roe_val > 10:
            lines.append(f"  盈利能力: 良好 (ROE {roe_val}%)")
        else:
            lines.append(f"  盈利能力: 一般 (ROE {roe_val}%)")

    if margin.get("latest"):
        trend_map = {"up": "改善", "down": "承压", "stable": "稳定"}
        trend_label = trend_map.get(margin.get("trend", ""), "")
        lines.append(f"  毛利水平: {margin['latest']}%，趋势{trend_label}")

    if debt.get("latest"):
        debt_val = debt["latest"]
        if debt_val < 30:
            lines.append(f"  财务安全: 稳健 (负债率 {debt_val}%)")
        elif debt_val < 60:
            lines.append(f"  财务安全: 适中 (负债率 {debt_val}%)")
        else:
            lines.append(f"  财务安全: 偏高 (负债率 {debt_val}%)")

    if revenue.get("yoy_growth") is not None:
        growth = revenue["yoy_growth"]
        if growth > 20:
            lines.append(f"  成长性: 高增长 (营收同比 +{growth}%)")
        elif growth > 0:
            lines.append(f"  成长性: 稳定增长 (营收同比 +{growth}%)")
        else:
            lines.append(f"  成长性: 增速放缓 (营收同比 {growth}%)")

    lines.append(f"{'=' * 60}")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="核心财务指标分析工具")
    parser.add_argument("--stock", required=True, help="股票代码(如 600519)")
    parser.add_argument("--years", type=int, default=5, help="分析年数(默认 5)")
    parser.add_argument("--data_dir", default="data/financial_data", help="财务数据目录")
    parser.add_argument("--output", default=None, help="JSON 结果保存路径(可选)")
    args = parser.parse_args()

    print(f"[开始] 分析 {args.stock} 近 {args.years} 年财务指标")

    df = load_financial_abstract(args.stock, args.data_dir)
    print(f"[加载] 财务摘要共 {len(df)} 行指标")

    metrics = extract_annual_metrics(df, args.years)
    print(f"[分析] 提取到 {len(metrics)} 个核心指标")

    report = generate_analysis(args.stock, metrics)
    print(report)

    if args.output:
        os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump({
                "stock": args.stock,
                "years": args.years,
                "metrics": metrics,
            }, f, ensure_ascii=False, indent=2)
        print(f"\n[保存] JSON 已保存: {args.output}")

    result = {
        "status": "success",
        "stock": args.stock,
        "metrics_count": len(metrics),
    }
    print(f"\n[结果] {json.dumps(result, ensure_ascii=False)}")


if __name__ == "__main__":
    main()
