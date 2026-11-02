---
name: read-pdf
description: "读取上市公司PDF财报与公告，构建RAG知识库，支持多轮问答和页码溯源。在用户请求读取、解析财报PDF、查询财报数据、下载年报时使用。"
keywords: 财报, 年报, PDF, 读取, 解析, 财务报表, 资产负债表, 利润表, 现金流, 公告, 阅读, 分析
---

# read-pdf 技能指南

## 适用场景
- 读取上市公司年报、半年报、季报 PDF
- 解析财务报表中的复杂表格（资产负债表、利润表、现金流量表）
- 针对财报内容进行多轮问答
- 提取关键财务指标并追溯页码来源
- 从巨潮资讯下载 PDF 年报、获取财务数据

## 两种解析模式

### 模式一：PyPDF2 基础解析（适合纯文本型 PDF）
- 脚本：`skills/read-pdf/scripts/parse_pdf_basic.py`
- 用法：`python skills/read-pdf/scripts/parse_pdf_basic.py --pdf <PDF路径> --output_dir <输出目录>`
- 特点：速度快、轻量，适合公告、文字型报告

### 模式二：多模态大模型解析（适合复杂表格/图表）
- 脚本：`skills/read-pdf/scripts/parse_pdf_ocr.py`
- 用法：`python skills/read-pdf/scripts/parse_pdf_ocr.py --pdf <PDF路径> --output_dir <输出目录>`
- 特点：高精度结构化输出，保留表格格式，适合财务报表
- 默认使用 qwen3.8-flash，也可用 `--model deepseek-ocr-2` 切换

## 可用脚本

| 脚本 | 功能 | 参数 |
|------|------|------|
| `scripts/parse_pdf_basic.py` | PyPDF2 基础解析 PDF | `--pdf`, `--output_dir` |
| `scripts/parse_pdf_ocr.py` | 多模态大模型解析复杂表格 | `--pdf`, `--output_dir`, `--pages`(可选), `--model`(可选) |
| `scripts/build_index.py` | 构建 FAISS 向量索引 | `--text_dir`(目录) 或 `--text_file`(单文件), `--index_dir` |
| `scripts/query_report.py` | RAG 问答查询 | `--index_dir`, `--query`, `--top_k`(默认6) |
| `scripts/fetch_financial_data.py` | 获取财务数据/下载年报PDF | `--stock`, `--type`(financial/pdf/all), `--output_dir` |

## 执行流程（必须遵守）

### 推荐方式: 使用统一索引（preprocess.py 预处理后）
1. **检查统一索引**: 检查 `data/vector_store/` 是否存在
2. **有统一索引 -> 直接查询**: 使用 `query_report.py` 并带 `--stock` 参数过滤
   `python skills/read-pdf/scripts/query_report.py --index_dir data/vector_store --query "问题" --stock 688981`
3. 回答中引用数据来源的文档名和页码

### 回退方式: 单文档索引（无统一索引时）
1. 检查 `data/vector_db/` 下是否有旧版单文档索引
2. 有索引 -> 直接用 `query_report.py` 查询（不带 --stock）
3. 无索引 -> 完整流程:
   a. 检查 `data/reports/` 下是否已有对应 PDF
   b. 如无 PDF，用 `fetch_financial_data.py --stock <代码> --type pdf` 下载
   c. 用 `parse_pdf_basic.py` 或 `parse_pdf_ocr.py` 解析 PDF
   d. 用 `build_index.py` 构建 FAISS 向量索引
   e. 用 `query_report.py` 回答用户问题

### 新增 PDF 的处理
- 将新 PDF 放入 `data/reports/`
- 在项目根目录执行 `python preprocess.py` 即可自动处理新文件并更新统一索引
- preprocess.py 会自动提取元数据(标题、股票代码、发布日期等)并保存到 `data/documents.db`

禁止: 安装依赖、检查环境、验证包版本。

注意: execute 执行脚本时，所有路径参数必须用**相对路径**（不带前导 /）。
例如: `--index_dir data/vector_store`，不要写成 `--index_dir /data/vector_store`。
ls 返回的路径带有前导 /，传给脚本时需要去掉。

## 示例对话

用户: "中芯国际 2025Q2 营收和毛利率是多少？"
步骤:
1. 检查 data/vector_store/ 是否存在统一索引
2. 执行 `python skills/read-pdf/scripts/query_report.py --index_dir data/vector_store --query "2025Q2营收和毛利率" --stock 688981`
3. 返回答案(带来源文档和页码)

用户: "帮我读一下贵州茅台的年报"
步骤:
1. 检查 data/vector_store/ 统一索引
2. 如有，执行 `python skills/read-pdf/scripts/query_report.py --index_dir data/vector_store --query "贵州茅台年报概况" --stock 600519`
3. 如无统一索引也无旧索引，需先下载 PDF、解析并建索引
