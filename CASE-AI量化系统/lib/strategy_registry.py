# -*- coding: utf-8 -*-
# Author：@跟陈博士学AI
# 25-AI量化系统 策略注册中心 -- 将多类策略统一封装为 (code, market, capital) -> signal
"""
StrategyRegistry -- 策略注册中心 (单只股票视角)

设计理念:
    - live_loop 每轮对 watch 池的每只股票调一次 evaluator
    - 我们这里把"全市场扫描"型策略 (多因子/龙头) 也包装成"单只股票评估"模式
    - 每个策略都返回统一格式: {"side": "buy"/"sell"/"hold", "strategy": str, "reason": str}

A 股 T+1，策略按日 K 波段，不做 5 分钟日内。

    [趋势跟踪]
        - macd_1d / ma20_hold / dual_ma_1d / macd_vol / turtle_donchian / turtle_adx / adx_adaptive

    [均值回归]
        - rsi_reversal / boll_revert / bias_revert

    [动量与背离]
        - momentum_roc / macd_divergence

    [震荡网格]
        - grid_classic / chan_grid

    [结构与战法]
        - dragon_picker_v1 / dragon_picker_v2
        - chan_buy1 / chan_buy2 / chan_buy3 / chan_buy2_ml / chan_third_buy

    [基本面与选股]
        - multi_factor_top / fund_quality

    [机器学习]
        - ml_xgb / ml_lgb
          第 11 课日 K 因子 + 滚动训练。涨概率 > 0.6 买, < 0.4 卖

    缠论一买、二买、三买和机器学习二买是第 13 课；缠论三买（第 9 讲）和中枢网格是另一套结构算法。

每个策略都做了健壮兜底: 数据不足 / 异常 -> 返回 hold (不会让循环挂掉)
"""

from __future__ import annotations
import math
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

import pandas as pd


# ============================================================
# 数据类
# ============================================================

@dataclass
class StrategyMeta:
    """策略元信息"""
    name: str                      # 唯一标识 (路由表里用)
    label: str                     # 中文显示名
    group: str                     # 分组 (技术指标 / 量化选股 / 龙头动量 / 震荡网格)
    description: str             # 一句话说明 (列表/折叠区仍用)
    evaluator: Callable            # (code, market, capital) -> dict
    scenario: str = ""             # 适用场景 (弹窗)
    rules: str = ""              # 规则要点 (弹窗)
    example: str = ""            # 简短示例 (弹窗)


# ============================================================
# 注册中心
# ============================================================

_REGISTRY: Dict[str, StrategyMeta] = {}


def register(
    name: str,
    label: str,
    group: str,
    description: str = "",
    *,
    scenario: str = "",
    rules: str = "",
    example: str = "",
):
    """装饰器: 注册一个策略 (scenario / rules / example 供持仓说明弹窗结构化展示)"""
    def deco(fn: Callable) -> Callable:
        _REGISTRY[name] = StrategyMeta(
            name=name,
            label=label,
            group=group,
            description=description,
            evaluator=fn,
            scenario=scenario,
            rules=rules,
            example=example,
        )
        return fn
    return deco


GROUP_ORDER = [
    "趋势跟踪", "均值回归", "动量与背离",
    "震荡网格", "结构与战法", "基本面与选股", "机器学习",
]


def get_strategy(name: str) -> Optional[StrategyMeta]:
    return _REGISTRY.get(name)


def _meta_item(meta: StrategyMeta) -> Dict[str, Any]:
    return {
        "name":        meta.name,
        "label":       meta.label,
        "display":     f"[{meta.group}] {meta.label}",
        "group":       meta.group,
        "description": meta.description,
        "scenario":    meta.scenario,
        "rules":       meta.rules,
        "example":     meta.example,
    }


def list_strategies() -> List[Dict[str, str]]:
    """按 GROUP_ORDER 排序, 供回测下拉。"""
    rank = {g: i for i, g in enumerate(GROUP_ORDER)}
    out = [_meta_item(meta) for meta in _REGISTRY.values()]
    out.sort(key=lambda x: (rank.get(x["group"], 99), x["name"]))
    return out


def list_groups() -> Dict[str, List[Dict[str, str]]]:
    """按 GROUP_ORDER 聚合, 组内保持注册顺序。"""
    grouped: Dict[str, List[Dict[str, str]]] = {}
    for meta in _REGISTRY.values():
        grouped.setdefault(meta.group, []).append(_meta_item(meta))
    return {g: grouped[g] for g in GROUP_ORDER if g in grouped}


# ============================================================
# 通用工具: 拉 K 线 (容错)
# ============================================================

def _safe_kline(market, code: str, period: str, count: int):
    """拉 K 线, 拿不到返回 None"""
    try:
        df = market.get_recent_kline(code, period=period, count=count)
        if df is None or len(df) == 0:
            return None
        return df
    except Exception:
        return None


def _hold(strategy: str, reason: str = "") -> dict:
    return {"side": "hold", "strategy": strategy, "reason": reason}


def _signal(side: str, strategy: str, reason: str = "") -> dict:
    return {"side": side, "strategy": strategy, "reason": reason}


def _macd_cross_from_close(close, strategy_name: str, bar_desc: str) -> dict:
    """
    经典 MACD: 快线=收盘 EMA12, 慢线=收盘 EMA26, DIF=快线-慢线, DEA=DIF 的 EMA9;
    信号: DIF 上穿 DEA 买, 下穿卖。
    bar_desc: 用于 reason 里标明周期, 如 "日K"
    """
    if close is None or len(close) < 30:
        return _hold(strategy_name, "K 线不足 30 根")
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    dif = ema12 - ema26
    dea = dif.ewm(span=9, adjust=False).mean()
    if len(dif) < 2:
        return _hold(strategy_name, "DIF 序列过短")
    prev = dif.iloc[-2] - dea.iloc[-2]
    curr = dif.iloc[-1] - dea.iloc[-1]
    if prev <= 0 and curr > 0:
        return _signal("buy", strategy_name,
                       f"[{bar_desc}] 金叉 12/26/9 DIF-DEA={curr:+.4f}")
    if prev >= 0 and curr < 0:
        return _signal("sell", strategy_name,
                       f"[{bar_desc}] 死叉 12/26/9 DIF-DEA={curr:+.4f}")
    return _hold(strategy_name, f"[{bar_desc}] 无交叉")


# ============================================================
# 策略 1: MACD 日线 (经典 12/26/9, 日 K 收盘)
# ============================================================

@register(
    name="macd_1d",
    label="MACD·日K线 (12/26/9·波段)",
    group="趋势跟踪",
    description="基于日 K 收盘价的 12/26/9, 与常见软件「日线 MACD」一致。适合多日持仓与 T+1 下的波段决策。",
    scenario="更做隔日、波段；与 A 股 T+1「今日买明日卖」的节奏更接近时。",
    rules="用日 K 收盘价算经典 MACD(12/26/9)，金叉偏买、死叉偏卖，每根 K 代表一个交易日。",
    example="连续回调后日线出现金叉，常作为波段关注信号之一（是否下单仍看资金与风控）。",
)
def strat_macd_1d(code: str, market, capital: float) -> dict:
    df = _safe_kline(market, code, "1d", 80)
    if df is None:
        return _hold("macd_1d", "无日 K 线")
    close = df["close"].astype(float)
    return _macd_cross_from_close(close, "macd_1d", "日K")


# ============================================================
# 策略 2: MA20 持股法 (日 K, 价格与 MA20 交叉)
# ============================================================
# 收盘价 vs 简单 MA20:
#   - 前一日收盘在 MA20 及以下、当日收盘站上 MA20 -> buy
#   - 前一日收盘在 MA20 及以上、当日收盘跌穿 MA20 -> sell

