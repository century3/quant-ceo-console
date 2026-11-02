#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
研报生成器

功能：将五步法分析结果组装为完整的 Markdown 格式深度研报。
包含封面信息、目录、五步分析内容、风险提示和免责声明。

用法：
    python report_generator.py --analysis_file <分析结果JSON> --output_dir <输出目录>
    python report_generator.py --analysis_file output/贵州茅台_analysis.json --output_dir output/reports/
"""

import argparse
import json
import os
import sys
from datetime import datetime


# 研报 Markdown 模板
REPORT_TEMPLATE = """# {stock_name} - 深度分析报告

> **分析框架**: 国泰君安"五步法"  
> **生成时间**: {analysis_date}  
> **分析模型**: {model}  
> **数据来源**: 公司财报 (来源页码: {source_pages})

---

## 目录

1. [信息差分析](#1-信息差分析)
2. [逻辑差分析](#2-逻辑差分析)
3. [预期差分析](#3-预期差分析)
4. [催化剂识别](#4-催化剂识别)
5. [投资结论](#5-投资结论)
6. [风险提示](#6-风险提示)
7. [免责声明](#7-免责声明)

---

{steps_content}

---

## 6. 风险提示

- 本报告基于公开财报数据和 AI 分析生成，分析结论可能存在偏差
- 财报数据具有滞后性，不代表公司当前经营状况
- 市场环境变化可能导致分析假设不再成立
- 行业政策调整可能对公司经营产生重大影响
- AI 模型的分析能力有限，无法完全替代专业分析师的判断

---

## 7. 免责声明

本报告由 AI 投研助手 Charles 自动生成，仅供学习和研究参考，**不构成任何投资建议**。

- 报告内容基于公开信息和 AI 分析，不保证信息的准确性和完整性
- 投资者据此操作，风险自担
- 报告作者不对任何投资损失承担责任
- 在做出投资决策前，建议咨询专业投资顾问

---

*报告生成于 {generation_time}*
"""

# 每个步骤的章节模板
STEP_SECTION_TEMPLATE = """## {step_num}. {step_title}

> 数据来源页码: {source_pages}

{analysis}

"""

# 步骤名称到章节标题的映射
STEP_TITLES = {
    "信息差": "信息差分析",
    "逻辑差": "逻辑差分析",
    "预期差": "预期差分析",
    "催化剂": "催化剂识别",
    "结论": "投资结论",
}


def load_analysis(analysis_file: str) -> dict:
    """加载五步法分析结果"""
    with open(analysis_file, "r", encoding="utf-8") as f:
        return json.load(f)


def generate_report(analysis: dict) -> str:
    """
    将分析结果组装为 Markdown 研报。

    Args:
        analysis: 五步法分析结果（来自 five_step_analysis.py 的输出）

    Returns:
        完整的 Markdown 格式研报
    """
    stock_name = analysis["stock_name"]
    analysis_date = analysis["analysis_date"]
    model = analysis["model"]
    all_pages = analysis.get("all_source_pages", [])

    # 生成各步骤章节
    steps_content = ""
    for step_data in analysis["steps"]:
        step_num = step_data["step"]
        step_name = step_data["name"]
        step_title = STEP_TITLES.get(step_name, step_name)
        step_analysis = step_data["analysis"]
        step_pages = step_data.get("source_pages", [])

        section = STEP_SECTION_TEMPLATE.format(
            step_num=step_num,
            step_title=step_title,
            source_pages=", ".join(str(p) for p in step_pages) if step_pages else "未标注",
            analysis=step_analysis,
        )
        steps_content += section

    # 组装完整报告
    report = REPORT_TEMPLATE.format(
        stock_name=stock_name,
        analysis_date=analysis_date,
        model=model,
        source_pages=", ".join(str(p) for p in all_pages) if all_pages else "未标注",
        steps_content=steps_content,
        generation_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )

    return report


def save_report(report: str, stock_name: str, output_dir: str) -> str:
    """
    保存研报到文件。

    Returns:
        报告文件路径
    """
    os.makedirs(output_dir, exist_ok=True)

    date_str = datetime.now().strftime("%Y%m%d")
    filename = f"{stock_name}_深度研报_{date_str}.md"
    filepath = os.path.join(output_dir, filename)

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(report)

    return filepath


def main():
    parser = argparse.ArgumentParser(description="研报生成器")
    parser.add_argument("--analysis_file", required=True, help="五步法分析结果 JSON 文件")
    parser.add_argument("--output_dir", default="./output/reports", help="研报输出目录")
    args = parser.parse_args()

    if not os.path.exists(args.analysis_file):
        print(f"[错误] 分析文件不存在: {args.analysis_file}")
        print("[提示] 请先运行 five_step_analysis.py 生成分析结果")
        sys.exit(1)

    print(f"[开始] 生成深度分析研报")

    # 加载分析结果
    analysis = load_analysis(args.analysis_file)
    stock_name = analysis["stock_name"]
    print(f"[信息] 目标公司: {stock_name}")
    print(f"[信息] 分析步骤: {len(analysis['steps'])} 步")

    # 生成报告
    report = generate_report(analysis)
    print(f"[完成] 研报生成完成，共 {len(report)} 字符")

    # 保存报告
    filepath = save_report(report, stock_name, args.output_dir)
    print(f"[保存] 研报已保存: {filepath}")

    result = {
        "status": "success",
        "stock_name": stock_name,
        "report_file": filepath,
        "report_length": len(report),
        "steps_included": len(analysis["steps"]),
    }
    print(f"\n[结果] {json.dumps(result, ensure_ascii=False, indent=2)}")


if __name__ == "__main__":
    main()
