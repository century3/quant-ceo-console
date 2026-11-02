#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RAG 问答查询器

功能：基于 FAISS 向量索引，对财报内容进行检索增强生成（RAG）问答。
支持多查询扩展、BM25+向量混合检索、Rerank 精排，并追溯答案来源页码。
支持统一索引(preprocess.py 构建)和旧版单文档索引两种格式。

用法：
    python query_report.py --index_dir <索引目录> --query "营收和净利润是多少"
    python query_report.py --index_dir <索引目录> --query "主要风险因素" --top_k 6
    python query_report.py --index_dir data/vector_store --query "营收" --stock 688981
"""

import argparse
import io
import json
import os
import pickle
import sys

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
from typing import List, Set, Tuple

import jieba
from rank_bm25 import BM25Okapi
from langchain_community.embeddings import DashScopeEmbeddings
from langchain_community.llms import Tongyi
from langchain_community.vectorstores import FAISS
from langchain_core.documents import Document


DASHSCOPE_API_KEY = os.getenv("DASHSCOPE_API_KEY")


def _load_faiss_safe(index_dir: str, embeddings):
    """
    加载 FAISS 索引，兼容 Windows 非 ASCII 路径。
    FAISS C++ fopen 在 Windows 上可能无法处理中文路径，
    如果直接加载失败则通过临时目录中转。
    """
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


def load_index(index_dir: str, stock_filter: str = None) -> Tuple[FAISS, List[str], dict]:
    """
    加载 FAISS 索引、chunks 和页码映射。
    自动检测索引格式(统一索引 / 旧版单文档索引)。

    Args:
        index_dir: 索引目录
        stock_filter: 按股票代码过滤(仅统一索引有效)

    Returns:
        vectorstore: FAISS 向量存储
        chunks: 文本切片列表
        page_info: chunk -> 页码映射
    """
    if not DASHSCOPE_API_KEY:
        print("[错误] 请设置环境变量 DASHSCOPE_API_KEY")
        sys.exit(1)

    embeddings = DashScopeEmbeddings(
        model="text-embedding-v4",
        dashscope_api_key=DASHSCOPE_API_KEY,
    )

    vectorstore = _load_faiss_safe(index_dir, embeddings)

    # 检测索引格式: 统一索引有 chunks.json, 旧版索引有 chunks.pkl
    chunks_json_path = os.path.join(index_dir, "chunks.json")
    chunks_pkl_path = os.path.join(index_dir, "chunks.pkl")

    chunks = []
    page_info = {}

    if os.path.exists(chunks_json_path):
        # ---- 统一索引格式 (preprocess.py 构建) ----
        with open(chunks_json_path, "r", encoding="utf-8") as f:
            chunks_data = json.load(f)

        # 按股票代码过滤
        if stock_filter:
            chunks_data = [
                c for c in chunks_data
                if c.get("metadata", {}).get("stock_code", "") == stock_filter
            ]
            print(f"[过滤] 股票代码 {stock_filter}, 匹配 {len(chunks_data)} 个 chunks")

        chunks = [c["text"] for c in chunks_data]
        # 从 metadata 中提取页码映射
        for c in chunks_data:
            page = c.get("metadata", {}).get("page", -1)
            if page > 0:
                page_info[c["text"]] = page
        # 将完整 metadata 存到 vectorstore 上供 rag_answer 使用
        vectorstore._chunks_metadata = {
            c["text"]: c.get("metadata", {}) for c in chunks_data
        }

    elif os.path.exists(chunks_pkl_path):
        # ---- 旧版索引格式 (build_index.py 构建) ----
        with open(chunks_pkl_path, "rb") as f:
            chunks = pickle.load(f)

        page_info_path = os.path.join(index_dir, "page_info.pkl")
        if os.path.exists(page_info_path):
            with open(page_info_path, "rb") as f:
                page_info = pickle.load(f)

    else:
        # 无 chunks 文件，仅使用 FAISS 向量检索
        print("[提示] 未找到 chunks 文件，将仅使用向量检索(无 BM25 混合)")

    vectorstore.page_info = page_info

    return vectorstore, chunks, page_info


def generate_multi_queries(query: str, llm, num_queries: int = 3) -> List[str]:
    """使用 LLM 生成多个查询变体，从不同角度检索"""
    prompt = f"""你是一个AI助手，负责生成多个不同视角的搜索查询。
