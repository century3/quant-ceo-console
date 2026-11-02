#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
跨期对比分析工具

功能: 从统一 FAISS 索引中检索同一公司不同时期的财报内容，
     由 LLM 自动对比分析关键指标和经营变化。

用法:
    python cross_period.py --stock 688981 --topics "营收,净利润,毛利率"
    python cross_period.py --stock 600519 --topics "经营情况" --index_dir data/vector_store
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

    # 加载 chunks 元数据
    chunks_json_path = os.path.join(index_dir, "chunks.json")
    chunks_metadata = {}
    if os.path.exists(chunks_json_path):
        with open(chunks_json_path, "r", encoding="utf-8") as f:
            chunks_data = json.load(f)
        for c in chunks_data:
            chunks_metadata[c["text"]] = c.get("metadata", {})

    return vectorstore, chunks_metadata


def search_by_stock_and_topic(
    vectorstore, chunks_metadata: dict, stock_code: str, topic: str, top_k: int = 8
):
    """按股票代码过滤并检索相关内容"""
    docs = vectorstore.similarity_search(topic, k=top_k * 3)

    # 按股票代码过滤
    filtered = []
    for doc in docs:
        meta = chunks_metadata.get(doc.page_content, {})
        if meta.get("stock_code") == stock_code:
            filtered.append({
                "text": doc.page_content,
                "filename": meta.get("filename", ""),
                "report_type": meta.get("report_type", ""),
                "publish_date": meta.get("publish_date", ""),
                "page": meta.get("page", -1),
            })
        if len(filtered) >= top_k:
            break

    return filtered


def cross_period_analysis(
    stock_code: str, topics: list, vectorstore, chunks_metadata: dict, model: str
):
    """执行跨期对比分析"""
    stock_name = STOCK_NAMES.get(stock_code, stock_code)

    # 搜索所有相关内容
    all_results = []
    for topic in topics:
        query = f"{stock_name} {topic}"
        results = search_by_stock_and_topic(vectorstore, chunks_metadata, stock_code, query)
        all_results.extend(results)

    if not all_results:
        print(f"[提示] 未找到 {stock_name}({stock_code}) 的相关内容")
        return None

    # 按报告类型/日期分组
    by_report = {}
    for r in all_results:
        key = r["filename"] or r["report_type"] or "unknown"
        if key not in by_report:
            by_report[key] = {
                "filename": r["filename"],
                "report_type": r["report_type"],
                "publish_date": r["publish_date"],
                "texts": [],
            }
        if r["text"] not in [t for t in by_report[key]["texts"]]:
            by_report[key]["texts"].append(r["text"])

    print(f"[检索] 找到 {len(all_results)} 条相关内容, 来自 {len(by_report)} 份报告")

    # 构建 LLM 分析的上下文
    context_parts = []
    for key, data in by_report.items():
        header = f"[来源: {data['filename']}]"
        if data["publish_date"]:
            header += f" (发布日期: {data['publish_date']})"
        content = "\n".join(data["texts"][:5])
        context_parts.append(f"{header}\n{content}")

    context = "\n\n---\n\n".join(context_parts)
    topics_str = "、".join(topics)

    prompt = f"""你是一位专业的投资分析师。请根据以下从不同时期的财报/研报中检索到的内容，
对 {stock_name}({stock_code}) 进行跨期对比分析。

对比维度: {topics_str}

要求:
1. 明确标注各时期的关键数据
2. 分析核心指标的变化趋势和幅度
3. 解读变化背后的原因(如果能从内容中推断)
4. 给出对未来趋势的判断
5. 如果信息不足以做完整对比，请说明

检索内容:
{context}"""

    llm = Tongyi(model_name=model, dashscope_api_key=DASHSCOPE_API_KEY)
    answer = llm.invoke(prompt)

    return {
        "stock_code": stock_code,
        "stock_name": stock_name,
        "topics": topics,
        "reports_used": list(by_report.keys()),
        "analysis": answer,
    }


def main():
    parser = argparse.ArgumentParser(description="跨期对比分析工具")
    parser.add_argument("--stock", required=True, help="股票代码(如 688981)")
    parser.add_argument("--topics", default="营收,净利润,毛利率,经营情况", help="对比维度(逗号分隔)")
    parser.add_argument("--index_dir", default="data/vector_store", help="统一索引目录")
    parser.add_argument("--model", default="qwen3.8-max", help="LLM 模型")
    parser.add_argument("--top_k", type=int, default=8, help="每个维度检索的文档数")
    args = parser.parse_args()

    if not os.path.exists(args.index_dir):
        print(f"[错误] 索引目录不存在: {args.index_dir}")
        print("[提示] 请先运行 python preprocess.py 构建统一索引")
        sys.exit(1)

    topics = [t.strip() for t in args.topics.split(",")]
    stock_name = STOCK_NAMES.get(args.stock, args.stock)

    print(f"[开始] {stock_name}({args.stock}) 跨期对比分析")
    print(f"[维度] {', '.join(topics)}")

    vectorstore, chunks_metadata = load_unified_index(args.index_dir)

    result = cross_period_analysis(
        args.stock, topics, vectorstore, chunks_metadata, args.model
    )

    if result:
        print(f"\n{'=' * 60}")
        print(f"[跨期对比分析] {result['stock_name']}")
        print(f"[参考报告] {', '.join(result['reports_used'])}")
        print(f"{'=' * 60}")
        print(result["analysis"])
        print(f"{'=' * 60}")

        output = {
            "status": "success",
            "stock": args.stock,
            "topics": topics,
            "reports_count": len(result["reports_used"]),
        }
        print(f"\n[结果] {json.dumps(output, ensure_ascii=False)}")
    else:
        print("[结果] 未能完成分析, 请确认索引中包含该公司的报告")


if __name__ == "__main__":
    main()
