# -*- coding: utf-8 -*-
# Author：@跟陈博士学AI
# 24-CASE-A: Brinson 归因模型
"""
BrinsonAttribution -- 经典的 Brinson-Hood-Beebower (BHB) 归因模型

学员痛点: "这个月我赚了 5%, 是运气还是实力? 是择时还是选股? 是行业 Beta 还是 Alpha?"

Brinson 把组合超额收益拆解成 3 部分:

    1. 配置效应 (Allocation Effect)  -- 行业配置贡献
       公式: AE = Σ (Wp_i - Wb_i) × Rb_i
       含义: 我超配的行业是不是真的强 (跑赢基准)
    
    2. 选股效应 (Selection Effect)   -- 个股选择贡献
       公式: SE = Σ Wb_i × (Rp_i - Rb_i)
       含义: 在每个行业里, 我选的票是不是比该行业平均强
    
    3. 交互效应 (Interaction Effect) -- 配置 × 选股
       公式: IE = Σ (Wp_i - Wb_i) × (Rp_i - Rb_i)
       含义: 我"超配且选对"或"低配且选错"带来的额外收益

    总超额收益 = AE + SE + IE = Rp - Rb

参数:
    Wp_i = 组合在行业 i 的权重
    Wb_i = 基准在行业 i 的权重 (例: 沪深 300)
    Rp_i = 组合在行业 i 持仓的加权收益
    Rb_i = 基准在行业 i 的收益

核心价值: 让你看清"赚 5% 里有多少是运气 (行业 beta), 多少是实力 (alpha)"
"""

from __future__ import annotations
from dataclasses import dataclass
from typing import Dict, List
import numpy as np
import pandas as pd


@dataclass
class BrinsonResult:
    portfolio_return:    float          # 组合总收益
    benchmark_return:    float          # 基准总收益
    excess_return:       float          # 超额收益
    allocation_effect:   float          # 配置效应
    selection_effect:    float          # 选股效应
    interaction_effect:  float          # 交互效应
    by_industry:         pd.DataFrame   # 各行业明细

    def __str__(self):
        return (f"组合收益 {self.portfolio_return:>+.2%}  "
                f"基准收益 {self.benchmark_return:>+.2%}  "
                f"超额 {self.excess_return:>+.2%}\n"
                f"  配置效应 {self.allocation_effect:>+.4%}  "
                f"({self.allocation_effect / self.excess_return * 100 if self.excess_return else 0:>+.0f}%)\n"
                f"  选股效应 {self.selection_effect:>+.4%}  "
                f"({self.selection_effect / self.excess_return * 100 if self.excess_return else 0:>+.0f}%)\n"
                f"  交互效应 {self.interaction_effect:>+.4%}  "
                f"({self.interaction_effect / self.excess_return * 100 if self.excess_return else 0:>+.0f}%)")


def brinson_attribution(
    portfolio_weights:  Dict[str, float],   # {行业: 组合权重}
    benchmark_weights:  Dict[str, float],   # {行业: 基准权重}
    portfolio_returns:  Dict[str, float],   # {行业: 组合内该行业加权收益}
    benchmark_returns:  Dict[str, float],   # {行业: 基准内该行业收益}
) -> BrinsonResult:
    """
    经典 Brinson-Hood-Beebower (BHB) 三因子归因
    """
    industries = sorted(set(portfolio_weights) | set(benchmark_weights))

    rows = []
    total_ae = 0
    total_se = 0
    total_ie = 0

    for ind in industries:
        wp = portfolio_weights.get(ind, 0)
        wb = benchmark_weights.get(ind, 0)
        rp = portfolio_returns.get(ind, 0)
        rb = benchmark_returns.get(ind, 0)

        ae = (wp - wb) * rb
        se = wb * (rp - rb)
        ie = (wp - wb) * (rp - rb)

        total_ae += ae
        total_se += se
        total_ie += ie

        rows.append({
            "industry":      ind,
            "Wp":            round(wp, 4),
            "Wb":            round(wb, 4),
            "Rp":            round(rp, 4),
            "Rb":            round(rb, 4),
            "allocation":    round(ae, 6),
            "selection":     round(se, 6),
            "interaction":   round(ie, 6),
            "total":         round(ae + se + ie, 6),
        })

    by_industry = pd.DataFrame(rows).sort_values("total", ascending=False)

    rp_total = sum(portfolio_weights[i] * portfolio_returns.get(i, 0)
                    for i in portfolio_weights)
    rb_total = sum(benchmark_weights[i] * benchmark_returns.get(i, 0)
                    for i in benchmark_weights)

    return BrinsonResult(
        portfolio_return=    rp_total,
        benchmark_return=    rb_total,
        excess_return=       rp_total - rb_total,
        allocation_effect=   total_ae,
        selection_effect=    total_se,
        interaction_effect=  total_ie,
        by_industry=         by_industry,
    )