@register(
    name="ma20_hold",
    label="MA20 持股法 (日K 突破/跌破)",
    group="趋势跟踪",
    description="日 K 线: 收盘向上突破 MA20 买入, 向下跌破 MA20 卖出 (站上持股、跌破离场)",
    scenario="想用最简单的「20 日线」做波段过滤: 站上认为趋势转强可介入, 跌破则离场观望时。",
    rules="用日 K 收盘价计算 MA20。前一日收盘 ≤ MA20 且当日收盘 > MA20 → 偏买；前一日收盘 ≥ MA20 且当日收盘 < MA20 → 偏卖；其余观望。",
    example="整理后首日阳线收盘站上 MA20 触发买入；之后若回调收在 MA20 下方, 触发卖出离场。",
)
def strat_ma20_hold(code: str, market, capital: float) -> dict:
    df = _safe_kline(market, code, "1d", 80)
    if df is None or len(df) < 22:
        return _hold("ma20_hold", "日 K 不足 22 根")
    close = df["close"].astype(float)
    ma20 = close.rolling(20).mean()
    if math.isnan(ma20.iloc[-1]) or math.isnan(ma20.iloc[-2]) or float(ma20.iloc[-1]) <= 0:
        return _hold("ma20_hold", "MA20 不可用")
    prev_c = float(close.iloc[-2])
    curr_c = float(close.iloc[-1])
    prev_m = float(ma20.iloc[-2])
    curr_m = float(ma20.iloc[-1])
    if prev_c <= prev_m and curr_c > curr_m:
        return _signal(
            "buy",
            "ma20_hold",
            f"日K 收盘 {prev_c:.2f}->{curr_c:.2f} 突破 MA20 {prev_m:.2f}->{curr_m:.2f}",
        )
    if prev_c >= prev_m and curr_c < curr_m:
        return _signal(
            "sell",
            "ma20_hold",
            f"日K 收盘 {prev_c:.2f}->{curr_c:.2f} 跌破 MA20 {prev_m:.2f}->{curr_m:.2f}",
        )
    return _hold(
        "ma20_hold",
        f"收盘 {curr_c:.2f} MA20 {curr_m:.2f} 无穿越",
    )


# ============================================================
# 策略 3: 多因子轻量版 (MOM_1M + RSI_14 + BIAS_20 合成)
# ============================================================
# 全市场截面多因子原版是"全市场截面 + IC 加权", 单只股做不了截面比较
# 这里改成"绝对阈值"版: 三个因子分别打分 [-1, +1] 后求平均

@register(
    name="multi_factor_top",
    label="多因子轻量 (动量+RSI+乖离)",
    group="基本面与选股",
    description="日线 MOM_1M + RSI_14 + BIAS_20 合成 alpha, > 0.3 买, < -0.3 卖",
    scenario="单只股票也想用「动量 + 超买超卖 + 乖离」综合打分，而不是只看一条均线时。",
    rules="在日 K 上算三类因子并归一后取平均得到 alpha；alpha 高于阈值偏买入，低于负阈值偏卖出；中间区间观望。",
    example="alpha 从负区间一举升到阈值之上，表示多因子同时转强，可能触发买入侧信号。",
)
def strat_multi_factor(code: str, market, capital: float) -> dict:
    df = _safe_kline(market, code, "1d", 200)
    if df is None or len(df) < 30:
        return _hold("multi_factor_top", "日 K 线不足 30 根")
    close = df["close"].astype(float)

    # 因子 1: 1 个月动量 (21 日涨跌幅), 映射到 [-1, +1] (15% 涨幅 -> 1.0)
    if len(close) >= 22:
        mom_1m = close.iloc[-1] / close.iloc[-22] - 1.0
    else:
        mom_1m = 0.0
    f_mom = max(-1.0, min(1.0, mom_1m / 0.15))

    # 因子 2: RSI_14, 偏离 50 越远动能越强; > 70 超买 (反转减分), < 30 超卖 (反转加分)
    delta = close.diff()
    gain = delta.where(delta > 0, 0).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    rs = gain / loss.replace(0, 1e-9)
    rsi = (100 - 100 / (1 + rs)).iloc[-1]
    if rsi > 70:
        f_rsi = -((rsi - 70) / 30)        # 超买扣分
    elif rsi < 30:
        f_rsi = (30 - rsi) / 30           # 超卖加分 (反转买入)
    else:
        f_rsi = (rsi - 50) / 50           # 中间区随动能

    # 因子 3: BIAS_20 (乖离率), 过涨易回调 (取负号)
    if len(close) >= 20:
        ma20 = close.rolling(20).mean().iloc[-1]
        bias = (close.iloc[-1] - ma20) / ma20 if ma20 > 0 else 0.0
    else:
        bias = 0.0
    f_bias = max(-1.0, min(1.0, -bias / 0.10))   # +/- 10% 乖离 -> +/- 1.0

    alpha = (f_mom + f_rsi + f_bias) / 3
    reason = (f"MOM_1M={mom_1m:+.2%} RSI={rsi:.1f} BIAS={bias:+.2%} "
              f"-> alpha={alpha:+.2f}")

    if alpha > 0.30:
        return _signal("buy", "multi_factor_top", reason)
    if alpha < -0.30:
        return _signal("sell", "multi_factor_top", reason)
    return _hold("multi_factor_top", reason)


# ============================================================
# 策略 4: 龙头动量 (单只股票版)
# ============================================================
# 入场版本写在策略名上: dragon_picker_v1 / dragon_picker_v2
# dragon_score 只用于排序/展示, 不再另设 1.5 门槛
# 出场仍是战法: 当日跌幅 > 3% 或 从当日最高回撤 > 3%

_DRAGON_LIVE_CTX = {"day": None, "meta": None, "sector": None}


