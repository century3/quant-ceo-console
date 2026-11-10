# -*- coding: utf-8 -*-
# Author：@跟陈博士学AI
# 23-CASE-C: 龙头战法 -- A 股化首板战法 (Ross Cameron 启发)
"""
DragonPicker -- A 股化的"动量日内"龙头战法

理论基础:
    Ross Cameron 的美股 Gap and Go 策略 -- 专门狩猎当日具有强劲动量的小盘股
    A 股因 ±10% 涨跌停限制, 不能直接照搬, 需本土化:
        美股: 涨幅 > 10% + 涨幅榜前 5 + 价格 < $20 + 流动性适中 + 突发新闻
        A 股: 涨幅 > 5% + 涨幅榜前 20 + 市值 50-200 亿 + 量比 > 3 + 突发利好

5 大筛选法则:

    1. 当日涨幅 > 5%
       已经涨 5% 以上的票, 大概率是当日热点, 短期延续性比"从零开始"高
       (行为金融学的"动量效应")

    2. 涨幅榜前 20 位
       集中度 + 成交量最高, 流动性好, 技术形态被市场尊重

    3. 流通市值 50-200 亿 (适中)
       太大: 筹码沉淀, 涨幅被稀释
       太小: 流动性差 + 容易被庄家拉抬

    4. 量比 > 3 (近 5 日成交额 / 60 日均)
       有量才是真涨, 没量的"假突破"会被快速打回

    5. 价格 < 30 元
       低价股波动性更强, 心理上"看似便宜", 容易被散户追捧
       (但要排除 ST/退市风险)

输出:
    每个候选标的含: code / name / 涨幅 / 量比 / 流通市值 / 龙头分

注意:
    本模块 demo 因为不是真实开盘时段, 只能用历史数据回放
    实际生产: 这个 picker 应该挂在 9:35 (开盘后 5 分钟) 跑, 拿到当日真实涨跌
"""

from __future__ import annotations
import math
import sys
from pathlib import Path
from typing import Dict, List, Optional
import numpy as np
import pandas as pd


def calc_dragon_score(stock_data: dict) -> float:
    """
    龙头综合分 -- v1 5 法则等权打分 + v2 板块共振加分

    输入:
        {
            "day_change_pct":   0.072,    # 当日涨幅
            "volume_ratio":     4.2,      # 量比 (vs 60 日均)
            "float_market_cap": 80e8,     # 流通市值 (元)
            "price":            15.6,     # 当前价
            "rank_in_top":      3,        # 当日涨幅榜排名 (1-100)
            # v2 新增 (来自 trade_sector_daily, 见 calc_sector_resonance)
            "sector_change_pct":  0.025,  # 所在 sector_2 当日涨幅 (小数, 如 0.025=2.5%)
            "sector_rise_ratio":  0.62,   # 板块上涨家数占比 (0-1)
        }
    """
    score = 0
    # 涨幅: 越大越好。A 股 > 9% 接近涨停, 减分; 美股没有涨停, penalize_limit_up=False
    chg = stock_data.get("day_change_pct", 0)
    if chg > 0.09 and stock_data.get("penalize_limit_up", True):
        score += 0.5
    else:
        score += min(chg * 10, 1.0)   # 5% -> 0.5, 8% -> 0.8, 10% 及以上封顶 1.0

    # 量比: 越大越好, 但 > 8 容易是异常
    vr = stock_data.get("volume_ratio", 0)
    score += min(vr / 5, 1.5)

    # 流通市值: 50-200 亿最佳。美股没有流通股本, 这项为 None 时不加分
    mcap = stock_data.get("float_market_cap", 0)
    if mcap is None:
        mcap = -1
    if 50e8 <= mcap <= 200e8:
        score += 1.0
    elif 30e8 <= mcap < 50e8 or 200e8 < mcap <= 500e8:
        score += 0.5
    else:
        score += 0.0

    # 涨幅榜排名: 越靠前越好
    rank = stock_data.get("rank_in_top", 100)
    if rank <= 5:
        score += 1.0
    elif rank <= 20:
        score += 0.5
    elif rank <= 50:
        score += 0.2
    else:
        score += 0.0

    # 价格: < 20 加分, 20-30 中性, > 30 减分
    price = stock_data.get("price", 100)
    if price < 20:
        score += 0.5
    elif price <= 30:
        score += 0.2
    else:
        score += 0.0

    # v2 新增: 板块共振加分
    # 站在强势板块里 + 板块内多家齐涨, 才像真龙
    score += calc_sector_resonance_score(stock_data)

    return round(score, 3)