# ============================================================
# Demo -- 教学构造, 给页面 / 命令行对照公式, 不是真实持仓
# ============================================================

# 剧本: AI 主线月, 重仓通信/电子/电力设备, 低配银行
DEMO_PORTFOLIO_WEIGHTS = {
    "通信": 0.30, "电子": 0.25, "电力设备": 0.20,
    "国防军工": 0.10, "有色金属": 0.10, "银行": 0.05,
}
# 基准行业权重示意 (不是当期沪深300 精确切分)
DEMO_BENCHMARK_WEIGHTS = {
    "通信": 0.05, "电子": 0.10, "电力设备": 0.08,
    "国防军工": 0.05, "有色金属": 0.06, "银行": 0.18,
    "食品饮料": 0.15, "医药生物": 0.12, "其他": 0.21,
}
# 行业基准收益: 板块轮动课的示意数字 (通信当月强)
DEMO_BENCHMARK_RETURNS = {
    "通信": 0.097, "电子": 0.056, "电力设备": 0.014,
    "国防军工": 0.008, "有色金属": 0.048, "银行": -0.012,
    "食品饮料": -0.026, "医药生物": 0.001, "其他": -0.008,
}
# 组合内假设选了龙头, Rp 比 Rb 略高
DEMO_PORTFOLIO_RETURNS = {
    "通信": 0.115, "电子": 0.072, "电力设备": 0.020,
    "国防军工": 0.015, "有色金属": 0.060, "银行": 0.005,
}

DEMO_SCENARIO = {
    "title": "AI 主线月 (教学构造)",
    "not_real": True,
    "story": "重仓通信 30% / 电子 25% / 电力设备 20%, 低配银行 5%; 食品饮料、医药生物组合没配.",
    "return_note": "行业基准收益是示意数字 (通信当月 +9.7%); 组合内假设选了龙头, 所以通信 +11.5%、电子 +7.2%, 都比基准略高.",
    "purpose": "数字事先给定, 用来手算对照 BHB: 配置=(Wp-Wb)×Rb, 选股=Wb×(Rp-Rb), 交互=(Wp-Wb)×(Rp-Rb). 三因子加总应对上超额 +5.70%.",
}


def run_demo_brinson() -> tuple:
    """跑演示归因, 返回 (BrinsonResult, scenario 字典)"""
    result = brinson_attribution(
        DEMO_PORTFOLIO_WEIGHTS, DEMO_BENCHMARK_WEIGHTS,
        DEMO_PORTFOLIO_RETURNS, DEMO_BENCHMARK_RETURNS,
    )
    return result, dict(DEMO_SCENARIO)


def demo():
    result, scenario = run_demo_brinson()
    print(f"\n{'='*78}")
    print(f"  CASE-A Brinson 归因 demo -- {scenario['title']}")
    print(f"  {scenario['story']}")
    print(f"{'='*78}\n")

    print(f"[总览]\n{result}\n")

    print(f"[各行业明细] (按 total 贡献降序)")
    print(result.by_industry.to_string(index=False))

    # 关键洞察
    print(f"\n[关键洞察]")
    if abs(result.allocation_effect) > abs(result.selection_effect):
        print(f"  配置效应 ({result.allocation_effect:+.2%}) > 选股效应 ({result.selection_effect:+.2%})")
        print(f"  -> 你赚的钱主要来自 [行业配置], 即'选对了赛道'")
        print(f"  -> 这个月运气好赶上了 AI 板块强势 (通信/电子超配 +25%/+15%)")
    else:
        print(f"  选股效应 ({result.selection_effect:+.2%}) > 配置效应 ({result.allocation_effect:+.2%})")
        print(f"  -> 你赚的钱主要来自 [选股能力], 即'在每个行业里选对了票'")

    if result.interaction_effect > 0:
        print(f"  交互效应 ({result.interaction_effect:+.2%}) > 0")
        print(f"  -> '超配且选对' = 配置和选股双重加持, 这是真正的 alpha")


if __name__ == "__main__":
    demo()
