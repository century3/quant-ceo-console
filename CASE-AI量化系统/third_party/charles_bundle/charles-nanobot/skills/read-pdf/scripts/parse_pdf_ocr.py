#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
多模态 PDF 解析器（VL 大模型方案）

功能：使用多模态大模型（默认 qwen3.8-flash）解析 PDF 中的复杂表格和图表，
      输出结构化 Markdown 格式。也支持 DeepSeek-OCR-2 等其他视觉模型。
适用于：含复杂表格的财务报表（资产负债表、利润表、现金流量表）、
       多栏布局的年报。

默认模型 qwen3.8-flash 特点：
- 通义千问多模态模型，支持图文理解
- 通过 DashScope API 调用，与项目其他组件共用同一 API Key
- 支持复杂表格识别和结构化输出

也可切换为 DeepSeek-OCR-2（需单独配置 DEEPSEEK_API_KEY）：
- 3B 参数视觉语言模型，OmniDocBench v1.5 达到 91.09%
- 擅长复杂表格（合并单元格、隐藏边框）

用法：
    python parse_pdf_ocr.py --pdf <PDF文件路径> --output_dir <输出目录>
    python parse_pdf_ocr.py --pdf <PDF文件路径> --output_dir <输出目录> --pages 1,2,3
    python parse_pdf_ocr.py --pdf <PDF文件路径> --model deepseek-ocr-2
