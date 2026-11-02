#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
跨公司对比分析工具

功能: 从统一 FAISS 索引中检索不同公司的研报/财报内容，
     由 LLM 横向对比分析指定主题的差异。

用法:
    python cross_company.py --stocks 688981,600519 --topic "经营状况和盈利能力"
    python cross_company.py --stocks 688981,600519 --topic "核心竞争力" --index_dir data/vector_store
"""

import argparse
import io
import json
import os
import sys

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

from langchain_community.embeddings import DashScopeEmbeddings
from langchain_community.llms import Tongyi
from langchain_community.vectorstores import FAISS


DASHSCOPE_API_KEY = os.getenv("DASHSCOPE_API_KEY")

STOCK_NAMES = {
    "688981": "中芯国际",
    "600519": "贵州茅台",
    "002594": "比亚迪",
    "300750": "宁德时代",
    "000858": "五粮液",
}


def _load_faiss_safe(index_dir: str, embeddings):
    """FAISS C++ fopen 兼容 Windows 非 ASCII 路径"""
    import shutil
    import tempfile

    try:
        return FAISS.load_local(
            index_dir, embeddings, allow_dangerous_deserialization=True
        )
    except RuntimeError:
        tmp_dir = tempfile.mkdtemp(prefix="faiss_load_")
        try:
            for f in os.listdir(index_dir):
                if f.endswith((".faiss", ".pkl")):
                    shutil.copy2(os.path.join(index_dir, f), tmp_dir)
            return FAISS.load_local(
                tmp_dir, embeddings, allow_dangerous_deserialization=True
            )
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)


def load_unified_index(index_dir: str):
    """加载统一 FAISS 索引"""
    if not DASHSCOPE_API_KEY:
        print("[错误] 请设置环境变量 DASHSCOPE_API_KEY")
        sys.exit(1)

    embeddings = DashScopeEmbeddings(
        model="text-embedding-v4",
        dashscope_api_key=DASHSCOPE_API_KEY,
    )

    vectorstore = _load_faiss_safe(index_dir, embeddings)

    chunks_json_path = os.path.join(index_dir, "chunks.json")
    chunks_metadata = {}
    if os.path.exists(chunks_json_path):
        with open(chunks_json_path, "r", encoding="utf-8") as f:
            chunks_data = json.load(f)
        for c in chunks_data:
            chunks_metadata[c["text"]] = c.get("metadata", {})

    return vectorstore, chunks_metadata


def search_for_company(vectorstore, chunks_metadata: dict, stock_code: str, topic: str, top_k: int = 6):
    """检索某公司关于特定主题的内容"""
    stock_name = STOCK_NAMES.get(stock_code, stock_code)
    query = f"{stock_name} {topic}"

    docs = vectorstore.similarity_search(query, k=top_k * 3)

    filtered = []
    for doc in docs:
        meta = chunks_metadata.get(doc.page_content, {})
        if meta.get("stock_code") == stock_code:
            filtered.append({
                "text": doc.page_content,
                "filename": meta.get("filename", ""),
                "report_type": meta.get("report_type", ""),
                "source": meta.get("source", ""),
            })
        if len(filtered) >= top_k:
            break

    return filtered


def cross_company_analysis(
    stock_codes: list, topic: str, vectorstore, chunks_metadata: dict, model: str
):
    """执行跨公司对比分析"""
    company_data = {}

    for code in stock_codes:
        name = STOCK_NAMES.get(code, code)
        results = search_for_company(vectorstore, chunks_metadata, code, topic)

        if results:
            company_data[code] = {
                "name": name,
                "results": results,
            }
            print(f"  {name}({code}): 检索到 {len(results)} 条相关内容")
        else:
            print(f"  {name}({code}): 未找到相关内容")

    if len(company_data) < 2:
        print("[警告] 至少需要 2 家公司的数据才能对比")
        return None

    # 构建对比上下文
    context_parts = []
    for code, data in company_data.items():
        texts = "\n".join([r["text"] for r in data["results"][:4]])
        sources = list(set(r["filename"] for r in data["results"] if r["filename"]))
        source_str = ", ".join(sources[:3]) if sources else "未知来源"
        context_parts.append(
            f"### {data['name']}({code})\n来源: {source_str}\n\n{texts}"
        )

    context = "\n\n---\n\n".join(context_parts)
    names = [company_data[c]["name"] for c in company_data]

    prompt = f"""你是一位专业的投资分析师。请根据以下从各公司财报/研报中检索到的内容，
对 {' 和 '.join(names)} 进行横向对比分析。

对比主题: {topic}

要求:
1. 按对比维度逐一分析各公司的表现
2. 明确数据差异和相对优劣
3. 分析各自的竞争优势和风险点
4. 给出总体对比结论
5. 如果两家公司行业不同，请从各自行业视角分析

检索内容:
{context}"""

    llm = Tongyi(model_name=model, dashscope_api_key=DASHSCOPE_API_KEY)
    answer = llm.invoke(prompt)

    return {
        "stocks": {c: d["name"] for c, d in company_data.items()},
        "topic": topic,
        "analysis": answer,
    }


def main():
    parser = argparse.ArgumentParser(description="跨公司对比分析工具")
    parser.add_argument("--stocks", required=True, help="股票代码(逗号分隔, 如 688981,600519)")
    parser.add_argument("--topic", default="经营状况和盈利能力", help="对比主题")
    parser.add_argument("--index_dir", default="data/vector_store", help="统一索引目录")
    parser.add_argument("--model", default="qwen3.8-max", help="LLM 模型")
    parser.add_argument("--top_k", type=int, default=6, help="每家公司检索的文档数")
    args = parser.parse_args()

    if not os.path.exists(args.index_dir):
        print(f"[错误] 索引目录不存在: {args.index_dir}")
        print("[提示] 请先运行 python preprocess.py 构建统一索引")
        sys.exit(1)

    stock_codes = [s.strip() for s in args.stocks.split(",")]
    names = [STOCK_NAMES.get(c, c) for c in stock_codes]

    print(f"[开始] 跨公司对比: {' vs '.join(names)}")
    print(f"[主题] {args.topic}")

    vectorstore, chunks_metadata = load_unified_index(args.index_dir)

    result = cross_company_analysis(
        stock_codes, args.topic, vectorstore, chunks_metadata, args.model
    )

    if result:
        print(f"\n{'=' * 60}")
        print(f"[跨公司对比] {' vs '.join(result['stocks'].values())}")
        print(f"[主题] {result['topic']}")
        print(f"{'=' * 60}")
        print(result["analysis"])
        print(f"{'=' * 60}")

        output = {
            "status": "success",
            "stocks": list(result["stocks"].keys()),
            "topic": args.topic,
        }
        print(f"\n[结果] {json.dumps(output, ensure_ascii=False)}")
    else:
        print("[结果] 未能完成分析, 请确认索引中包含相关公司的报告")


if __name__ == "__main__":
    main()