def _dragon_live_ctx():
    """当天读一次 status + 近 12 日板块, 给单票套 v1/v2"""
    from datetime import date, timedelta
    from dragon_strategy.dragon_backtest import load_status_meta, load_sector_panel
    day = date.today().isoformat()
    if _DRAGON_LIVE_CTX["day"] != day:
        end = date.today()
        start = end - timedelta(days=12)
        _DRAGON_LIVE_CTX["meta"] = load_status_meta()
        _DRAGON_LIVE_CTX["sector"] = load_sector_panel(
            start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"), sector_level=2)
        _DRAGON_LIVE_CTX["day"] = day
    return _DRAGON_LIVE_CTX


def _eval_dragon_us(code: str, version: str, day_change: float, vol_ratio: float,
                    cur_price: float, asof) -> dict:
    """美股龙头。日K和分组都来自 trade_stock_us_daily。

    不套 A 股的流通市值、涨停 9.5% 和上市天数: 库里没有流通股本和上市日期,
    也没有涨停。价格上限按 400 美元。v2 仍要求分组等权涨幅和上涨家数占比达标。
    """
    from dragon_strategy.dragon_picker import (
        calc_dragon_score, filter_dragon_candidates, dragon_version_params,
        US_MAX_PRICE,
    )
    from morning_brief.lib.rotation_runner import us_sector_asof

    strat_name = f"dragon_picker_{version}"
    sector = us_sector_asof(code, asof)
    params = dragon_version_params(version)
    snap = {
        "code": code,
        "name": "",
        "day_change_pct": day_change,
        "price": cur_price,
        "volume_ratio": vol_ratio,
        "float_market_cap": None,
        "listed_days": None,
        "sector_2": sector["sector"],
        "sector_change_pct": sector["sector_change_pct"],
        "sector_rise_ratio": sector["sector_rise_ratio"],
        "penalize_limit_up": False,
    }
    passed = filter_dragon_candidates(
        [snap],
        max_price=US_MAX_PRICE,
        require_sector_resonance=params["require_sector_resonance"],
        max_change=1.0,
        min_listed_days=0,
        check_mcap=False,
    )
    score = passed[0]["dragon_score"] if passed else calc_dragon_score(snap)
    s_chg = sector["sector_change_pct"]
    s_rise = sector["sector_rise_ratio"]
    board = ""
    if s_chg is not None:
        board = f" 板涨={s_chg:+.2%}/{s_rise:.0%}" if s_rise is not None else f" 板涨={s_chg:+.2%}"
    sector_name = sector["sector"] or "未分组"
    reason = (f"{version} 美股 日涨={day_change:+.2%} 量比={vol_ratio:.2f} "
              f"价={cur_price:.2f} 板块={sector_name}"
              f"{board} 龙头分={score:.2f}")
    if passed:
        return _signal("buy", strat_name, reason)
    return _hold(strat_name, reason)


def _eval_dragon(code: str, market, capital: float, version: str) -> dict:
    import pandas as pd
    from dragon_strategy.dragon_picker import (
        calc_dragon_score, filter_dragon_candidates, dragon_version_params,
    )
    strat_name = f"dragon_picker_{version}"

    df_d = _safe_kline(market, code, "1d", 10)
    if df_d is None or len(df_d) < 6:
        return _hold(strat_name, "日 K 不足 6 根")
    prev_close = float(df_d["close"].iloc[-2])
    avg_vol_5d = float(df_d["volume"].iloc[-6:-1].mean()) if "volume" in df_d.columns else 0
    last = df_d.iloc[-1]
    cur_price = float(last["close"])
    today_high = float(last["high"]) if "high" in df_d.columns else cur_price
    today_vol = float(last["volume"]) if "volume" in df_d.columns else 0

    day_change = (cur_price / prev_close - 1.0) if prev_close > 0 else 0.0
    vol_ratio = (today_vol / avg_vol_5d) if avg_vol_5d > 0 else 0.0

    drawdown = (cur_price / today_high - 1.0) if today_high > 0 else 0.0
    if day_change < -0.03 or drawdown < -0.03:
        return _signal("sell", strat_name,
                       f"日内 chg={day_change:+.2%} 回撤={drawdown:+.2%}")

    if str(code).upper().endswith(".US"):
        return _eval_dragon_us(
            code, version, day_change, vol_ratio, cur_price, df_d.index[-1],
        )

    ctx = _dragon_live_ctx()
    meta = ctx["meta"]
    if meta is None or meta.empty or code not in meta.index:
        return _hold(strat_name, "trade_stock_status 无此股, 无法套 v1/v2")

    meta_row = meta.loc[code]
    name = str(meta_row.get("stock_name") or "")
    sector_2 = str(meta_row.get("sector_2") or "")
    fshare = float(meta_row.get("float_shares") or 0)
    list_date = meta_row.get("list_date")
    listed_days = int((pd.Timestamp.today().normalize() - list_date).days) if pd.notna(list_date) else None

    s_chg, s_rise = None, None
    sector = ctx["sector"]
    if sector_2 and sector is not None and not sector.empty:
        try:
            sub = sector.xs(sector_2, level=0)
            sec_row = sub.loc[sub.index.max()]
            s_chg = float(sec_row["change_pct"]) if pd.notna(sec_row["change_pct"]) else None
            s_rise = float(sec_row["rise_ratio"]) if pd.notna(sec_row["rise_ratio"]) else None
        except KeyError:
            s_chg, s_rise = None, None

    snap = {
        "code": code, "name": name,
        "day_change_pct": day_change, "price": cur_price,
        "volume_ratio": vol_ratio, "float_market_cap": fshare * cur_price,
        "listed_days": listed_days, "sector_2": sector_2,
        "sector_change_pct": s_chg, "sector_rise_ratio": s_rise,
    }
    params = dragon_version_params(version)
    passed = filter_dragon_candidates(
        [snap],
        require_sector_resonance=params["require_sector_resonance"],
        max_change=params["max_change"],
        min_listed_days=params["min_listed_days"],
    )
    score = passed[0]["dragon_score"] if passed else calc_dragon_score(snap)
    board = ""
    if s_chg is not None:
        board = f" 板涨={s_chg:+.2%}/{s_rise:.0%}" if s_rise is not None else f" 板涨={s_chg:+.2%}"
    reason = (f"{version} 日涨={day_change:+.2%} 量比={vol_ratio:.2f} "
              f"价={cur_price:.2f} 市值={fshare * cur_price / 1e8:.0f}亿"
              f"{board} 龙头分={score:.2f}")
    if passed:
        return _signal("buy", strat_name, reason)
    return _hold(strat_name, reason)


@register(
    name="dragon_picker_v1",
    label="龙头首板战法 v1",
    group="结构与战法",
    description="v1 五法则对照: 涨幅>5%、价<30、市值 30–500 亿、量比>2、非 ST。不限涨停/次新/共振。美股价<400美元, 不套市值。",
    scenario="对照课件五法则, 或想看涨停板/次新会不会混进来时。",
    rules="入场：涨幅>5%、价<30、流通市值 30–500 亿、量比>2、非 ST。不限涨停、次新、板块共振。出场：当日跌幅>3% 或从当日最高回撤>3%。美股日K来自 trade_stock_us_daily，板块用采集分组的等权涨幅；价格上限 400 美元，不设流通市值，涨幅超过 9% 不减分。",
    example="一只票涨 7%、量比 4、价 12、市值 80 亿 -> 买入；从当日高点回撤超过 3% -> 卖出。",
)
def strat_dragon_v1(code: str, market, capital: float) -> dict:
    return _eval_dragon(code, market, capital, "v1")


@register(
    name="dragon_picker_v2",
    label="龙头首板战法 v2",
    group="结构与战法",
    description="v2 硬过滤: 涨幅 5%–9.5%、价<30、市值 30–500 亿、量比>2、非 ST、上市≥60 日、板块共振。美股不套涨停、上市天数和市值。",
    scenario="关注当日强势、放量上攻的短线博弈，愿意承担较大波动时。",
    rules="入场：涨幅 5%–9.5%、价<30、流通市值 30–500 亿、量比>2、非 ST、上市≥60 日、板块共振。出场：当日跌幅>3% 或从当日最高回撤>3%。美股板块用采集分组，涨幅不低于 0.5% 且上涨家数占比不低于 40% 才买；不套 9.5% 涨停上限、60 日上市和流通市值，价格上限 400 美元。",
    example="一只票涨 7%、量比 4、价 12、市值 80 亿、板块涨 2% 且涨家过半 -> 买入；从当日高点回撤超过 3% -> 卖出。",
)
def strat_dragon_v2(code: str, market, capital: float) -> dict:
    return _eval_dragon(code, market, capital, "v2")


# ============================================================
# 策略 6: RSI 反转（经典超买超卖 + 穿越确认）
# ============================================================
# 经典 RSI: RSI<30 买、RSI>70 卖, period=14
# 这里在原逻辑基础上 + "穿越确认"（减少在强趋势里反复触发）:
#   - RSI 上穿 30  -> 买  (从超卖反弹, 比单纯 <30 更稳, 避免抄底抄到一半)
#   - RSI 下穿 70  -> 卖  (从超买回落, 减少在强趋势里被洗下车)
# 适用: 震荡 / 反转性强的票 (银行 / 公用 / 部分大盘蓝筹)

@register(
    name="rsi_reversal",
    label="RSI 反转 (14·30/70 穿越)",
    group="均值回归",
    description="日 K RSI(14) 上穿 30 买入, 下穿 70 卖出 (经典 RSI + 穿越确认)",
    scenario="震荡市 / 反转性强的票（银行、公用事业、部分大盘蓝筹），不追趋势, 抓「跌透了反弹」「涨过头回落」的边界。",
    rules="日 K 收盘算 RSI(14)。RSI 从 ≤30 上穿 30 -> 买（超卖反弹确认）；从 ≥70 下穿 70 -> 卖（超买回落确认）；其他时间观望。比裸 <30 / >70 信号更稳, 避免在强趋势里被反复打脸。",
    example="平安银行 RSI 跌到 28, 次日反弹收 32 -> 触发买入；后续涨到 RSI 73, 次日回落收 68 -> 触发卖出。",
)
def strat_rsi_reversal(code: str, market, capital: float) -> dict:
    df = _safe_kline(market, code, "1d", 60)
    if df is None or len(df) < 20:
        return _hold("rsi_reversal", "日 K 不足 20 根")
    close = df["close"].astype(float)
    delta = close.diff()
    gain = delta.where(delta > 0, 0).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    rs = gain / loss.replace(0, 1e-9)
    rsi = 100 - 100 / (1 + rs)
    if len(rsi) < 2 or math.isnan(rsi.iloc[-1]) or math.isnan(rsi.iloc[-2]):
        return _hold("rsi_reversal", "RSI 序列不足")
    prev = float(rsi.iloc[-2])
    curr = float(rsi.iloc[-1])
    if prev <= 30 < curr:
        return _signal("buy", "rsi_reversal",
                       f"RSI 上穿 30: {prev:.1f} -> {curr:.1f} (超卖反弹)")
    if prev >= 70 > curr:
        return _signal("sell", "rsi_reversal",
                       f"RSI 下穿 70: {prev:.1f} -> {curr:.1f} (超买回落)")
    return _hold("rsi_reversal", f"RSI={curr:.1f} 中性区")


# ============================================================
# 策略 7: 布林带均值回归 (BollingerBands)
# ============================================================
# 经典版: 触下轨买、触上轨卖, period=20, dev=2.0
# 完全保留原参数; 加 "中轨止盈" 防止持仓涨到中轨就吐回去
#   - 收盘 < 下轨   -> 买
#   - 收盘 > 上轨   -> 卖 (止盈)
#   - 收盘 < 中轨且前一日 >= 中轨 -> 卖 (跌破中轨止盈)

@register(
    name="boll_revert",
    label="布林带均值回归 (20·2σ)",
    group="均值回归",
    description="日 K 布林带(20, 2σ) 触下轨买、触上轨卖 + 跌破中轨止盈",
    scenario="波动有规律的票, 价格围绕中线均值上下震荡, 想做「触下轨吃货, 触上轨止盈」的均值回归操作。",
    rules="日 K 收盘价计算 MA20 与 ±2σ 三条线。收盘 < 下轨 -> 买; 收盘 > 上轨 -> 卖; 持仓时收盘从中轨上方跌破中轨 -> 卖 (止盈, 防回吐)。波动率扩张到强趋势时容易追涨杀跌, 需配合风控。",
    example="纳指 ETF 价格触下轨 1.65 触发买入; 反弹到 1.78 突破上轨 -> 卖出止盈。",
)
def strat_boll_revert(code: str, market, capital: float) -> dict:
    df = _safe_kline(market, code, "1d", 60)
    if df is None or len(df) < 25:
        return _hold("boll_revert", "日 K 不足 25 根")
    close = df["close"].astype(float)
    ma = close.rolling(20).mean()
    std = close.rolling(20).std(ddof=0)
    upper = ma + 2.0 * std
    lower = ma - 2.0 * std
    if math.isnan(ma.iloc[-1]):
        return _hold("boll_revert", "BOLL 序列不足")
    cur = float(close.iloc[-1])
    prev = float(close.iloc[-2])
    mid_cur = float(ma.iloc[-1])
    mid_prev = float(ma.iloc[-2]) if not math.isnan(ma.iloc[-2]) else mid_cur
    up_cur = float(upper.iloc[-1])
    lo_cur = float(lower.iloc[-1])
    if cur < lo_cur:
        return _signal("buy", "boll_revert",
                       f"收盘 {cur:.2f} < 下轨 {lo_cur:.2f} (中轨 {mid_cur:.2f})")
    if cur > up_cur:
        return _signal("sell", "boll_revert",
                       f"收盘 {cur:.2f} > 上轨 {up_cur:.2f} (中轨 {mid_cur:.2f})")
    if prev >= mid_prev and cur < mid_cur:
        return _signal("sell", "boll_revert",
                       f"跌破中轨 {mid_cur:.2f} 止盈 (前 {prev:.2f}/中 {mid_prev:.2f})")
    return _hold("boll_revert",
                 f"在 [{lo_cur:.2f}, {up_cur:.2f}] 之间 中轨 {mid_cur:.2f}")


# ============================================================
# 策略 8: 乖离率均值回归 (BIAS)
# ============================================================
# 经典阈值: BIAS<-6% 买, BIAS>3% 卖, MA20
# 适用: 短期超跌反弹 / 涨多回调 -- 节奏型票 (大盘蓝筹反弹)

@register(
    name="bias_revert",
    label="乖离率均值回归 (BIAS·20)",
    group="均值回归",
    description="日 K 乖离率 < -6% 买入 (超跌), > 3% 卖出 (超涨)",
    scenario="跟随 20 日均线节奏运行的票, 想做「跌得离均线太远 -> 反弹补涨」「涨得离均线太远 -> 回调收口」的中期均值回归。",
    rules="BIAS = (收盘 - MA20) / MA20。BIAS < -6% -> 偏买 (超跌反弹); BIAS > 3% -> 偏卖 (涨过头, 注意不对称: 上涨节奏比下跌温和)。趋势单边市 (持续创新高 / 新低) 容易钝化。",
    example="贵州茅台日 K 收盘 1450, MA20 在 1545, BIAS = -6.15% -> 触发买入; 涨到 BIAS = 3.5% -> 卖出。",
)
def strat_bias_revert(code: str, market, capital: float) -> dict:
    df = _safe_kline(market, code, "1d", 60)
    if df is None or len(df) < 25:
        return _hold("bias_revert", "日 K 不足 25 根")
    close = df["close"].astype(float)
    ma20 = close.rolling(20).mean()
    if math.isnan(ma20.iloc[-1]) or ma20.iloc[-1] <= 0:
        return _hold("bias_revert", "MA20 不可用")
    cur = float(close.iloc[-1])
    bias = (cur - float(ma20.iloc[-1])) / float(ma20.iloc[-1])
    reason = f"BIAS={bias:+.2%} 收盘 {cur:.2f}/MA20 {float(ma20.iloc[-1]):.2f}"
    if bias < -0.06:
        return _signal("buy", "bias_revert", "超跌 " + reason)
    if bias > 0.03:
        return _signal("sell", "bias_revert", "超涨 " + reason)
    return _hold("bias_revert", reason)


# ============================================================
# 策略 9: 海龟唐奇安通道（经典简化版）
# ============================================================
# 原版含: 唐奇安通道 20 入 / 10 出 + ATR 仓位 + 金字塔加仓 + 2N 止损
# 本工作台为「信号 + 路由」框架, 仓位由风控统一管, 只输出方向信号:
#   - 收盘 > 过去 20 日最高 (不含今日) -> buy  (突破入场)
#   - 收盘 < 过去 10 日最低 (不含今日) -> sell (跌破出场)
# 适用: 趋势性强的票 (科技成长 / 宽基 ETF / 跨境 ETF)

@register(
    name="turtle_donchian",
    label="海龟唐奇安通道 (20入/10出)",
    group="趋势跟踪",
    description="日 K 突破 20 日新高买入, 跌破 10 日新低卖出 (海龟唐奇安简化版, 不含 ATR 加仓)",
    scenario="趋势性强的标的: 科技成长 (中芯/宁德), 宽基 ETF, 跨境 ETF (纳指 ETF), 想等趋势确认再上车 / 跌破就走人时。",
    rules="收盘 > 过去 20 个交易日最高价 (不含今日) -> 买; 收盘 < 过去 10 个交易日最低价 (不含今日) -> 卖。趋势市赚大段, 震荡市频繁假突破被打脸 (需要止损 + 选标的)。",
    example="中芯国际经过整理, 收盘突破前 20 日最高的 102.5 -> 买入; 后续高位震荡, 跌破前 10 日最低 95.2 -> 卖出离场。",
)
def strat_turtle_donchian(code: str, market, capital: float) -> dict:
    df = _safe_kline(market, code, "1d", 60)
    if df is None or len(df) < 22:
        return _hold("turtle_donchian", "日 K 不足 22 根")
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)
    cur = float(close.iloc[-1])
    entry_high = float(high.iloc[-21:-1].max())   # 不含今日的过去 20 日
    exit_low = float(low.iloc[-11:-1].min())      # 不含今日的过去 10 日
    if cur > entry_high:
        return _signal("buy", "turtle_donchian",
                       f"收盘 {cur:.2f} 突破 20 日高 {entry_high:.2f}")
    if cur < exit_low:
        return _signal("sell", "turtle_donchian",
                       f"收盘 {cur:.2f} 跌破 10 日低 {exit_low:.2f}")
    return _hold("turtle_donchian",
                 f"在 [{exit_low:.2f}, {entry_high:.2f}] 之间, 收盘 {cur:.2f}")