# ============================================================
# v2 新增: 板块共振 (sector resonance)
# ------------------------------------------------------------
# 金融含义:
#   A 股龙头本质是"题材资金", 孤雁难成龙. 一只股涨 7% 但所在 sector_2 当日跌,
#   多半是诱多; 站在强势板块 + 板块内 >= 5 家齐涨, 才是机构资金共识.
# 数据来源:
#   trade_sector_daily (sector_name=sector_2, sector_level=2):
#       change_pct  -- 板块当日等权涨幅 (%)
#       rise_count  -- 板块内上涨家数
#       stock_count -- 板块成份股数
# ============================================================

# 共振硬阈值: 板块当日涨幅 >= 0.5%, 上涨家数占比 >= 40%, 才允许进候选
SECTOR_MIN_CHANGE_PCT = 0.005
SECTOR_MIN_RISE_RATIO = 0.40
# 美股价格上限。A 股仍用 30 元，两边不共用这个数。
US_MAX_PRICE = 400.0


def calc_sector_resonance_score(stock_data: dict) -> float:
    """
    板块共振加分 (0 ~ 1.5)
        - 板块涨幅 0.5% -> 0 分; 1% -> 0.2; 3% -> 1.0 (满)
        - 板块上涨家数占比 50% -> 0 分; 70% -> 0.5 (满)
    板块字段缺失 (例如 sector_2 为空) 时不加不减
    """
    s_chg = stock_data.get("sector_change_pct")
    s_rise = stock_data.get("sector_rise_ratio")
    if s_chg is None and s_rise is None:
        return 0.0
    score = 0.0
    if s_chg is not None and s_chg > 0.005:
        score += min((s_chg - 0.005) / 0.025, 1.0)
    if s_rise is not None and s_rise > 0.5:
        score += min((s_rise - 0.5) / 0.2, 1.0) * 0.5
    return round(score, 3)


def passes_sector_resonance(stock_data: dict) -> bool:
    """
    板块共振硬过滤: 板块涨幅 + 上涨家数占比 都达标才放行
    没有板块数据 (sector_2 为空 / 当日无板块行情) 直接淘汰, 不放过孤雁
    """
    s_chg = stock_data.get("sector_change_pct")
    s_rise = stock_data.get("sector_rise_ratio")
    if s_chg is None or s_rise is None:
        return False
    return s_chg >= SECTOR_MIN_CHANGE_PCT and s_rise >= SECTOR_MIN_RISE_RATIO


def dragon_version_params(version: str) -> dict:
    """v1 对照 / v2 默认 -- 给 filter_dragon_candidates 的硬过滤参数"""
    if version == "v1":
        return {"max_change": 1.0, "min_listed_days": 0, "require_sector_resonance": False}
    if version == "v2":
        return {"max_change": 0.095, "min_listed_days": 60, "require_sector_resonance": True}
    raise ValueError(f"龙头版本只能是 v1 或 v2, 收到: {version!r}")


def filter_dragon_candidates(stocks_today: List[dict],
                             min_change: float = 0.05,
                             max_price: float = 30,
                             mcap_range: tuple = (30e8, 500e8),
                             min_volume_ratio: float = 2.0,
                             require_sector_resonance: bool = True,
                             max_change: float = 0.095,
                             min_listed_days: int = 60,
                             check_mcap: bool = True) -> List[dict]:
    """
    应用 v1 5 大筛选法则 + v2 硬规则补丁 + 板块共振硬过滤, 返回候选

    v1 5 法则 (老逻辑保留):
        1) 当日涨幅 > min_change
        2) 涨幅榜前 50
        3) 流通市值 mcap_range 区间
        4) 量比 > min_volume_ratio
        5) 排除 ST / 退市

    v2 硬规则补丁 (避免实战漏洞):
        6) 涨幅 < max_change   涨停板 / 一字板买不到, T+1 高开导致回测虚胖
        7) 上市天数 >= min_listed_days  排除次新股 (波动巨大 + 形态不可信)
        8) 板块共振 require_sector_resonance=True 时, 板块涨幅 / 上涨家数占比双达标

    输入: stocks_today = [{
        code, name, day_change_pct, price, volume_ratio, float_market_cap,
        # 可选, v2 用:
        listed_days, sector_change_pct, sector_rise_ratio,
    }, ...]
    """
    # 排序拿涨幅榜前 N 名
    sorted_today = sorted(stocks_today, key=lambda x: x.get("day_change_pct", 0), reverse=True)
    for i, s in enumerate(sorted_today, 1):
        s["rank_in_top"] = i

    candidates = []
    for s in sorted_today[:50]:    # 只看前 50 名
        chg = s.get("day_change_pct", 0)
        # v1 法则 1
        if chg < min_change:
            continue
        # v2 法则 6: 接近涨停的不收 (T+1 高开污染统计)
        if chg > max_change:
            continue
        if s.get("price", 999) > max_price:
            continue
        # 美股没有流通股本, 调用方显式关掉市值过滤, 不把缺失当成 0 再淘汰
        if check_mcap:
            mcap = s.get("float_market_cap", 0) or 0
            if mcap < mcap_range[0] or mcap > mcap_range[1]:
                continue
        if s.get("volume_ratio", 0) < min_volume_ratio:
            continue
        # v1 法则 5: 排除 ST / 退市
        name = s.get("name", "")
        if "ST" in name.upper() or "*" in name or "退" in name:
            continue
        # v2 法则 7: 排除次新股 (上市不足 N 个交易日)
        ld = s.get("listed_days")
        if ld is not None and ld < min_listed_days:
            continue
        # v2 法则 8: 板块共振硬过滤
        if require_sector_resonance and not passes_sector_resonance(s):
            continue
        # 算分 (v1 5 项 + v2 共振加分)
        s["dragon_score"] = calc_dragon_score(s)
        candidates.append(s)

    # 按分数排序
    candidates.sort(key=lambda x: x.get("dragon_score", 0), reverse=True)
    return candidates