给定一个用户问题，生成{num_queries}个不同但相关的查询，以帮助检索更全面的信息。
每个查询应该从不同角度表达相同的信息需求。

原始问题: {query}

请直接输出{num_queries}个查询，每行一个，不要编号和其他内容:"""

    response = llm.invoke(prompt)
    queries = [q.strip() for q in response.strip().split("\n") if q.strip()]
    return [query] + queries[:num_queries]


class HybridRetriever:
    """混合检索器：BM25 关键词检索 + FAISS 向量语义检索"""

    def __init__(self, chunks: List[str], vectorstore: FAISS, alpha: float = 0.5):
        self.chunks = chunks
        self.vectorstore = vectorstore
        self.alpha = alpha

        tokenized = [list(jieba.cut(c)) for c in chunks]
        self.bm25 = BM25Okapi(tokenized)
        self.chunk_to_idx = {c: i for i, c in enumerate(chunks)}

    def search(self, query: str, k: int = 6) -> List[Document]:
        """执行混合检索"""
        # BM25 检索
        tokenized_query = list(jieba.cut(query))
        bm25_scores = self.bm25.get_scores(tokenized_query)
        max_bm25 = max(bm25_scores) if max(bm25_scores) > 0 else 1
        bm25_normalized = [s / max_bm25 for s in bm25_scores]

        # 向量检索
        vector_results = self.vectorstore.similarity_search_with_score(
            query, k=len(self.chunks)
        )
        vector_scores = {}
        max_dist = max(s for _, s in vector_results) if vector_results else 1
        for doc, dist in vector_results:
            idx = self.chunk_to_idx.get(doc.page_content)
            if idx is not None:
                vector_scores[idx] = 1 - (dist / max_dist) if max_dist > 0 else 0

        # 融合分数
        hybrid = []
        for idx in range(len(self.chunks)):
            combined = (
                self.alpha * vector_scores.get(idx, 0)
                + (1 - self.alpha) * bm25_normalized[idx]
            )
            hybrid.append((idx, combined))

        hybrid.sort(key=lambda x: x[1], reverse=True)

        results = []
        for idx, score in hybrid[:k]:
            doc = Document(
                page_content=self.chunks[idx],
                metadata={"hybrid_score": score},
            )
            results.append(doc)

        return results


def multi_query_hybrid_search(
    query: str,
    hybrid_retriever: HybridRetriever,
    llm,
    initial_k: int = 8,
) -> List[Document]:
    """多查询 + 混合检索，合并去重"""
    queries = generate_multi_queries(query, llm)
    print(f"[多查询] 生成 {len(queries)} 个查询变体")

    seen = set()
    candidates = []

    for q in queries:
        docs = hybrid_retriever.search(q, k=initial_k)
        for doc in docs:
            if doc.page_content not in seen:
                seen.add(doc.page_content)
                candidates.append(doc)

    print(f"[召回] 共召回 {len(candidates)} 个候选文档")
    return candidates


def rag_answer(
    query: str,
    docs: List[Document],
    page_info: dict,
    llm,
    vectorstore=None,
) -> Tuple[str, Set[int], List[dict]]:
    """
    基于检索到的文档生成回答，并返回来源页码和来源信息。

    Returns:
        answer: LLM 生成的回答
        source_pages: 来源页码集合
        source_details: 来源详情列表(统一索引时包含文档名、股票等)
    """
    context = "\n\n---\n\n".join([doc.page_content for doc in docs])

    prompt = f"""你是一位专业的投资分析师。请根据以下从财报中检索到的内容，回答用户的问题。