# ============================================================
# 策略 5: 经典网格 (无状态版)
# ============================================================
# 经典网格为有状态「格子位置变化触发」策略
# 这里改成"无状态阈值版": 每轮根据日 K 收盘价在网格中的位置判断
#   - 价格落到下半区 (<= 第 2 格) -> buy (低吸)
#   - 价格冲到上半区 (>= 倒数第 2 格) -> sell (高抛)

@register(
    name="grid_classic",
    label="经典网格 (60 日区间 8 格)",
    group="震荡网格",
    description="过去 60 日 high/low 切 8 格, 价格在底部 2 格买, 顶部 2 格卖 (适合震荡市)",
    scenario="判断该股在一段时期内主要在箱体内震荡，想做「低位多吸、高位分批减」时。",
    rules="取约 60 个交易日最高价与最低价划成 8 格；现价落在最下两格偏买、最上两格偏卖；冲出区间上下沿另有止损/止盈类处理。",
    example="长期在箱体内运行时，价格回到区间下沿附近可能出现低吸类信号；单边趋势市则容易反复打脸。",
)
def strat_grid_classic(code: str, market, capital: float) -> dict:
    df_d = _safe_kline(market, code, "1d", 80)
    if df_d is None or len(df_d) < 60:
        return _hold("grid_classic", "日 K 不足 60 根")
    high_60 = float(df_d["high"].iloc[-60:].max())
    low_60 = float(df_d["low"].iloc[-60:].min())
    if high_60 <= low_60:
        return _hold("grid_classic", "网格区间无效")

    # 上下预留 2% 缓冲
    margin = (high_60 - low_60) * 0.02
    upper = high_60 + margin
    lower = low_60 - margin
    grid_size = (upper - lower) / 8

    cur_price = float(df_d["close"].iloc[-1])

    grid_idx = int((cur_price - lower) / grid_size) if grid_size > 0 else 4
    grid_idx = max(0, min(7, grid_idx))

    reason = (f"区间[{lower:.2f},{upper:.2f}] 当前={cur_price:.2f} "
              f"位于第 {grid_idx + 1}/8 格")

    # 出界处理: 跌破下界 (满仓套牢) 或 涨破上界 (踏空)
    if cur_price < lower:
        return _signal("sell", "grid_classic", f"跌破下界 {lower:.2f} -> 止损 " + reason)
    if cur_price > upper:
        return _signal("sell", "grid_classic", f"涨破上界 {upper:.2f} -> 止盈 " + reason)

    if grid_idx <= 1:        # 底部 2 格
        return _signal("buy", "grid_classic", "底部低吸 " + reason)
    if grid_idx >= 6:        # 顶部 2 格
        return _signal("sell", "grid_classic", "顶部高抛 " + reason)
    return _hold("grid_classic", reason)


