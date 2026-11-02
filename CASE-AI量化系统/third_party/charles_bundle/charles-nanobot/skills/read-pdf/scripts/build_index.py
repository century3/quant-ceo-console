#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FAISS 向量索引构建器

功能：将解析后的文本进行切片、向量化，构建 FAISS 索引。
支持 DashScope text-embedding-v4 模型和混合检索（BM25 + Vector）。
索引构建后可由 query_report.py 进行 RAG 问答。

用法：
    python build_index.py --text_dir <文本目录> --index_dir <索引保存目录>
    python build_index.py --text_file <单个文本文件> --index_dir <索引保存目录>
"""

import argparse
import json
import os
import pickle
import re
import sys
from typing import List, Tuple

import numpy as np
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_community.embeddings import DashScopeEmbeddings
from langchain_community.vectorstores import FAISS


DASHSCOPE_API_KEY = os.getenv("DASHSCOPE_API_KEY")


def load_text_files(text_dir: str = None, text_file: str = None) -> Tuple[str, dict]:
    """
    加载文本文件。

    Args:
        text_dir: 文本目录，读取其中所有 *_full.txt 文件
        text_file: 单个文本文件路径

    Returns:
        full_text: 合并后的全文
        page_info_raw: 页码信息（如果有对应的 _pages.json）
    """
    full_text = ""
    page_data = []

    if text_file:
        with open(text_file, "r", encoding="utf-8") as f:
            full_text = f.read()
        # 尝试加载对应的 pages.json
        base = os.path.splitext(text_file)[0]
        # 去掉 _full 后缀
        if base.endswith("_full"):
            base = base[:-5]
        pages_json = f"{base}_pages.json"
        if os.path.exists(pages_json):
            with open(pages_json, "r", encoding="utf-8") as f:
                page_data = json.load(f)
    elif text_dir:
        txt_files = sorted([
            f for f in os.listdir(text_dir)
            if f.endswith("_full.txt")
        ])
        if not txt_files:
            txt_files = sorted([
                f for f in os.listdir(text_dir)
                if f.endswith(".txt")
            ])
        for txt_file in txt_files:
            path = os.path.join(text_dir, txt_file)
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
            full_text += content + "\n\n"
            print(f"  -> 已加载: {txt_file} ({len(content)} 字符)")

        # 尝试加载 pages.json
        json_files = [f for f in os.listdir(text_dir) if f.endswith("_pages.json")]
        for jf in json_files:
            with open(os.path.join(text_dir, jf), "r", encoding="utf-8") as f:
                page_data.extend(json.load(f))

    return full_text, page_data


def extract_page_from_comment(text: str) -> int:
    """从 OCR 输出的 HTML 注释中提取页码"""
    match = re.search(r"<!--\s*第\s*(\d+)\s*页\s*-->", text)
    if match:
        return int(match.group(1))
    return -1


def chunk_text_with_pages(
    full_text: str,
    page_data: list,
    chunk_size: int = 800,
    chunk_overlap: int = 150,
) -> Tuple[List[str], dict]:
    """
    将文本切片并建立每个 chunk 到页码的映射。

    Args:
        full_text: 完整文本
        page_data: 分页数据（来自 parse_pdf 的输出）
        chunk_size: 切片大小
        chunk_overlap: 切片重叠

    Returns:
        chunks: 文本切片列表
        chunk_page_map: chunk 内容 -> 页码 的映射
    """
    text_splitter = RecursiveCharacterTextSplitter(
        separators=["\n\n", "\n", "。", ".", " ", ""],
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        length_function=len,
    )

    chunks = text_splitter.split_text(full_text)
    print(f"[切片] 文本被分割成 {len(chunks)} 个块 (chunk_size={chunk_size}, overlap={chunk_overlap})")

    # 建立 chunk -> 页码的映射
    chunk_page_map = {}

    if page_data:
        # 方式1：基于分页数据的字符偏移量计算
        page_boundaries = []
        offset = 0
        for p in page_data:
            page_boundaries.append({
                "page": p.get("page", -1),
                "start": offset,
                "end": offset + p.get("char_count", 0),
            })
            offset += p.get("char_count", 0)

        for chunk in chunks:
            chunk_start = full_text.find(chunk[:100])
            if chunk_start >= 0:
                for pb in page_boundaries:
                    if pb["start"] <= chunk_start < pb["end"]:
                        chunk_page_map[chunk] = pb["page"]
                        break
                else:
                    chunk_page_map[chunk] = -1
            else:
                # 尝试从 OCR 注释中提取页码
                page_from_comment = extract_page_from_comment(chunk)
                chunk_page_map[chunk] = page_from_comment
    else:
        # 方式2：从 OCR 输出的 HTML 注释中提取页码
        current_page = 1
        for chunk in chunks:
            page_from_comment = extract_page_from_comment(chunk)
            if page_from_comment > 0:
                current_page = page_from_comment
            chunk_page_map[chunk] = current_page

    return chunks, chunk_page_map


def build_faiss_index(
    chunks: List[str],
    chunk_page_map: dict,
    index_dir: str,
    embedding_model: str = "text-embedding-v4",
) -> FAISS:
    """
    构建 FAISS 向量索引并保存。

    Args:
        chunks: 文本切片列表
        chunk_page_map: chunk -> 页码映射
        index_dir: 索引保存目录
        embedding_model: Embedding 模型名称

    Returns:
        vectorstore: FAISS 向量存储对象
    """
    if not DASHSCOPE_API_KEY:
        print("[错误] 请设置环境变量 DASHSCOPE_API_KEY")
        sys.exit(1)

    embeddings = DashScopeEmbeddings(
        model=embedding_model,
        dashscope_api_key=DASHSCOPE_API_KEY,
    )

    print(f"[索引] 正在使用 {embedding_model} 生成向量...")
    vectorstore = FAISS.from_texts(chunks, embeddings)
    vectorstore.page_info = chunk_page_map
    print(f"[索引] FAISS 索引构建完成，共 {len(chunks)} 个向量")

    # 保存索引
    os.makedirs(index_dir, exist_ok=True)
    vectorstore.save_local(index_dir)
    print(f"[保存] FAISS 索引已保存: {index_dir}")

    # 保存页码映射
    with open(os.path.join(index_dir, "page_info.pkl"), "wb") as f:
        pickle.dump(chunk_page_map, f)

    # 保存 chunks（供混合检索使用）
    with open(os.path.join(index_dir, "chunks.pkl"), "wb") as f:
        pickle.dump(chunks, f)

    # 保存构建元数据
    meta = {
        "total_chunks": len(chunks),
        "embedding_model": embedding_model,
        "chunk_size": 800,
        "chunk_overlap": 150,
        "pages_mapped": sum(1 for v in chunk_page_map.values() if v > 0),
    }
    with open(os.path.join(index_dir, "index_meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    return vectorstore


def main():
    parser = argparse.ArgumentParser(description="FAISS 向量索引构建器")
    parser.add_argument("--text_dir", default=None, help="文本文件目录")
    parser.add_argument("--text_file", default=None, help="单个文本文件路径")
    parser.add_argument("--index_dir", default="./data/vector_db", help="索引保存目录")
    parser.add_argument("--chunk_size", type=int, default=800, help="切片大小（默认 800）")
    parser.add_argument("--chunk_overlap", type=int, default=150, help="切片重叠（默认 150）")
    parser.add_argument("--embedding_model", default="text-embedding-v4", help="Embedding 模型")
    args = parser.parse_args()

    if not args.text_dir and not args.text_file:
        print("[错误] 请指定 --text_dir 或 --text_file")
        sys.exit(1)

    print("[开始] 构建 FAISS 向量索引")

    # 加载文本
    full_text, page_data = load_text_files(args.text_dir, args.text_file)
    if not full_text.strip():
        print("[错误] 未加载到任何文本内容")
        sys.exit(1)
    print(f"[加载] 共加载 {len(full_text)} 字符")

    # 切片
    chunks, chunk_page_map = chunk_text_with_pages(
        full_text, page_data, args.chunk_size, args.chunk_overlap
    )

    # 构建索引
    vectorstore = build_faiss_index(
        chunks, chunk_page_map, args.index_dir, args.embedding_model
    )

    result = {
        "status": "success",
        "index_dir": args.index_dir,
        "total_chunks": len(chunks),
        "embedding_model": args.embedding_model,
    }
    print(f"\n[结果] {json.dumps(result, ensure_ascii=False, indent=2)}")


if __name__ == "__main__":
    main()