要求：
1. 仅基于提供的上下文回答，如果信息不足请明确说明
2. 涉及数字时保持原始精度
3. 用专业但易懂的语言回答
4. 如果涉及多个方面，请分点阐述

上下文（来自财报）：
{context}

问题：{query}"""

    answer = llm.invoke(prompt)

    # 收集来源信息
    source_pages = set()
    source_details = []
    chunks_metadata = getattr(vectorstore, "_chunks_metadata", {}) if vectorstore else {}

    for doc in docs:
        text = doc.page_content.strip()

        # 统一索引: 优先从 _chunks_metadata 获取完整信息
        meta = chunks_metadata.get(text, {})
        if meta:
            page = meta.get("page", -1)
            if page > 0:
                source_pages.add(page)
            source_details.append({
                "filename": meta.get("filename", ""),
                "stock_name": meta.get("stock_name", ""),
                "page": page,
                "report_type": meta.get("report_type", ""),
            })
        else:
            # 旧版索引: 从 page_info 获取页码
            page = page_info.get(text, -1)
            if page > 0:
                source_pages.add(page)

    return answer, source_pages, source_details


def main():
    parser = argparse.ArgumentParser(description="RAG 问答查询器")
    parser.add_argument("--index_dir", required=True, help="FAISS 索引目录")
    parser.add_argument("--query", required=True, help="查询问题")
    parser.add_argument("--top_k", type=int, default=6, help="返回文档数（默认 6）")
    parser.add_argument("--model", default="deepseek-v3", help="LLM 模型（默认 deepseek-v3）")
    parser.add_argument("--alpha", type=float, default=0.5, help="混合检索中向量权重（默认 0.5）")
    parser.add_argument("--stock", default=None, help="按股票代码过滤（如 688981），仅统一索引有效")
    args = parser.parse_args()

    if not os.path.exists(args.index_dir):
        print(f"[错误] 索引目录不存在: {args.index_dir}")
        sys.exit(1)

    print(f"[开始] RAG 问答")
    print(f"[问题] {args.query}")
    if args.stock:
        print(f"[过滤] 股票代码: {args.stock}")

    # 加载索引
    vectorstore, chunks, page_info = load_index(args.index_dir, stock_filter=args.stock)
    print(f"[加载] 索引包含 {len(chunks)} 个文本块")

    # 初始化 LLM
    llm = Tongyi(model_name=args.model, dashscope_api_key=DASHSCOPE_API_KEY)

    # 检索
    if chunks:
        hybrid = HybridRetriever(chunks, vectorstore, alpha=args.alpha)
        docs = multi_query_hybrid_search(args.query, hybrid, llm, initial_k=args.top_k)
        docs = docs[: args.top_k]
    else:
        search_kwargs = {"k": args.top_k}
        if args.stock:
            search_kwargs["filter"] = {"stock_code": args.stock}
        docs = vectorstore.similarity_search(args.query, **search_kwargs)

    # 生成回答
    answer, source_pages, source_details = rag_answer(
        args.query, docs, page_info, llm, vectorstore
    )

    # 输出结果
    print("\n" + "=" * 60)
    print(f"[回答]\n{answer}")
    print(f"\n[来源页码] {sorted(source_pages) if source_pages else '未知'}")

    # 统一索引时显示来源文档详情
    if source_details:
        seen_files = set()
        print("[来源文档]")
        for sd in source_details:
            fn = sd.get("filename", "")
            if fn and fn not in seen_files:
                seen_files.add(fn)
                stock = sd.get("stock_name", "")
                rtype = sd.get("report_type", "")
                print(f"  - {fn} ({stock} {rtype})")

    print("=" * 60)

    result = {
        "status": "success",
        "query": args.query,
        "answer": answer,
        "source_pages": sorted(source_pages),
        "docs_used": len(docs),
    }
    if source_details:
        result["source_files"] = list({sd.get("filename", "") for sd in source_details if sd.get("filename")})
    print(f"\n[结果JSON] {json.dumps(result, ensure_ascii=False, indent=2)}")


if __name__ == "__main__":
    main()