# ============================================================
# 缠论一买 / 二买 / 三买（第 13 课 ChanAnalyzer）
# ============================================================

def _register_chan(name: str, label: str, scenario: str, example: str):
    @register(
        name=name,
        label=label,
        group="结构与战法",
        description="用第 13 课缠论分析器在日 K 上找买点。跌破结构、收盘涨幅达到 15%，或出现三卖，离场。",
        scenario=scenario,
        rules="买点要等下一根日 K 确认后才发单，再下一根开盘成交。止损用课程里的结构价：一买和二买看参照低点，三买看中枢上沿。收盘涨幅达到 15% 或三卖确认则卖出。",
        example=example,
    )
    def _eval(code: str, market, capital: float, _name=name) -> dict:
        from lib.chan_strategy import evaluate_chan
        return evaluate_chan(_name, code, market)
    return _eval


_register_chan(
    "chan_buy1",
    "缠论一买",
    "下跌趋势里出现背驰，想按第一类买点做波段时。",
    "两段中枢依次下移，后一段力度小于前一段，底分型确认后作为一买。",
)
_register_chan(
    "chan_buy2",
    "缠论二买",
    "一买之后的第一次回调没有跌破前低，想按第二类买点介入时。",
    "反弹后再回调，低点仍高于参照低点，记为二买。区间里没有一买时，用中枢后下跌笔的低点作参照。",
)
_register_chan(
    "chan_buy3",
    "缠论三买",
    "价格离开中枢后的回踩不再进中枢，想按第三类买点做突破回踩时。",
    "向上离开中枢，回踩低点仍高于中枢上沿。收盘跌回中枢上沿则结构失效。",
)


@register(
    name="chan_buy2_ml",
    label="缠论二买·机器学习",
    group="结构与战法",
    description="放宽二买后用 LightGBM 过滤。预测 20 日内最大涨幅能否超过 5%，概率达到 0.5 才买。第 13 讲。",
    scenario="二买信号偏多，想只做模型认为更容易涨的那一批时。",
    rules="买点等下一根日 K 确认后发单。训练样本来自课件股票池，并且 20 日结果已经走完。离场看参照低点、三卖、中枢下沿，或从持仓后最高收盘回撤 15%。美股用全部美股训练，A 股用课件里的六只。",
    example="确认日概率 0.62 则买入；概率 0.31 则跳过这笔二买。",
)
def strat_chan_buy2_ml(code: str, market, capital: float) -> dict:
    from lib.chan_buy2_ml import evaluate
    model = getattr(market, "chan_buy2_ml_model", None) if getattr(market, "chan_buy2_ml_ready", False) else None
    if getattr(market, "chan_buy2_ml_ready", False):
        return evaluate(code, market, model=model)
    return evaluate(code, market)


# ============================================================
# 第 11 讲: XGBoost / LightGBM 概率择时
# 回测整段先算概率序列; 模拟盘按代码和最后一根 K 缓存, 同一天不重复训练
# ============================================================

ML_MODELS = {"ml_xgb": "xgboost", "ml_lgb": "lightgbm"}
CHAN_STRATEGIES = {"chan_third_buy", "chan_grid"}


def _macd_lines(close):
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    dif = ema12 - ema26
    dea = dif.ewm(span=9, adjust=False).mean()
    return dif, dea


def _rsi_series(close, period=14):
    delta = close.diff()
    gain = delta.where(delta > 0, 0).rolling(period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(period).mean()
    rs = gain / loss.replace(0, 1e-9)
    return 100 - 100 / (1 + rs)


def _adx_last(high, low, close, period=14) -> Optional[float]:
    """Wilder ADX，返回最后一根；算不出则 None。"""
    if len(close) < period + 2:
        return None
    prev_c = close.shift(1)
    tr = pd.concat([
        (high - low),
        (high - prev_c).abs(),
        (low - prev_c).abs(),
    ], axis=1).max(axis=1)
    up = high.diff()
    dn = -low.diff()
    plus_dm = up.where((up > dn) & (up > 0), 0.0)
    minus_dm = dn.where((dn > up) & (dn > 0), 0.0)
    alpha = 1.0 / period
    atr = tr.ewm(alpha=alpha, adjust=False).mean()
    plus_di = 100 * plus_dm.ewm(alpha=alpha, adjust=False).mean() / atr.replace(0, math.nan)
    minus_di = 100 * minus_dm.ewm(alpha=alpha, adjust=False).mean() / atr.replace(0, math.nan)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, math.nan)
    adx = dx.ewm(alpha=alpha, adjust=False).mean()
    val = float(adx.iloc[-1])
    if math.isnan(val):
        return None
    return val


