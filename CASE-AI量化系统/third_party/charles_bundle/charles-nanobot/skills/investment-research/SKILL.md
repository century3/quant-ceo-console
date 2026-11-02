---
name: investment-research
description: "投研分析技能索引 -- 根据用户需求选择合适的技能组合。当不确定使用哪个技能时先读取此文件。"
---

# 投研分析技能索引

## 按场景选择技能

| 用户需求 | 主要技能 | 辅助技能 |
|---------|---------|---------|
| 写研报/深度分析/五步法 | write-report | web-search, read-pdf, financial-analysis |
| 查最新股价/行情/走势 | stock-price | - |
| 采集美股日线/更新美股行情库 | us-stock-collect | - |
| 美股未来 N 日涨幅超过阈值的概率 | stock-rebound-forecast | - |
| 搜最新新闻/政策/评级 | web-search | - |
| 读财报PDF/查报告数据 | read-pdf | - |
| 分析财务指标/ROE/负债率 | financial-analysis | read-pdf(获取数据) |
| 跨期对比/同行对比 | compare-reports | read-pdf(需要索引) |
| 舆情监控/事件驱动 | sentiment-analysis | web-search, write-report |

## 技能协同规则

1. **写研报优先用 write-report**: 它包含完整的五步法框架和五种场景指南
2. **数据获取优先用 web-search**: nanobot 内置的 web_search 用于实时搜索；`scripts/search_market.py` 用于 A 股专用搜索
3. **本地数据用 read-pdf**: 检查 `data/vector_store/` 是否存在统一索引后再查询
4. **定量分析用 financial-analysis**: 先确认 `data/financial_data/` 有对应 CSV 数据

## 通用执行提示

- 所有脚本通过 exec 工具执行，路径格式: `python skills/<技能名>/scripts/<脚本>.py <参数>`
- 数据目录: `data/vector_store/`(向量索引), `data/financial_data/`(财务CSV), `data/reports/`(PDF)
- 多轮搜索是核心: 不要一次搜完就停，根据发现追加搜索
