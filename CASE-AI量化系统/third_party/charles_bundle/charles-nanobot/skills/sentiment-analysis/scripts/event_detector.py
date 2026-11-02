#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
事件识别与交易信号生成器

功能：从新闻中识别重大事件（资产重组、业绩预告等），
      根据事件类型生成交易信号和操作建议。

事件分类体系：
- 利好事件：资产重组、回购、业绩预增、股权激励、大额订单
- 利空事件：业绩预减、违规处罚、股东减持、商誉减值
- 政策事件：降准降息、产业政策、监管新规

用法：
    python event_detector.py --news_file data/002594_news.json --output_dir output/
"""

import argparse
import json
import os
import sys
from typing import List

from openai import OpenAI


DASHSCOPE_API_KEY = os.getenv("DASHSCOPE_API_KEY")

# 事件分类关键词库
EVENT_KEYWORDS = {
    "利好": {
        "资产重组": ["资产重组", "重大资产", "借壳上市", "资产注入", "资产置换"],
        "回购增持": ["回购", "增持", "股份回购", "大股东增持", "实控人增持"],
        "业绩预增": ["业绩预增", "业绩大增", "净利润增长", "营收增长", "扭亏为盈"],
        "股权激励": ["股权激励", "限制性股票", "股票期权", "员工持股"],
        "大额订单": ["中标", "大额订单", "重大合同", "战略合作", "签约"],
        "分红送转": ["分红", "送股", "转增", "派息", "高送转"],
    },
    "利空": {
        "业绩预减": ["业绩预减", "业绩下滑", "亏损", "营收下降", "净利润下降"],
        "违规处罚": ["违规", "处罚", "立案调查", "行政处罚", "警示函"],
        "股东减持": ["减持", "股东减持", "大股东减持", "高管减持", "清仓"],
        "商誉减值": ["商誉减值", "资产减值", "计提减值", "坏账准备"],
        "退市风险": ["退市", "ST", "*ST", "暂停上市", "终止上市"],
        "诉讼仲裁": ["诉讼", "仲裁", "被诉", "索赔", "判决"],
    },
    "政策": {
        "货币政策": ["降准", "降息", "MLF", "逆回购", "LPR", "流动性"],
        "产业政策": ["产业政策", "补贴", "扶持", "规划", "纲要"],
        "监管政策": ["监管", "新规", "征求意见", "暂停", "整顿", "规范"],
    },
}

# 事件 -> 交易信号映射
SIGNAL_MAP = {
    "利好": {
        "default_signal": "关注",
        "资产重组": {"signal": "强烈关注", "reason": "资产重组可能带来基本面质变"},
        "回购增持": {"signal": "看多", "reason": "大股东/公司用真金白银表达信心"},
        "业绩预增": {"signal": "看多", "reason": "业绩超预期增长"},
        "股权激励": {"signal": "中性偏多", "reason": "管理层利益绑定，长期利好"},
        "大额订单": {"signal": "看多", "reason": "订单驱动业绩确定性增长"},
        "分红送转": {"signal": "关注", "reason": "高分红体现公司现金流充裕"},
    },
    "利空": {
        "default_signal": "回避",
        "业绩预减": {"signal": "看空", "reason": "基本面恶化"},
        "违规处罚": {"signal": "回避", "reason": "合规风险不确定性大"},
        "股东减持": {"signal": "谨慎", "reason": "内部人士减持可能释放负面信号"},
        "商誉减值": {"signal": "看空", "reason": "减值压力影响利润"},
        "退市风险": {"signal": "强烈回避", "reason": "退市风险极高"},
        "诉讼仲裁": {"signal": "谨慎", "reason": "诉讼结果不确定"},
    },
    "政策": {
        "default_signal": "关注",
        "货币政策": {"signal": "关注宏观", "reason": "影响市场整体流动性"},
        "产业政策": {"signal": "关注板块", "reason": "政策利好相关板块"},
        "监管政策": {"signal": "谨慎观望", "reason": "监管收紧可能影响行业格局"},
    },
}

EVENT_DETECTION_PROMPT = """你是一位专业的金融事件分析师。请从以下新闻中识别重大金融事件。

新闻内容：
{news_text}

请以严格的 JSON 格式输出（不要输出其他内容）：
{{
    "has_event": true或false,
    "event_type": "利好/利空/政策/无",
    "event_category": "具体事件类别（如：资产重组、业绩预增、股东减持等）",
    "event_description": "事件描述，一句话",
    "related_stocks": ["相关股票名称或代码"],
    "urgency": "高/中/低",
    "confidence": 1到5的整数（对判断的信心程度）
}}