# ============================================================
# 入场出场逻辑
# ============================================================

class DragonEntryExit:
    """
    龙头战法的入场出场规则

    核心铁律 (Ross Cameron 总结):
        1. Base Hit 小赢 -- 每股目标 0.3-0.8 元, 不追求本垒打
        2. 盈亏比 ≥ 2:1
        3. 单笔最大亏损 ≤ 总资金 × 1%
        4. 时间止损 -- 持仓不过当日 (或最长 N 分钟)
    """

    def __init__(self,
                 capital: float = 1_000_000,
                 risk_per_trade_pct: float = 0.01,    # 单笔风险 1%
                 target_payoff_ratio: float = 2.0,
                 max_hold_minutes: int = 120,         # 最长持有 2 小时
                 daily_max_loss_pct: float = 0.02):   # 日亏损上限 2%
        self.capital = capital
        self.risk_per_trade_pct = risk_per_trade_pct
        self.target_payoff_ratio = target_payoff_ratio
        self.max_hold_minutes = max_hold_minutes
        self.daily_max_loss_pct = daily_max_loss_pct

    def calc_entry(self, candidate: dict) -> dict:
        """
        给一只候选股算入场参数
        
        关键参数:
            entry_price: 限价 (= 卖一价 + 1 跳)
            stop_loss:   止损价 (= 入场价 × (1 - max_loss_pct))
            target:      止盈价 (= 入场价 × (1 + max_loss_pct × 2:1))
            quantity:    股数 (基于风险预算)
        """
        price = candidate["price"]
        # 止损位 = 入场价 - 1 个 ATR (简化为 2% 价格波动)
        # 实战可用日线 ATR 等指标替换此处占位
        atr_pct = 0.02
        stop_pct = atr_pct
        target_pct = stop_pct * self.target_payoff_ratio

        max_loss_amount = self.capital * self.risk_per_trade_pct
        per_share_loss = price * stop_pct
        quantity = int(max_loss_amount / per_share_loss / 100) * 100
        if quantity == 0:
            quantity = 100   # 至少 1 手试探
        # 单笔不超过 5%
        max_amount = self.capital * 0.05
        if quantity * price > max_amount:
            quantity = int(max_amount / price / 100) * 100

        return {
            "code":        candidate["code"],
            "name":        candidate.get("name", ""),
            "entry_price": round(price, 3),
            "stop_loss":   round(price * (1 - stop_pct), 3),
            "target":      round(price * (1 + target_pct), 3),
            "quantity":    quantity,
            "amount":      round(quantity * price, 2),
            "max_loss":    round(quantity * price * stop_pct, 2),
            "max_gain":    round(quantity * price * target_pct, 2),
            "max_hold_minutes": self.max_hold_minutes,
            "payoff_ratio": self.target_payoff_ratio,
        }


# ============================================================
# Demo (用模拟数据 + 真实价格混合 -- 因为不是真实开盘时段)
# ============================================================

