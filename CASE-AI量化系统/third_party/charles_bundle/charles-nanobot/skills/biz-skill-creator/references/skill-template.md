# Skill 输出模板

> **语言要求**: 生成的所有内容（SKILL.md、examples.md）必须全部使用中文。包括 frontmatter 中的 description、所有标题、流程描述、规则说明均使用中文，不得使用英文。

生成 SKILL.md 时严格使用以下模板。用 `{placeholder}` 标注的位置需要替换为实际内容。

## SKILL.md 模板

```markdown
---
name: {skill-name}
description: "{description-what}。当{description-when}时使用。"
---

# {skill-title}

## 操作流程

{workflow-steps}

## 关键规则

### 必须遵守
{must-do-rules}

### 禁止事项
{must-not-rules}

## 参考文档
{references-list}
```

## 各字段填写说明

### name
- 小写字母 + 数字 + 连字符
- 动词开头优先，如 `review-bond-credit`、`generate-daily-brief`
- 不超过 64 个字符

### description
- 合并 Step 1 的"做什么"和"什么时候做"
- 格式: "{做什么}。当{触发场景1}、{触发场景2}时使用。"
- 必须同时包含 WHAT（做什么）和 WHEN（什么时候）

### 操作流程
- 从 Step 2 采集的示例中提炼通用步骤
- 用祈使句，每步一行编号
- 如果某步需要调用工具或脚本，写明具体命令
- 示例:
```markdown
1. 获取目标公司最近一期财务报告（年报或半年报）
2. 提取关键财务指标: 营收、净利润、资产负债率、经营性现金流
3. 与上一期数据对比，计算同比变化率
4. 查询最新信用评级和行业评级中位数
5. 综合以上信息，输出信用评估结论（参考 references/examples.md 中的输出格式）
```

### 关键规则
- "必须遵守"和"禁止事项"分开列出
- 每条规则用 `- ` 开头，一句话说清
- 示例:
```markdown
### 必须遵守
- 必须使用最近一期的财务数据，不能用超过两个季度的旧数据
- 信用评估结论必须包含评级建议和主要风险点

### 禁止事项
- 不能仅凭单一指标（如 ROE）下结论
- 不能使用非官方披露的财务数据
```

### 参考文档
- 列出 references/ 目录下的相关文件
- 如果无参考文档，写"无额外参考文档"
- 示例:
```markdown
- 示例案例与输出格式: [examples.md](references/examples.md)
- 评级标准参考: [rating-criteria.md](references/rating-criteria.md)
```

## examples.md 模板

采集到的示例保存到 `references/examples.md`，格式如下:

```markdown
# 示例案例

## 案例 1: {case-title}

### 输入
{input-description}

### 执行步骤
1. {step-1}
2. {step-2}
...

### 输出
{output-content}

---

## 案例 2: {case-title}
（如有第二个案例，同上格式）
```