注意：
- 只识别确定性较高的事件，传闻和猜测标注较低的 confidence
- urgency 高表示需要立即关注，低表示可以后续跟踪"""


def init_client() -> OpenAI:
    """初始化 API 客户端"""
    if not DASHSCOPE_API_KEY:
        print("[错误] 请设置环境变量 DASHSCOPE_API_KEY")
        sys.exit(1)

    return OpenAI(
        api_key=DASHSCOPE_API_KEY,
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
    )


def keyword_based_detection(news_text: str) -> List[dict]:
    """基于关键词的快速事件检测（作为 LLM 检测的前置过滤器）"""
    detected = []

    for event_type, categories in EVENT_KEYWORDS.items():
        for category, keywords in categories.items():
            matched_keywords = [kw for kw in keywords if kw in news_text]
            if matched_keywords:
                detected.append({
                    "event_type": event_type,
                    "event_category": category,
                    "matched_keywords": matched_keywords,
                })

    return detected


def llm_event_detection(client: OpenAI, news_text: str, model: str = "qwen3.8-flash") -> dict:
    """使用 LLM 进行精细事件识别"""
    prompt = EVENT_DETECTION_PROMPT.format(news_text=news_text[:2000])

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            extra_body={"enable_thinking": False},
        )
        content = response.choices[0].message.content.strip()

        if content.startswith("```"):
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]

        return json.loads(content)
    except Exception as e:
        return {"has_event": False, "error": str(e)}


def generate_signal(event_type: str, event_category: str) -> dict:
    """根据事件类型和类别生成交易信号"""
    type_signals = SIGNAL_MAP.get(event_type, {})
    category_signal = type_signals.get(event_category)

    if category_signal:
        return category_signal
    else:
        default = type_signals.get("default_signal", "观望")
        return {"signal": default, "reason": f"{event_type}事件 - {event_category}"}


def extract_news_text(news_item: dict) -> str:
    """从新闻条目中提取文本"""
    text_fields = ["新闻内容", "content", "内容", "新闻标题", "title", "标题"]
    parts = []
    for field in text_fields:
        if field in news_item and news_item[field]:
            parts.append(str(news_item[field]))
    return " ".join(parts) if parts else ""


def main():
    parser = argparse.ArgumentParser(description="事件识别与交易信号生成器")
    parser.add_argument("--news_file", required=True, help="新闻 JSON 文件路径")
    parser.add_argument("--output_dir", default="./output", help="输出目录")
    parser.add_argument("--model", default="qwen3.8-flash", help="LLM 模型")
    parser.add_argument("--use_llm", action="store_true", help="使用 LLM 进行精细事件识别（较慢但更准确）")
    args = parser.parse_args()

    if not os.path.exists(args.news_file):
        print(f"[错误] 新闻文件不存在: {args.news_file}")
        sys.exit(1)

    print(f"[开始] 事件检测: {args.news_file}")

    # 加载新闻
    with open(args.news_file, "r", encoding="utf-8") as f:
        news_list = json.load(f)

    if not news_list:
        print("[错误] 新闻文件为空")
        sys.exit(1)

    print(f"[加载] 共 {len(news_list)} 条新闻")

    client = None
    if args.use_llm:
        client = init_client()

    events = []
    signals = []

    for i, news in enumerate(news_list):
        news_text = extract_news_text(news)
        if not news_text.strip():
            continue

        # 关键词快速检测
        kw_events = keyword_based_detection(news_text)

        if kw_events:
            for kw_event in kw_events:
                event = {
                    "news_index": i,
                    "news_preview": news_text[:150],
                    "detection_method": "keyword",
                    **kw_event,
                }

                # 生成交易信号
                signal = generate_signal(kw_event["event_type"], kw_event["event_category"])
                event["signal"] = signal["signal"]
                event["signal_reason"] = signal["reason"]

                events.append(event)
                signals.append(signal)
                print(f"[事件] ({kw_event['event_type']}) {kw_event['event_category']} "
                      f"-> {signal['signal']} | {news_text[:60]}...")

        # 如果启用 LLM 检测（对关键词未匹配的新闻）
        if args.use_llm and not kw_events:
            llm_result = llm_event_detection(client, news_text, args.model)
            if llm_result.get("has_event"):
                event = {
                    "news_index": i,
                    "news_preview": news_text[:150],
                    "detection_method": "llm",
                    **llm_result,
                }

                signal = generate_signal(
                    llm_result.get("event_type", ""),
                    llm_result.get("event_category", ""),
                )
                event["signal"] = signal["signal"]
                event["signal_reason"] = signal["reason"]

                events.append(event)
                signals.append(signal)
                print(f"[LLM事件] ({llm_result.get('event_type')}) {llm_result.get('event_category')} "
                      f"-> {signal['signal']}")

    # 统计
    event_summary = {
        "total_news": len(news_list),
        "events_detected": len(events),
        "bullish_events": sum(1 for e in events if e.get("event_type") == "利好"),
        "bearish_events": sum(1 for e in events if e.get("event_type") == "利空"),
        "policy_events": sum(1 for e in events if e.get("event_type") == "政策"),
    }

    # 保存结果
    os.makedirs(args.output_dir, exist_ok=True)
    base_name = os.path.splitext(os.path.basename(args.news_file))[0]

    events_file = os.path.join(args.output_dir, f"{base_name}_events.json")
    with open(events_file, "w", encoding="utf-8") as f:
        json.dump({
            "summary": event_summary,
            "events": events,
        }, f, ensure_ascii=False, indent=2)
    print(f"\n[保存] 事件检测结果: {events_file}")

    # 打印摘要
    print(f"\n{'='*60}")
    print(f"[检测结果] 共检测到 {len(events)} 个事件")
    print(f"  利好: {event_summary['bullish_events']} 个")
    print(f"  利空: {event_summary['bearish_events']} 个")
    print(f"  政策: {event_summary['policy_events']} 个")

    if events:
        print(f"\n[交易信号]")
        for e in events:
            print(f"  [{e.get('event_type', '')}] {e.get('event_category', '')} "
                  f"-> {e.get('signal', '')} ({e.get('signal_reason', '')})")

    print(f"{'='*60}")

    result = {
        "status": "success",
        **event_summary,
        "events_file": events_file,
    }
    print(f"\n[结果] {json.dumps(result, ensure_ascii=False, indent=2)}")


if __name__ == "__main__":
    main()