def prepare_backtest_market(market, strategy_name: str, full_df, code: str,
                            cutoff: str = "") -> None:
    """回测开跑前准备模型。概率择时挂整段概率，增强二买按截止日训练一次。"""
    if strategy_name in ML_MODELS:
        from ml_strategy.ml_prob_runner import run_ml_prob
        out = run_ml_prob(
            full_df,
            model_type=ML_MODELS[strategy_name],
            code=code,
            verbose=False,
        )
        market.ml_prob = out["prob_series"]
        return
    if strategy_name == "chan_buy2_ml":
        from lib.chan_buy2_ml import train_model
        market.chan_buy2_ml_ready = True
        market.chan_buy2_ml_model = train_model(code, cutoff or full_df.index[0])
        return
    if strategy_name in CHAN_STRATEGIES:
        from lib.chan_analyzer import ChanAnalyzer
        analyzer = ChanAnalyzer(full_df)
        analyzer.analyze()
        market.chan_df = analyzer.get_signal_df()


_CHAN_LIVE_CACHE: Dict[str, Any] = {"key": None, "df": None}


def _chan_last_row(code: str, market):
    """回测读 market.chan_df；模拟盘对最近 300 根分析并按日缓存。"""
    attached = getattr(market, "chan_df", None)
    vis = _safe_kline(market, code, "1d", 8)
    if vis is None or vis.empty:
        return None
    ts = vis.index[-1]
    if attached is not None and ts in attached.index:
        return attached.loc[ts]
    df = _safe_kline(market, code, "1d", 300)
    if df is None or len(df) < 80:
        return None
    key = f"{code}|{str(df.index[-1])[:10]}|{len(df)}"
    if _CHAN_LIVE_CACHE["key"] != key:
        from lib.chan_analyzer import ChanAnalyzer
        analyzer = ChanAnalyzer(df)
        analyzer.analyze()
        _CHAN_LIVE_CACHE["key"] = key
        _CHAN_LIVE_CACHE["df"] = analyzer.get_signal_df()
    sig_df = _CHAN_LIVE_CACHE["df"]
    if ts not in sig_df.index:
        return None
    return sig_df.loc[ts]


@register(
    name="dual_ma_1d",
    label="双均线 (SMA10/30)",
    group="趋势跟踪",
    description="日 K 快线 SMA10 上穿慢线 SMA30 买入，下穿卖出。第 5 讲。",
    scenario="想用课上第一套趋势规则做波段，比单根 MA20 多一条慢线确认时。",
    rules="SMA10 上穿 SMA30 买入，下穿卖出。震荡市交叉频繁。",
    example="整理后 10 日线上穿 30 日线买入；跌破慢线卖出。",
)
def strat_dual_ma_1d(code: str, market, capital: float) -> dict:
    df = _safe_kline(market, code, "1d", 80)
    if df is None or len(df) < 32:
        return _hold("dual_ma_1d", "日 K 不足 32 根")
    close = df["close"].astype(float)
    fast = close.rolling(10).mean()
    slow = close.rolling(30).mean()
    if math.isnan(fast.iloc[-1]) or math.isnan(slow.iloc[-1]):
        return _hold("dual_ma_1d", "均线未就绪")
    prev = float(fast.iloc[-2] - slow.iloc[-2])
    curr = float(fast.iloc[-1] - slow.iloc[-1])
    if prev <= 0 < curr:
        return _signal("buy", "dual_ma_1d",
                       f"SMA10 上穿 SMA30, diff={curr:+.3f}")
    if prev >= 0 > curr:
        return _signal("sell", "dual_ma_1d",
                       f"SMA10 下穿 SMA30, diff={curr:+.3f}")
    return _hold("dual_ma_1d", f"快{float(fast.iloc[-1]):.2f} 慢{float(slow.iloc[-1]):.2f} 无交叉")


@register(
    name="macd_vol",
    label="MACD 放量确认 (12/26/9)",
    group="趋势跟踪",
    description="日 K MACD 金叉且成交量大于 20 日均量才买，死叉卖。第 6 讲。",
    scenario="嫌缩量金叉假信号多，要量能跟上才上车时。",
    rules="金叉且当日量大于 20 日均量则买入；死叉卖出。无量金叉观望。",
    example="金叉当日量比 20 日均量高则买入；之后死叉卖出。",
)
def strat_macd_vol(code: str, market, capital: float) -> dict:
    df = _safe_kline(market, code, "1d", 80)
    if df is None or len(df) < 40:
        return _hold("macd_vol", "日 K 不足 40 根")
    if "volume" not in df.columns:
        return _hold("macd_vol", "无成交量")
    close = df["close"].astype(float)
    vol = df["volume"].astype(float)
    dif, dea = _macd_lines(close)
    prev = float(dif.iloc[-2] - dea.iloc[-2])
    curr = float(dif.iloc[-1] - dea.iloc[-1])
    vol_ma = float(vol.iloc[-20:].mean())
    last_vol = float(vol.iloc[-1])
    if prev <= 0 < curr:
        if vol_ma > 0 and last_vol > vol_ma:
            return _signal("buy", "macd_vol",
                           f"金叉放量 量={last_vol:.0f} 均量={vol_ma:.0f}")
        return _hold("macd_vol", f"金叉但缩量 {last_vol:.0f}/{vol_ma:.0f}")
    if prev >= 0 > curr:
        return _signal("sell", "macd_vol", "死叉离场")
    return _hold("macd_vol", "无交叉")


@register(
    name="adx_adaptive",
    label="ADX 自适应 (趋势MACD / 震荡RSI)",
    group="趋势跟踪",
    description="ADX 大于 25 走 MACD 金叉死叉；小于 20 走 RSI 超卖超买；中间观望。第 6 讲。",
    scenario="同一只票既有趋势段又有震荡段，不想死守一套指标时。",
    rules="ADX 大于 25：金叉买、死叉卖。ADX 小于 20：RSI 低于 30 买、高于 70 卖。20 到 25 之间观望。",
    example="ADX 升到 28 后 MACD 金叉买入；转入震荡且 RSI 到 72 卖出。",
)
def strat_adx_adaptive(code: str, market, capital: float) -> dict:
    df = _safe_kline(market, code, "1d", 80)
    if df is None or len(df) < 40:
        return _hold("adx_adaptive", "日 K 不足 40 根")
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)
    adx = _adx_last(high, low, close, 14)
    if adx is None:
        return _hold("adx_adaptive", "ADX 未就绪")
    if adx > 25:
        dif, dea = _macd_lines(close)
        prev = float(dif.iloc[-2] - dea.iloc[-2])
        curr = float(dif.iloc[-1] - dea.iloc[-1])
        if prev <= 0 < curr:
            return _signal("buy", "adx_adaptive", f"趋势 ADX={adx:.1f} MACD 金叉")
        if prev >= 0 > curr:
            return _signal("sell", "adx_adaptive", f"趋势 ADX={adx:.1f} MACD 死叉")
        return _hold("adx_adaptive", f"趋势 ADX={adx:.1f} 无交叉")
    if adx < 20:
        rsi = _rsi_series(close)
        curr_r = float(rsi.iloc[-1])
        if math.isnan(curr_r):
            return _hold("adx_adaptive", "RSI 未就绪")
        if curr_r < 30:
            return _signal("buy", "adx_adaptive", f"震荡 ADX={adx:.1f} RSI={curr_r:.1f}")
        if curr_r > 70:
            return _signal("sell", "adx_adaptive", f"震荡 ADX={adx:.1f} RSI={curr_r:.1f}")
        return _hold("adx_adaptive", f"震荡 ADX={adx:.1f} RSI={curr_r:.1f}")
    return _hold("adx_adaptive", f"过渡 ADX={adx:.1f}")


