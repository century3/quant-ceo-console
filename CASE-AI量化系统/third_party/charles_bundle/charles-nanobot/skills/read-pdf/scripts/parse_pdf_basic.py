#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PyPDF2 基础 PDF 解析器

功能：从 PDF 中提取纯文本，记录每个字符对应的页码映射。
适用于：纯文本型 PDF（公告、通知、文字型报告）。
对于含复杂表格的财务报表，建议使用 parse_pdf_ocr.py（DeepSeek-OCR-2）。

用法：
    python parse_pdf_basic.py --pdf <PDF文件路径> --output_dir <输出目录>
"""

import argparse
import json
import os
import sys
from typing import List, Tuple

from PyPDF2 import PdfReader


def extract_text_with_pages(pdf_path: str) -> Tuple[str, List[dict]]:
    """
    从 PDF 中逐页提取文本，返回完整文本和按页组织的结果。

    Args:
        pdf_path: PDF 文件路径

    Returns:
        full_text: 合并后的全文
        pages: 每页的文本和元数据列表
    """
    reader = PdfReader(pdf_path)
    full_text = ""
    pages = []
    char_page_mapping = []

    for page_num, page in enumerate(reader.pages, start=1):
        text = page.extract_text()
        if text:
            pages.append({
                "page": page_num,
                "text": text,
                "char_count": len(text),
            })
            full_text += text
            char_page_mapping.extend([page_num] * len(text))
        else:
            pages.append({
                "page": page_num,
                "text": "",
                "char_count": 0,
            })
            print(f"[警告] 第 {page_num} 页未提取到文本（可能是扫描件，建议使用 parse_pdf_ocr.py）")

    return full_text, pages, char_page_mapping


def save_extracted_text(
    full_text: str,
    pages: List[dict],
    char_page_mapping: List[int],
    output_dir: str,
    pdf_name: str,
):
    """
    将提取的文本保存到文件。

    保存内容：
    - {pdf_name}_full.txt: 合并后的全文
    - {pdf_name}_pages.json: 按页组织的文本和元数据
    - {pdf_name}_mapping.json: 字符级页码映射的摘要信息
    """
    os.makedirs(output_dir, exist_ok=True)

    base = os.path.splitext(pdf_name)[0]

    # 保存全文
    full_path = os.path.join(output_dir, f"{base}_full.txt")
    with open(full_path, "w", encoding="utf-8") as f:
        f.write(full_text)
    print(f"[完成] 全文已保存: {full_path} ({len(full_text)} 字符)")

    # 保存按页文本
    pages_path = os.path.join(output_dir, f"{base}_pages.json")
    with open(pages_path, "w", encoding="utf-8") as f:
        json.dump(pages, f, ensure_ascii=False, indent=2)
    print(f"[完成] 分页文本已保存: {pages_path} ({len(pages)} 页)")

    # 保存页码映射摘要（完整映射太大，只存摘要）
    mapping_summary = {
        "total_chars": len(char_page_mapping),
        "total_pages": max(char_page_mapping) if char_page_mapping else 0,
        "page_char_counts": {},
    }
    for page_num in set(char_page_mapping):
        mapping_summary["page_char_counts"][str(page_num)] = char_page_mapping.count(page_num)

    mapping_path = os.path.join(output_dir, f"{base}_mapping.json")
    with open(mapping_path, "w", encoding="utf-8") as f:
        json.dump(mapping_summary, f, ensure_ascii=False, indent=2)

    return full_path, pages_path


def main():
    parser = argparse.ArgumentParser(description="PyPDF2 基础 PDF 解析器")
    parser.add_argument("--pdf", required=True, help="PDF 文件路径")
    parser.add_argument("--output_dir", default="./data", help="输出目录（默认 ./data）")
    args = parser.parse_args()

    if not os.path.exists(args.pdf):
        print(f"[错误] PDF 文件不存在: {args.pdf}")
        sys.exit(1)

    pdf_name = os.path.basename(args.pdf)
    print(f"[开始] 使用 PyPDF2 解析: {pdf_name}")

    full_text, pages, char_page_mapping = extract_text_with_pages(args.pdf)

    total_pages = len(pages)
    non_empty = sum(1 for p in pages if p["char_count"] > 0)
    print(f"[统计] 共 {total_pages} 页，其中 {non_empty} 页包含文本，总计 {len(full_text)} 字符")

    full_path, pages_path = save_extracted_text(
        full_text, pages, char_page_mapping, args.output_dir, pdf_name
    )

    # 输出结果摘要供 Agent 使用
    result = {
        "status": "success",
        "parser": "PyPDF2",
        "pdf_file": args.pdf,
        "total_pages": total_pages,
        "pages_with_text": non_empty,
        "total_chars": len(full_text),
        "output_full_text": full_path,
        "output_pages": pages_path,
    }
    print(f"\n[结果] {json.dumps(result, ensure_ascii=False, indent=2)}")


if __name__ == "__main__":
    main()