def _build_mock_today_stocks() -> List[dict]:
    """
    构造一批"今日候选"用于演示 v1 / v2 法则.
    价、流通市值 = 2026-09-15 close_price × float_shares (wucai_trade);
    涨幅 / 量比 / 板块强弱仍按教学场景构造.

    中芯 / 宁德 真实流通市值已是千亿级, 进不了 30-500 亿带,
    「真龙」位用仍落在该带内的真实中盘.
    """
    return [
        {"code": "002465.SZ", "name": "海格通信",   "day_change_pct": 0.072,
         "price": 10.03,  "volume_ratio": 4.8, "float_market_cap": 249e8,
         "sector_2": "军工电子", "sector_change_pct": 0.032, "sector_rise_ratio": 0.70,
         "listed_days": 4200},
        {"code": "300432.SZ", "name": "富临精工",   "day_change_pct": 0.068,
         "price": 13.79,  "volume_ratio": 3.6, "float_market_cap": 233e8,
         "sector_2": "电池", "sector_change_pct": 0.028, "sector_rise_ratio": 0.65,
         "listed_days": 2800},
        {"code": "002456.SZ", "name": "欧菲光",     "day_change_pct": 0.063,
         "price": 6.95,   "volume_ratio": 3.8, "float_market_cap": 231e8,
         "sector_2": "光学光电子", "sector_change_pct": 0.022, "sector_rise_ratio": 0.62,
         "listed_days": 4000},
        {"code": "300253.SZ", "name": "卫宁健康",   "day_change_pct": 0.058,
         "price": 6.69,   "volume_ratio": 3.2, "float_market_cap": 124e8,
         "sector_2": "软件开发", "sector_change_pct": 0.018, "sector_rise_ratio": 0.58,
         "listed_days": 3200},
        {"code": "600161.SH", "name": "天坛生物",   "day_change_pct": 0.085,
         "price": 12.32,  "volume_ratio": 5.2, "float_market_cap": 244e8,
         "sector_2": "生物制品", "sector_change_pct": 0.003, "sector_rise_ratio": 0.35,
         "listed_days": 4500},
        {"code": "603799.SH", "name": "华友钴业",   "day_change_pct": 0.097,
         "price": 35.63,  "volume_ratio": 6.5, "float_market_cap": 673e8,
         "sector_2": "能源金属", "sector_change_pct": 0.020, "sector_rise_ratio": 0.60,
         "listed_days": 2800},
        {"code": "301999.SZ", "name": "次新示例",   "day_change_pct": 0.072,
         "price": 22.00,  "volume_ratio": 4.2, "float_market_cap": 90e8,
         "sector_2": "半导体", "sector_change_pct": 0.032, "sector_rise_ratio": 0.70,
         "listed_days": 30},
        {"code": "600519.SH", "name": "贵州茅台",   "day_change_pct": 0.018,
         "price": 1277.21, "volume_ratio": 1.4, "float_market_cap": 15966e8,
         "sector_2": "白酒", "sector_change_pct": -0.005, "sector_rise_ratio": 0.30,
         "listed_days": 5500},
        {"code": "300750.SZ", "name": "宁德时代",   "day_change_pct": 0.072,
         "price": 328.88, "volume_ratio": 3.5, "float_market_cap": 14011e8,
         "sector_2": "电池", "sector_change_pct": 0.028, "sector_rise_ratio": 0.65,
         "listed_days": 2200},
        {"code": "688981.SH", "name": "中芯国际",   "day_change_pct": 0.070,
         "price": 114.86, "volume_ratio": 4.8, "float_market_cap": 2297e8,
         "sector_2": "半导体", "sector_change_pct": 0.032, "sector_rise_ratio": 0.70,
         "listed_days": 1200},
        {"code": "002460.SZ", "name": "赣锋锂业",   "day_change_pct": 0.056,
         "price": 46.17,  "volume_ratio": 2.8, "float_market_cap": 559e8,
         "sector_2": "能源金属", "sector_change_pct": 0.028, "sector_rise_ratio": 0.65,
         "listed_days": 3500},
        {"code": "002719.SZ", "name": "ST麦趣",     "day_change_pct": 0.099,
         "price": 6.49,   "volume_ratio": 8.0, "float_market_cap": 11e8,
         "sector_2": "休闲食品", "sector_change_pct": 0.010, "sector_rise_ratio": 0.50,
         "listed_days": 2000},
    ]