@register(
    name="turtle_adx",
    label="海龟 + ADX 过滤 (20入/10出)",
    group="趋势跟踪",
    description="唐奇安突破且 ADX 大于 15 才买，跌破 10 日低仍卖。第 8 讲。",
    scenario="海龟假突破多，只想在有趋势时开仓时。",
    rules="收盘突破 20 日高且 ADX 大于 15 则买入；跌破 10 日低卖出。ADX 只过滤开仓。",
    example="突破前高但 ADX 为 12 不买；ADX 到 18 再突破才买。",
)
def strat_turtle_adx(code: str, market, capital: float) -> dict:
    df = _safe_kline(market, code, "1d", 60)
    if df is None or len(df) < 22:
        return _hold("turtle_adx", "日 K 不足 22 根")
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)
    cur = float(close.iloc[-1])
    entry_high = float(high.iloc[-21:-1].max())
    exit_low = float(low.iloc[-11:-1].min())
    if cur < exit_low:
        return _signal("sell", "turtle_adx",
                       f"收盘 {cur:.2f} 跌破 10 日低 {exit_low:.2f}")
    if cur > entry_high:
        adx = _adx_last(high, low, close, 14)
        if adx is None or adx < 15:
            return _hold("turtle_adx",
                         f"突破 {entry_high:.2f} 但 ADX={adx} < 15")
        return _signal("buy", "turtle_adx",
                       f"突破 {entry_high:.2f} ADX={adx:.1f}")
    return _hold("turtle_adx", f"在 [{exit_low:.2f}, {entry_high:.2f}] 收盘 {cur:.2f}")


@register(
    name="momentum_roc",
    label="动量 ROC (20日·5%)",
    group="动量与背离",
    description="20 日涨幅超过 5% 买入，跌幅超过 5% 卖出。第 5 讲。",
    scenario="只看价格惯性、不管均线交叉时。",
    rules="20 日涨幅大于 5% 买入；跌幅大于 5% 卖出。",
    example="20 日涨了 7% 买入；随后 20 日回撤超过 5% 卖出。",
)
def strat_momentum_roc(code: str, market, capital: float) -> dict:
    df = _safe_kline(market, code, "1d", 40)
    if df is None or len(df) < 22:
        return _hold("momentum_roc", "日 K 不足 22 根")
    close = df["close"].astype(float)
    prev = float(close.iloc[-21])
    if prev <= 0:
        return _hold("momentum_roc", "基准价无效")
    roc = (float(close.iloc[-1]) / prev - 1.0) * 100.0
    if roc > 5.0:
        return _signal("buy", "momentum_roc", f"ROC20={roc:+.2f}%")
    if roc < -5.0:
        return _signal("sell", "momentum_roc", f"ROC20={roc:+.2f}%")
    return _hold("momentum_roc", f"ROC20={roc:+.2f}% 未过阈值")


@register(
    name="macd_divergence",
    label="MACD 底背离 (30日)",
    group="动量与背离",
    description="价近 30 日低但 DIF 未创新低且金叉则买，死叉卖。第 5 讲。",
    scenario="大跌后想找动能衰竭的反转点，而不是再等均线上穿时。",
    rules="低点在 30 日最低附近、DIF 高于 30 日最低的 80%、且金叉则买入；死叉卖出。",
    example="股价再探前低，DIF 明显高于前低且金叉则买入。",
)
def strat_macd_divergence(code: str, market, capital: float) -> dict:
    df = _safe_kline(market, code, "1d", 80)
    if df is None or len(df) < 40:
        return _hold("macd_divergence", "日 K 不足 40 根")
    close = df["close"].astype(float)
    low = df["low"].astype(float)
    dif, dea = _macd_lines(close)
    look = 30
    price_low = float(low.iloc[-look:].min())
    macd_low = float(dif.iloc[-look:].min())
    last_low = float(low.iloc[-1])
    last_dif = float(dif.iloc[-1])
    last_dea = float(dea.iloc[-1])
    prev_dif = float(dif.iloc[-2])
    prev_dea = float(dea.iloc[-2])
    dead = prev_dif >= prev_dea and last_dif < last_dea
    if dead:
        return _signal("sell", "macd_divergence", "MACD 死叉离场")
    at_low = last_low <= price_low * 1.01
    macd_higher = last_dif > macd_low * 0.8
    golden = last_dif > last_dea
    if at_low and macd_higher and golden:
        return _signal("buy", "macd_divergence",
                       f"底背离 低={last_low:.2f} DIF={last_dif:.4f}")
    return _hold("macd_divergence", "无底背离")


@register(
    name="chan_third_buy",
    label="缠论三买（第9讲）",
    group="结构与战法",
    description="日 K 缠论第三类买点买入，收盘跌破中枢上沿或出现三卖则卖。第 9 讲。和第 13 课的缠论三买是两套算法。",
    scenario="能看懂中枢结构，想做突破回踩不回中枢的那段时。",
    rules="结构信号为三买则买入；收盘跌破中枢上沿，或出现三卖，则卖出。",
    example="突破中枢后回踩不进中枢打出三买则买入；跌回中枢上沿则卖出。",
)
def strat_chan_third_buy(code: str, market, capital: float) -> dict:
    row = _chan_last_row(code, market)
    if row is None:
        return _hold("chan_third_buy", "缠论结构未就绪")
    sig = int(row.get("chan_signal") or 0)
    zg = row.get("chan_zg")
    close = _safe_kline(market, code, "1d", 3)
    cur = float(close["close"].iloc[-1]) if close is not None else None
    if sig == 3:
        return _signal("buy", "chan_third_buy", f"三买 ZG={zg}")
    if sig == -3:
        return _signal("sell", "chan_third_buy", "三卖离场")
    if cur is not None and zg is not None and not math.isnan(float(zg)) and cur < float(zg):
        return _signal("sell", "chan_third_buy", f"收盘 {cur:.2f} < ZG {float(zg):.2f}")
    return _hold("chan_third_buy", f"信号={sig} ZG={zg}")


@register(
    name="chan_grid",
    label="缠论中枢网格 (6 格)",
    group="震荡网格",
    description="用当前中枢上下沿切 6 格，底部买顶部卖，出中枢清仓。第 10 讲。",
    scenario="价格正在中枢里磨，不想用 60 日高低这种固定区间时。",
    rules="有中枢时下两格买、上两格卖；价格离开中枢则卖出。没有中枢就观望。",
    example="中枢 40 到 48，价在 41 靠近下沿则买；冲到 47 或跌破 40 则卖。",
)
def strat_chan_grid(code: str, market, capital: float) -> dict:
    row = _chan_last_row(code, market)
    if row is None:
        return _hold("chan_grid", "缠论结构未就绪")
    zg = row.get("chan_zg")
    zd = row.get("chan_zd")
    if zg is None or zd is None or math.isnan(float(zg)) or math.isnan(float(zd)):
        return _hold("chan_grid", "当前无中枢")
    zg = float(zg)
    zd = float(zd)
    if zg <= zd:
        return _hold("chan_grid", "中枢区间无效")
    close = _safe_kline(market, code, "1d", 3)
    if close is None:
        return _hold("chan_grid", "无收盘价")
    cur = float(close["close"].iloc[-1])
    if cur > zg or cur < zd:
        return _signal("sell", "chan_grid", f"出中枢 价={cur:.2f} [{zd:.2f},{zg:.2f}]")
    span = (zg - zd) / 6.0
    idx = int((cur - zd) / span) if span > 0 else 3
    idx = max(0, min(5, idx))
    reason = f"中枢[{zd:.2f},{zg:.2f}] 第 {idx + 1}/6 格 价={cur:.2f}"
    if idx <= 1:
        return _signal("buy", "chan_grid", "底部 " + reason)
    if idx >= 4:
        return _signal("sell", "chan_grid", "顶部 " + reason)
    return _hold("chan_grid", reason)


