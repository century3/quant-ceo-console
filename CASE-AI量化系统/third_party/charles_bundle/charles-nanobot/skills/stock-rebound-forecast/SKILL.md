---
name: stock-rebound-forecast
description: "用 XGBoost 预测美股未来 N 个交易日涨幅超过阈值的概率（默认 5 天 / 15%）。支持拼接已退市前身代码延长历史样本，walk-forward 样本外验证后输出当前反弹概率。在用户询问某只美股短期反弹概率、超跌反弹信号、或要求用机器学习预测股票涨跌概率时使用。"
keywords: 预测, 反弹概率, 涨跌概率, XGBoost, 美股, COHR
---

# 美股反弹概率

预测的是「未来 N 个交易日累计涨幅超过阈值」的概率，不是未来股价。不要把概率换算成目标价。A 股不要用本技能。

模型、特征和验证都在本机脚本里，这里只负责调用：

`C:\Users\ADMIN\.claude\skills\stock-rebound-forecast\scripts\predict_rebound.py`

## 命令

在 Charles 工作目录执行。代码写 `COHR` 这种美股代码，不要加 `.US`。单次大约 1 到 3 分钟。

```
python skills/stock-rebound-forecast/scripts/predict_rebound.py --symbol COHR
```

拼接已退市前身代码：

```
python skills/stock-rebound-forecast/scripts/predict_rebound.py --symbol COHR --legacy IIVI
```

自定义阈值、周期和验证窗口：

```
python skills/stock-rebound-forecast/scripts/predict_rebound.py --symbol NVDA --threshold 0.10 --horizon 5 --eval 250
```

## 怎么向用户解释结果

1. AUC 是可信度：0.5 等于随机，大于 0.6 才可用，大于 0.7 较强。AUC 低于 0.6 时说明模型没有预测力，概率不可信。
2. 把输出概率和基础概率（正样本率）对比，说明是常态的几倍。
3. 短历史和加 `--legacy` 的长历史如果相差超过 20 个百分点，两个数都报出来，分歧本身就是不确定性。
4. 附上说明：这是统计模型，不构成投资建议。