def demo():
    print("\n" + "=" * 78)
    print("  CASE-23C 龙头战法 demo -- A 股化首板战法")
    print("=" * 78)

    # 1) 拉今日候选 (mock)
    print("\n[1] 模拟当日全市场涨跌数据 (12 只代表性标的)")
    today_stocks = _build_mock_today_stocks()
    for s in sorted(today_stocks, key=lambda x: x["day_change_pct"], reverse=True):
        st = " (ST!)" if "ST" in s["name"].upper() or "*" in s["name"] else ""
        print(f"  {s['code']}  {s['name']:<10s}{st}  涨幅 {s['day_change_pct']:+.2%}  "
              f"价 {s['price']:>7.2f}  量比 {s['volume_ratio']:.1f}  "
              f"流通市值 {s['float_market_cap']/1e8:>5.0f}亿")

    # 2) 应用 v1 5 大法则 + v2 硬规则补丁 + 板块共振
    print("\n[2] 应用筛选法则 (v1 5 法则 + v2 3 条补丁):")
    print("    v1-1: 涨幅 > 5%")
    print("    v1-2: 涨幅榜前 50")
    print("    v1-3: 流通市值 30-500 亿")
    print("    v1-4: 量比 > 2.0")
    print("    v1-5: ST / 退市 直接排除")
    print("    v2-6: 涨幅 < 9.5%   涨停板买不到, T+1 高开污染统计")
    print("    v2-7: 上市天数 >= 60 个交易日   排除次新股")
    print("    v2-8: 板块共振   sector_2 当日涨幅 >= 0.5% 且上涨家数占比 >= 40%")

    candidates = filter_dragon_candidates(
        today_stocks,
        min_change=0.05,
        max_price=30,
        mcap_range=(30e8, 500e8),
        min_volume_ratio=2.0,
        require_sector_resonance=True,
    )

    print(f"\n[3] 筛选后龙头候选: {len(candidates)} 只 (按 dragon_score 排序)")
    for c in candidates:
        sec = c.get("sector_2", "")
        s_chg = c.get("sector_change_pct", 0) or 0
        s_rise = c.get("sector_rise_ratio", 0) or 0
        print(f"  [{c['rank_in_top']:>2}] {c['code']}  {c['name']:<10s}  "
              f"涨幅 {c['day_change_pct']:+.2%}  量比 {c['volume_ratio']:.1f}  "
              f"市值 {c['float_market_cap']/1e8:.0f}亿  价 {c['price']:.2f}  "
              f"| 板块 [{sec}] {s_chg:+.2%} 涨家占比 {s_rise:.0%}  "
              f"-> dragon_score {c['dragon_score']:+.2f}")

    if not candidates:
        print("  (无符合条件的标的)")
        return

    # 3) 给 Top 3 算入场参数
    print(f"\n[4] Top 3 入场参数 (Base Hit 小赢 + 2:1 盈亏比 + 1% 单笔风险)")
    entry_calc = DragonEntryExit(
        capital=1_000_000,
        risk_per_trade_pct=0.01,
        target_payoff_ratio=2.0,
    )
    for c in candidates[:3]:
        entry = entry_calc.calc_entry(c)
        print(f"\n  {entry['code']}  {entry['name']}")
        print(f"    入场价: {entry['entry_price']:.2f}")
        print(f"    止损价: {entry['stop_loss']:.2f}  (-{(entry['entry_price']-entry['stop_loss'])/entry['entry_price']:.1%})")
        print(f"    目标价: {entry['target']:.2f}  (+{(entry['target']-entry['entry_price'])/entry['entry_price']:.1%})")
        print(f"    数量:   {entry['quantity']} 股")
        print(f"    金额:   {entry['amount']:,.0f} 元")
        print(f"    最大亏损: {entry['max_loss']:,.0f} 元")
        print(f"    最大盈利: {entry['max_gain']:,.0f} 元")
        print(f"    盈亏比: {entry['payoff_ratio']:.1f}:1")
        print(f"    最长持仓: {entry['max_hold_minutes']} 分钟")

    print(f"\n{'='*78}")
    print("  铁律提醒")
    print(f"{'='*78}")
    print("  1. Base Hit 小赢 -- 不追求本垒打, 每股 0.3-0.8 元就走")
    print("  2. 盈亏比 ≥ 2:1 -- 数学保证: 胜率 33% 也能盈利")
    print("  3. 当日累计亏损 ≥ 2% -- 强制收手, 不报复性交易")
    print("  4. 连续 3 笔亏损 -- 暂停 1 小时, 让情绪冷静")
    print("  5. 不留隔夜 -- 收盘前 10 分钟全部平仓")
    print()
    print("  重要警示:")
    print("    Ross Cameron 在 YouTube 公开实盘记录, 用 583 美元做到千万级")
    print("    但 FTC (美国联邦贸易委员会) 起诉指出: 99% 学员复制后亏损")
    print("    这个策略需要极高的纪律性, 不是技术问题, 是心理问题")


if __name__ == "__main__":
    demo()