def _fund_rows(code: str, market):
    """A 股读 trade_stock_financial，美股读 trade_stock_financial_us。回测只看到当日及以前的报告期。"""
    from dragon_strategy.db_config import execute_query
    us = str(code).upper().endswith(".US")
    table = "trade_stock_financial_us" if us else "trade_stock_financial"
    sql = (
        "SELECT report_date, roe, gross_margin, debt_ratio, "
        "operating_cashflow, revenue, net_profit "
        f"FROM {table} WHERE stock_code=%s"
    )
    params: List[Any] = [code]
    asof = getattr(market, "_asof", None)
    if us and callable(asof):
        cutoff = str(asof())[:10]
        sql += " AND report_date<=%s"
        params.append(cutoff)
    sql += " ORDER BY report_date DESC LIMIT 8"
    return us, execute_query(sql, tuple(params))


def _net_profit_yoy(rows, us: bool) -> Optional[float]:
    """A 股沿用相邻报告期。美股是单季报，同比对齐约一年前的那一季。"""
    np0 = float(rows[0]["net_profit"] or 0)
    if not us:
        if len(rows) < 2:
            return None
        np1 = float(rows[1]["net_profit"] or 0)
        if np1 == 0:
            return None
        return (np0 - np1) / abs(np1) * 100.0
    current = rows[0]["report_date"]
    best = None
    best_gap = 10 ** 9
    for row in rows[1:]:
        gap = abs((row["report_date"] - current).days)
        if abs(gap - 365) < best_gap:
            best_gap = abs(gap - 365)
            best = row
    if best is None or best_gap > 45:
        return None
    np1 = float(best["net_profit"] or 0)
    if np1 == 0:
        return None
    return (np0 - np1) / abs(np1) * 100.0


@register(
    name="fund_quality",
    label="基本面质量漏斗",
    group="基本面与选股",
    description="最新财报过 5 层阈值才买，质量掉下去卖。第 4 讲。A 股读 trade_stock_financial，美股读 trade_stock_financial_us。",
    scenario="只想拿质量过关的票，技术面另说时。",
    rules="ROE 不低于 15%、净利同比不低于 10%、毛利率不低于 30%、负债率不高于 60%、经营现金流占营收不低于 10% 才买入；任一不达标则卖出。美股净利润同比用约一年前的单季报。",
    example="ROE 18%、同比 12%、毛利 35% 则买入；下期负债升到 70% 则卖出。",
)
def strat_fund_quality(code: str, market, capital: float) -> dict:
    us, rows = _fund_rows(code, market)
    if not rows:
        return _hold("fund_quality", "无财报")
    cur = rows[0]
    roe = float(cur["roe"] or 0)
    gm = float(cur["gross_margin"] or 0)
    debt = float(cur["debt_ratio"] or 0)
    ocf = float(cur["operating_cashflow"] or 0)
    rev = float(cur["revenue"] or 0)
    yoy = _net_profit_yoy(rows, us)
    ocf_rev = (ocf / rev * 100.0) if rev != 0 else None
    if yoy is None or ocf_rev is None:
        return _hold("fund_quality", "财报缺同比或经营现金流/营收, 无法过漏斗")
    ok = (
        roe >= 15
        and gm >= 30
        and 0 < debt <= 60
        and yoy >= 10
        and ocf_rev >= 10
    )
    reason = (
        f"ROE={roe:.1f} 毛利={gm:.1f} 负债={debt:.1f} "
        f"同比={yoy:.1f} 现金流/营收={ocf_rev:.1f}"
    )
    if ok:
        return _signal("buy", "fund_quality", reason)
    return _signal("sell", "fund_quality", "漏斗未过 " + reason)


def _ml_last_prob(code: str, market, model_type: str):
    """回测读已算好的序列; 模拟盘当场滚动训练, 按最后一根 K 缓存。"""
    df = _safe_kline(market, code, "1d", 400)
    if df is None or len(df) < 150:
        return None
    attached = getattr(market, "ml_prob", None)
    if attached is not None:
        ts = df.index[-1]
        if ts in attached.index:
            val = attached.loc[ts]
            if hasattr(val, "iloc"):
                val = val.iloc[-1]
            return float(val)
        return None
    from ml_strategy.ml_prob_runner import run_ml_prob
    out = run_ml_prob(df, model_type=model_type, code=code, verbose=False)
    return float(out["meta"]["last_prob"])


def _ml_signal(code: str, market, name: str, model_type: str) -> dict:
    prob = _ml_last_prob(code, market, model_type)
    if prob is None:
        return _hold(name, "概率未就绪 (日 K 需约 150 根)")
    if prob > 0.6:
        return _signal("buy", name, f"涨概率 {prob:.3f} > 0.6")
    if prob < 0.4:
        return _signal("sell", name, f"涨概率 {prob:.3f} < 0.4")
    return _hold(name, f"涨概率 {prob:.3f} 中性")


@register(
    name="ml_xgb",
    label="XGBoost 概率择时",
    group="机器学习",
    description="50 多个日 K 因子加 XGBoost 滚动训练, 概率高于 0.6 买、低于 0.4 卖。第 11 讲。",
    scenario="用课上的树模型做波段, 并和规则策略对照时。",
    rules="训练窗口 120 个交易日, 每 20 日重训。预测下一交易日涨跌。回测整段只训一轮; 模拟盘同一天命中缓存。",
    example="最新概率 0.72 买入; 落到 0.35 卖出。",
)
def strat_ml_xgb(code: str, market, capital: float) -> dict:
    return _ml_signal(code, market, "ml_xgb", "xgboost")


@register(
    name="ml_lgb",
    label="LightGBM 概率择时",
    group="机器学习",
    description="和 XGBoost 同一套因子与阈值, 模型换成 LightGBM。第 11 讲对照。",
    scenario="和 XGBoost 并排回测, 看两棵树模型谁更稳。",
    rules="概率高于 0.6 买、低于 0.4 卖。回测整段只训一轮。",
    example="LightGBM 概率 0.68 买入。",
)
def strat_ml_lgb(code: str, market, capital: float) -> dict:
    return _ml_signal(code, market, "ml_lgb", "lightgbm")


# ============================================================
# Router: 按 (per_stock_map, default) 路由表派发
# ============================================================

class StrategyRouter:
    """
    策略路由器 -- 给 LiveTradingLoop 用的 evaluator

    用法:
        router = StrategyRouter(
            per_stock={"600519.SH": "macd_1d", "510300.SH": "grid_classic"},
            default="macd_1d",
        )
        loop = LiveTradingLoop(..., signal_evaluator=router)
    """

    def __init__(self, per_stock: Dict[str, str], default: str = "macd_1d"):
        self.per_stock = dict(per_stock or {})
        self.default = default

    def update(self, per_stock: Optional[Dict[str, str]] = None,
               default: Optional[str] = None):
        """热更新路由表 (前端保存配置后调)"""
        if per_stock is not None:
            self.per_stock = dict(per_stock)
        if default is not None:
            self.default = default

    def __call__(self, code: str, market, capital: float) -> dict:
        name = self.per_stock.get(code, self.default)
        meta = get_strategy(name)
        if meta is None:
            return _hold("unknown", f"策略 {name} 未注册")
        try:
            return meta.evaluator(code, market, capital)
        except Exception as e:
            return _hold(name, f"策略异常: {type(e).__name__}: {e}")
