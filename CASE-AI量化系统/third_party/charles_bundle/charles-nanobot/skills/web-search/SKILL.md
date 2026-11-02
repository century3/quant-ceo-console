---
name: web-search
description: "联网搜索实时市场信息，获取最新股价、行业动态、政策新闻、分析师观点等。在用户需要最新数据、实时行情、当前市场状况时使用。"
keywords: 联网, 搜索, 最新, 实时, 行情, 股价, 今天, 当前, 新闻, 政策, 分析师, 市场
---

# web-search 技能指南

## 适用场景
- 查询某只股票的最新行情、股价、涨跌幅
- 获取最新的行业政策、市场动态、宏观经济数据
- 搜索分析师对某公司的最新评级和目标价
- 获取最新的公司公告、业绩快报等时效性信息
- 回答任何需要实时/最新数据的投研问题

## 可用脚本

| 脚本 | 功能 | 参数 |
|------|------|------|
| `scripts/search_market.py` | 联网搜索市场信息 | `--query`(搜索问题), `--type`(搜索类型: stock/news/policy/general) |

## 工作流程

1. 判断用户问题是否需要实时/最新数据
2. 执行 `search_market.py --query "问题" --type 类型`
3. 查询写成"最新/近期"，不要写具体日历日期
4. 将搜索结果整合到回答中；若结果自称"当前是 2024 年"，视为过期幻觉

`search_market.py` 已开启 Qwen `forced_search`，强制联网，避免用约 2024 年中的训练记忆当"现在"。

## 示例对话

用户: "贵州茅台今天股价多少？"
步骤:
1. 执行 `python skills/web-search/scripts/search_market.py --query "贵州茅台最新股价行情" --type stock`
2. 返回最新行情数据

用户: "最近半导体行业有什么政策利好？"
步骤:
1. 执行 `python skills/web-search/scripts/search_market.py --query "半导体行业最新政策利好" --type policy`
2. 整理并返回政策信息

用户: "券商对中芯国际的最新评级是什么？"
步骤:
1. 执行 `python skills/web-search/scripts/search_market.py --query "中芯国际 券商评级 目标价 最新" --type stock`
2. 返回分析师观点
