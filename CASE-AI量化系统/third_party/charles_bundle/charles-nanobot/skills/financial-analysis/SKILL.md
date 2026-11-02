---
name: financial-analysis
description: "分析上市公司财务指标趋势(毛利率/ROE/负债率等)，支持同行业横向对比。在用户请求分析财务数据、对比公司指标、查看财务趋势时使用。"
keywords: 财务分析, 指标, 毛利率, ROE, 净利率, 负债率, 趋势, 对比, 同行, 横向比较, 杜邦分析
---

# financial-analysis 技能指南

## 适用场景
- 分析某公司近几年的核心财务指标趋势(毛利率、净利率、ROE、负债率等)
- 两家或多家公司的财务指标横向对比
- 快速了解一家公司的财务健康状况

## 依赖技能
- **read-pdf**: 如果本地没有财务数据 CSV，需要先用 `fetch_financial_data.py` 获取

## 数据来源
- `data/financial_data/{股票代码}_financial_abstract.csv` (东方财富财务摘要)
- `data/financial_data/{股票代码}_income_statement.csv` (利润表)
- `data/financial_data/{股票代码}_balance_sheet.csv` (资产负债表)
- `data/financial_data/{股票代码}_cash_flow.csv` (现金流量表)

## 可用脚本

| 脚本 | 功能 | 参数 |
|------|------|------|
| `scripts/ratio_analysis.py` | 核心财务指标分析 | `--stock`(股票代码), `--years`(分析年数,默认5), `--data_dir` |
| `scripts/peer_compare.py` | 多公司横向对比 | `--stocks`(多个股票代码,逗号分隔), `--data_dir` |

## 工作流程

1. 检查 `data/financial_data/` 下是否有目标公司的 CSV 数据
2. 如无数据，先执行 `python skills/read-pdf/scripts/fetch_financial_data.py --stock <代码> --type financial`
3. 执行分析脚本

## 示例对话

用户: "帮我分析贵州茅台的财务状况"
步骤:
1. 检查 data/financial_data/ 下是否有 600519 开头的 CSV
2. 执行 `python skills/financial-analysis/scripts/ratio_analysis.py --stock 600519`
3. 返回核心指标趋势分析

用户: "对比中芯国际和台积电的盈利能力"
步骤:
1. 确认两家公司的财务数据都已获取
2. 执行 `python skills/financial-analysis/scripts/peer_compare.py --stocks 688981,TSM`
3. 返回横向对比结果

注意：所有路径参数使用相对路径（不带前导 /）。
