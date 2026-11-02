---
name: sentiment-analysis
description: "监控东方财富等新闻源，利用NLP分析市场情绪（贪婪/恐慌），捕捉事件驱动交易机会。在用户请求查看新闻、舆情分析、市场情绪时使用。"
keywords: 舆情, 情感分析, 新闻监控, 市场情绪, 资产重组, 事件驱动, 贪婪, 恐慌, 舆论, 消息面
---

# sentiment-analysis 技能指南

## 适用场景
- 监控某只股票或行业的近期新闻舆情
- 分析市场整体情绪（贪婪/恐慌）
- 捕捉"资产重组"、"业绩预增"等关键事件
- 生成事件驱动的交易信号和机会提示

## 数据来源
- 东方财富个股新闻（通过 akshare）
- 上市公司公告
- 央视新闻（政策面）

## 依赖技能
- **read-pdf**: 事件触发后可联动 read-pdf 进行深度财报分析
- **write-report**: 可联动 write-report 生成完整的事件驱动分析报告

## 可用脚本

| 脚本 | 功能 | 参数 |
|------|------|------|
| `scripts/news_fetcher.py` | 抓取新闻并按关键词过滤 | `--stock`(股票代码), `--keywords`, `--days` |
| `scripts/sentiment_scorer.py` | LLM 情感分析与评分 | `--news_file`, `--output_dir` |
| `scripts/event_detector.py` | 事件识别与交易信号 | `--news_file`, `--output_dir` |

## 关键词过滤体系

### 利好类
资产重组、回购、业绩预增、股权激励、大额订单、战略合作、增持

### 利空类
业绩预减、违规处罚、股东减持、商誉减值、退市风险、诉讼仲裁

### 政策类
降准降息、产业政策、监管新规、财政刺激、贸易政策

## 工作流程

1. 使用 `news_fetcher.py` 获取目标股票/行业的近期新闻
2. 使用 `sentiment_scorer.py` 对新闻进行情感分析
   - 每条新闻输出：情感倾向（正面/负面/中性）、强度（1-5）、关键实体
   - 汇总生成情绪指数
3. 使用 `event_detector.py` 识别重大事件并生成交易信号
   - 事件分类：利好/利空/政策
   - 交易信号：买入/卖出/观望
4. 如发现重大事件，可联动其他技能进行深度分析

## 示例对话

用户: "帮我看看比亚迪最近有什么新闻"
步骤:
1. 执行 `news_fetcher.py --stock 002594 --days 7`
2. 执行 `sentiment_scorer.py --news_file data/002594_news.json`
3. 返回新闻摘要和情绪分析结果

用户: "最近A股有没有资产重组的消息"
步骤:
1. 执行 `news_fetcher.py --keywords 资产重组 --days 3`
2. 执行 `event_detector.py --news_file data/资产重组_news.json`
3. 返回事件列表和交易机会提示

用户: "发现比亚迪有资产重组，帮我做个深度分析"
步骤:
1. 联动 read-pdf 技能读取比亚迪最新财报
2. 联动 write-report 技能生成五步法分析报告
3. 将舆情分析与基本面分析结合，给出完整建议
