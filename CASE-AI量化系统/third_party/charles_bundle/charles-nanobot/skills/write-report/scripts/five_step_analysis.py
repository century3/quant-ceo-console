#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
国泰君安"五步法"分析引擎

功能：按照五步法框架（信息差->逻辑差->预期差->催化剂->结论），
      结合 RAG 财报数据，逐步调用 LLM 生成深度分析。
      每一步的输出作为下一步的输入，形成递进式分析链。

依赖：需要先通过 read-pdf Skill 构建好 FAISS 索引。

用法：
    python five_step_analysis.py --index_dir <索引目录> --stock_name <公司名称> --output_dir <输出目录>
    python five_step_analysis.py --index_dir data/vector_db --stock_name 贵州茅台 --output_dir output/
    python five_step_analysis.py --index_dir data/vector_db --stock_name 贵州茅台 --focus catalyst
"""

import argparse
import json
import os
import pickle
import sys
from datetime import datetime
from typing import List, Tuple

from langchain_community.embeddings import DashScopeEmbeddings
from langchain_community.llms import Tongyi
from langchain_community.vectorstores import FAISS

# 导入五步法 Prompt 模板
sys.path.insert(0, os.path.dirname(__file__))
from prompts import FIVE_STEP_CONFIG


DASHSCOPE_API_KEY = os.getenv("DASHSCOPE_API_KEY")


def load_rag_index(index_dir: str) -> Tuple[FAISS, dict]:
    """加载 FAISS 索引和页码映射"""
    if not DASHSCOPE_API_KEY:
        print("[错误] 请设置环境变量 DASHSCOPE_API_KEY")
        sys.exit(1)

    embeddings = DashScopeEmbeddings(
        model="text-embedding-v4",
        dashscope_api_key=DASHSCOPE_API_KEY,
    )

    vectorstore = FAISS.load_local(
        index_dir, embeddings, allow_dangerous_deserialization=True
    )

    page_info = {}
    page_info_path = os.path.join(index_dir, "page_info.pkl")
    if os.path.exists(page_info_path):
        with open(page_info_path, "rb") as f:
            page_info = pickle.load(f)

    return vectorstore, page_info


def rag_retrieve(
    vectorstore: FAISS,
    query: str,
    page_info: dict,
    top_k: int = 6,
) -> Tuple[str, List[int]]:
    """
    从 RAG 索引中检索相关内容。

    Returns:
        context: 拼接后的上下文文本
        source_pages: 来源页码列表
    """
    docs = vectorstore.similarity_search(query, k=top_k)

    context_parts = []
    source_pages = []

    for doc in docs:
        context_parts.append(doc.page_content)
        page = page_info.get(doc.page_content.strip(), -1)
        if page > 0:
            source_pages.append(page)

    context = "\n\n---\n\n".join(context_parts)
    return context, sorted(set(source_pages))


def run_single_step(
    step_config: dict,
    stock_name: str,
    vectorstore: FAISS,
    page_info: dict,
    llm,
    previous_analysis: str = "",
) -> dict:
    """
    执行五步法中的单个步骤。

    Args:
        step_config: 该步骤的配置（来自 FIVE_STEP_CONFIG）
        stock_name: 公司名称
        vectorstore: FAISS 索引
        page_info: 页码映射
        llm: LLM 实例
        previous_analysis: 前面步骤的分析结果

    Returns:
        步骤结果字典
    """
    step_num = step_config["step"]
    step_name = step_config["name"]

    print(f"\n{'='*60}")
    print(f"[Step {step_num}] {step_name}")
    print(f"{'='*60}")

    # RAG 检索相关数据
    rag_query = f"{stock_name} {step_config['rag_query']}"
    context, source_pages = rag_retrieve(vectorstore, rag_query, page_info)
    print(f"[RAG] 检索到 {len(context)} 字符的相关数据，来源页码: {source_pages}")

    # 构建 Prompt
    prompt = step_config["prompt_template"].format(
        stock_name=stock_name,
        context=context,
        previous_analysis=previous_analysis if previous_analysis else "（这是第一步分析，无前置分析结果）",
    )

    # 调用 LLM
    print(f"[LLM] 正在分析...")
    analysis = llm.invoke(prompt)
    print(f"[完成] 生成 {len(analysis)} 字符的分析结果")

    return {
        "step": step_num,
        "name": step_name,
        "name_en": step_config["name_en"],
        "analysis": analysis,
        "source_pages": source_pages,
        "rag_query": rag_query,
    }


def run_five_step_analysis(
    index_dir: str,
    stock_name: str,
    output_dir: str,
    model: str = "deepseek-v3",
    focus: str = None,
) -> dict:
    """
    执行完整的五步法分析。

    Args:
        index_dir: FAISS 索引目录
        stock_name: 公司名称
        output_dir: 输出目录
        model: LLM 模型名称
        focus: 聚焦某个步骤进行深入分析（可选）

    Returns:
        完整的分析结果
    """
    # 加载 RAG 索引
    print(f"[初始化] 加载 RAG 索引: {index_dir}")
    vectorstore, page_info = load_rag_index(index_dir)

    # 初始化 LLM
    llm = Tongyi(model_name=model, dashscope_api_key=DASHSCOPE_API_KEY)

    # 确定要执行的步骤
    if focus:
        step_configs = [s for s in FIVE_STEP_CONFIG if s["name_en"] == focus or s["name"] == focus]
        if not step_configs:
            print(f"[警告] 未找到步骤 '{focus}'，将执行全部五步")
            step_configs = FIVE_STEP_CONFIG
    else:
        step_configs = FIVE_STEP_CONFIG

    # 逐步执行分析
    results = []
    accumulated_analysis = ""

    for config in step_configs:
        step_result = run_single_step(
            config, stock_name, vectorstore, page_info, llm, accumulated_analysis
        )
        results.append(step_result)

        # 将当前步骤的分析结果累加，供下一步使用
        accumulated_analysis += f"\n\n### Step {config['step']}: {config['name']}\n{step_result['analysis']}"

    # 汇总所有来源页码
    all_pages = set()
    for r in results:
        all_pages.update(r["source_pages"])

    # 构建最终结果
    analysis_result = {
        "stock_name": stock_name,
        "analysis_date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "model": model,
        "index_dir": index_dir,
        "steps": results,
        "all_source_pages": sorted(all_pages),
    }

    # 保存结果
    os.makedirs(output_dir, exist_ok=True)
    output_file = os.path.join(output_dir, f"{stock_name}_analysis.json")
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(analysis_result, f, ensure_ascii=False, indent=2)
    print(f"\n[保存] 分析结果已保存: {output_file}")

    result_summary = {
        "status": "success",
        "stock_name": stock_name,
        "steps_completed": len(results),
        "output_file": output_file,
        "all_source_pages": sorted(all_pages),
    }
    print(f"\n[结果] {json.dumps(result_summary, ensure_ascii=False, indent=2)}")

    return analysis_result


def main():
    parser = argparse.ArgumentParser(description="国泰君安五步法分析引擎")
    parser.add_argument("--index_dir", required=True, help="FAISS 索引目录")
    parser.add_argument("--stock_name", required=True, help="公司名称（如：贵州茅台）")
    parser.add_argument("--output_dir", default="./output", help="输出目录（默认 ./output）")
    parser.add_argument("--model", default="deepseek-v3", help="LLM 模型（默认 deepseek-v3）")
    parser.add_argument(
        "--focus",
        default=None,
        choices=["information_gap", "logic_gap", "expectation_gap", "catalyst", "conclusion",
                 "信息差", "逻辑差", "预期差", "催化剂", "结论"],
        help="聚焦某个步骤深入分析",
    )
    args = parser.parse_args()

    if not os.path.exists(args.index_dir):
        print(f"[错误] 索引目录不存在: {args.index_dir}")
        print("[提示] 请先使用 read-pdf 技能解析财报并构建索引")
        sys.exit(1)

    run_five_step_analysis(
        index_dir=args.index_dir,
        stock_name=args.stock_name,
        output_dir=args.output_dir,
        model=args.model,
        focus=args.focus,
    )


if __name__ == "__main__":
    main()