"""

import argparse
import base64
import json
import os
import sys
from pathlib import Path

from openai import OpenAI


def pdf_page_to_base64(pdf_path: str, page_num: int) -> str:
    """
    将 PDF 的指定页转换为 base64 编码的图片。

    使用 PyPDF2 提取页面后通过 fitz (PyMuPDF) 渲染为图片。
    如果 PyMuPDF 不可用，则回退到逐页截取方案。
    """
    try:
        import fitz  # PyMuPDF
    except ImportError:
        print("[警告] 未安装 PyMuPDF，请执行: pip install PyMuPDF")
        print("[提示] 回退到使用 pdf2image 方案")
        return _pdf_page_to_base64_fallback(pdf_path, page_num)

    doc = fitz.open(pdf_path)
    if page_num < 1 or page_num > len(doc):
        doc.close()
        return ""

    page = doc[page_num - 1]
    # 使用 1.5x 分辨率渲染，在清晰度与文件大小间取平衡
    # 2x 分辨率图片可能过大导致 API 超时
    mat = fitz.Matrix(1.5, 1.5)
    pix = page.get_pixmap(matrix=mat)
    img_bytes = pix.tobytes("png")
    doc.close()

    return base64.b64encode(img_bytes).decode("utf-8")


def _pdf_page_to_base64_fallback(pdf_path: str, page_num: int) -> str:
    """回退方案：使用 pdf2image 将 PDF 页面转为图片"""
    try:
        from pdf2image import convert_from_path
        import io

        images = convert_from_path(
            pdf_path,
            first_page=page_num,
            last_page=page_num,
            dpi=200,
        )
        if not images:
            return ""

        buf = io.BytesIO()
        images[0].save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode("utf-8")
    except ImportError:
        print("[错误] 需要安装 PyMuPDF 或 pdf2image 之一")
        sys.exit(1)


def ocr_page_with_vl_model(
    client: OpenAI,
    image_base64: str,
    page_num: int,
    model: str = "qwen3.8-flash",
) -> str:
    """
    调用多模态大模型 API 对单页图片进行结构化解析。

    支持的模型：
    - qwen3.8-flash（默认，DashScope）
    - qwen3.8-flash（更强，DashScope）
    - deepseek-ocr-2（DeepSeek API）

    Args:
        client: OpenAI 兼容客户端
        image_base64: 页面图片的 base64 编码
        page_num: 页码（用于提示词）
        model: 模型名称

    Returns:
        解析后的 Markdown 文本
    """
    response = client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "请将这张财务报表/研报图片中的所有内容转换为结构化的 Markdown 格式。"
                            "要求：\n"
                            "1. 表格使用 Markdown 表格语法，保留所有行列\n"
                            "2. 数字保持原始精度，不要四舍五入\n"
                            "3. 保留标题、注释、脚注等所有文字内容\n"
                            "4. 合并单元格用适当方式表示\n"
                            "5. 如果有图表，用文字描述图表内容和关键数据\n"
                            f"6. 这是第 {page_num} 页的内容"
                        ),
                    },
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/png;base64,{image_base64}",
                        },
                    },
                ],
            }
        ],
        max_tokens=4096,
        extra_body={"enable_thinking": False},
    )

    return response.choices[0].message.content


def parse_pdf_with_ocr(
    pdf_path: str,
    output_dir: str,
    pages: list = None,
    api_key: str = None,
    base_url: str = None,
    model: str = None,
) -> dict:
    """
    使用多模态大模型解析 PDF。

    默认使用 qwen3.8-flash（DashScope），也支持 deepseek-ocr-2。

    Args:
        pdf_path: PDF 文件路径
        output_dir: 输出目录
        pages: 指定页码列表，None 则解析全部
        api_key: API Key（默认从环境变量读取）
        base_url: API Base URL（默认从环境变量读取）
        model: 模型名称（默认 qwen3.8-flash）

    Returns:
        解析结果摘要
    """
    # 初始化 API 客户端
    # 默认使用 DashScope（qwen3.8-flash），也支持 DeepSeek API
    if api_key is None:
        api_key = os.getenv("DASHSCOPE_API_KEY") or os.getenv("DEEPSEEK_API_KEY")
    if model is None:
        model = os.getenv("OCR_MODEL", "qwen3.8-flash")
    if base_url is None:
        # 根据模型名称自动选择 API 端点
        if model.startswith("deepseek"):
            base_url = "https://api.deepseek.com/v1"
        else:
            base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"

    if not api_key:
        print("[错误] 请设置环境变量 DASHSCOPE_API_KEY")
        sys.exit(1)

    client = OpenAI(api_key=api_key, base_url=base_url)

    # 获取 PDF 总页数
    try:
        import fitz
        doc = fitz.open(pdf_path)
        total_pages = len(doc)
        doc.close()
    except ImportError:
        from PyPDF2 import PdfReader
        reader = PdfReader(pdf_path)
        total_pages = len(reader.pages)

    # 确定要解析的页码
    if pages:
        target_pages = [p for p in pages if 1 <= p <= total_pages]
    else:
        target_pages = list(range(1, total_pages + 1))

    print(f"[开始] 多模态解析({model}): {os.path.basename(pdf_path)}")
    print(f"[信息] 共 {total_pages} 页，将解析 {len(target_pages)} 页")

    os.makedirs(output_dir, exist_ok=True)
    pdf_name = os.path.splitext(os.path.basename(pdf_path))[0]

    all_pages_md = []
    page_results = []

    for i, page_num in enumerate(target_pages):
        print(f"[进度] 正在解析第 {page_num}/{total_pages} 页 ({i+1}/{len(target_pages)})")

        # 将 PDF 页面转为图片
        img_b64 = pdf_page_to_base64(pdf_path, page_num)
        if not img_b64:
            print(f"[警告] 第 {page_num} 页转换图片失败，跳过")
            continue

        # 调用 OCR API
        try:
            md_text = ocr_page_with_vl_model(client, img_b64, page_num, model)
            page_md = f"\n\n<!-- 第 {page_num} 页 -->\n\n{md_text}"
            all_pages_md.append(page_md)
            page_results.append({
                "page": page_num,
                "char_count": len(md_text),
                "has_table": "|" in md_text,
            })
            print(f"  -> 提取 {len(md_text)} 字符" + ("，包含表格" if "|" in md_text else ""))
        except Exception as e:
            print(f"[错误] 第 {page_num} 页解析失败: {e}")
            page_results.append({
                "page": page_num,
                "char_count": 0,
                "error": str(e),
            })

    # 合并保存全文 Markdown
    full_md = "\n".join(all_pages_md)
    md_path = os.path.join(output_dir, f"{pdf_name}_ocr.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(full_md)
    print(f"[完成] OCR 结果已保存: {md_path}")

    # 同时保存为 txt（供 build_index.py 使用）
    txt_path = os.path.join(output_dir, f"{pdf_name}_full.txt")
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(full_md)

    # 保存分页结果元数据
    meta_path = os.path.join(output_dir, f"{pdf_name}_ocr_pages.json")
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(page_results, f, ensure_ascii=False, indent=2)

    result = {
        "status": "success",
        "parser": f"VL-Model({model})",
        "model": model,
        "pdf_file": pdf_path,
        "total_pages": total_pages,
        "parsed_pages": len([p for p in page_results if p.get("char_count", 0) > 0]),
        "total_chars": len(full_md),
        "tables_found": sum(1 for p in page_results if p.get("has_table")),
        "output_markdown": md_path,
        "output_full_text": txt_path,
        "output_meta": meta_path,
    }
    print(f"\n[结果] {json.dumps(result, ensure_ascii=False, indent=2)}")
    return result


def main():
    parser = argparse.ArgumentParser(description="多模态 PDF 解析器（默认 qwen3.8-flash）")
    parser.add_argument("--pdf", required=True, help="PDF 文件路径")
    parser.add_argument("--output_dir", default="./data", help="输出目录（默认 ./data）")
    parser.add_argument(
        "--pages",
        default=None,
        help="指定解析的页码，逗号分隔（如 10,11,12）。不指定则解析全部",
    )
    parser.add_argument("--model", default=None, help="多模态模型名称（默认 qwen3.8-flash，也支持 deepseek-ocr-2）")
    args = parser.parse_args()

    if not os.path.exists(args.pdf):
        print(f"[错误] PDF 文件不存在: {args.pdf}")
        sys.exit(1)

    pages = None
    if args.pages:
        pages = [int(p.strip()) for p in args.pages.split(",")]

    parse_pdf_with_ocr(
        pdf_path=args.pdf,
        output_dir=args.output_dir,
        pages=pages,
        model=args.model,
    )


if __name__ == "__main__":
    main()
