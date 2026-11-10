# -*- coding: utf-8 -*-
# Author：@跟陈博士学AI
# 24-CASE-C: 策略注册中心 + 生命周期管理
"""
StrategyRegistry -- 策略生命周期管理

学员痛点: "我有 5 个策略, 不知道哪个该上、哪个该下、哪个该加资金"
答: 用策略生命周期 + KPI 驱动的自动迁移

策略 5 阶段生命周期 (类似产品 / 投资组合):

    1. INCUBATING (孵化)    -- 刚开发, 用纸面回测验证
                                 KPI: walk-forward IS/OOS 比例 > 0.7
                                 资金: 0
    
    2. PAPER (纸交易)        -- 通过验证, 进入 dry-run 跑 1 个月
                                 KPI: 累计收益 > 0 + 最大回撤 < 5%
                                 资金: 0 (虚拟)
    
    3. PROBATION (试用)     -- 真实小资金跑 1 个月
                                 KPI: 实盘 Sharpe > 0.5
                                 资金: 总资金的 5-10%
    
    4. PRODUCTION (主力)    -- 长期运行的稳定策略
                                 KPI: 滚动 30 日 Sharpe > 1.0 + 最大回撤 < 15%
                                 资金: 总资金的 30-50%
    
    5. RETIRED (退役)       -- KPI 持续低于阈值, 自动下架
                                 KPI: 滚动 30 日 Sharpe < 0.3 持续 2 周
                                 资金: 0 (清仓)

自动迁移规则 (按 KPI 评分):
    INCUBATING -> PAPER     -- IS/OOS > 0.7
    PAPER -> PROBATION      -- 1 个月累计收益 > 0
    PROBATION -> PRODUCTION -- 1 个月 Sharpe > 0.5
    PRODUCTION -> RETIRED   -- 30 日 Sharpe < 0.3 持续 2 周
    任何阶段 -> RETIRED      -- 单日亏损 > 3%
"""

from __future__ import annotations
import json
import math
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional


class LifecycleStage(str, Enum):
    INCUBATING  = "incubating"   # 孵化: 纸面回测
    PAPER       = "paper"        # 纸交易
    PROBATION   = "probation"    # 真实小资金试用
    PRODUCTION  = "production"   # 主力策略
    RETIRED     = "retired"      # 已下架


@dataclass
class StrategyKPI:
    """策略 KPI 数据"""
    rolling_30d_sharpe: float = 0.0
    rolling_30d_return: float = 0.0
    rolling_30d_maxdd: float = 0.0
    days_since_promotion: int = 0
    consecutive_low_sharpe_days: int = 0   # 连续低 Sharpe 天数 (用于退役)
    total_trades: int = 0
    win_rate: float = 0.0
    walk_forward_is_oos_ratio: float = 0.0


@dataclass
class StrategyRecord:
    name: str
    description: str
    stage: LifecycleStage
    capital_allocated: float
    capital_pct: float
    kpi: StrategyKPI = field(default_factory=StrategyKPI)
    created_at: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d"))
    last_updated: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d"))
    history: list = field(default_factory=list)   # 阶段迁移历史


# ============================================================
# 注册中心
# ============================================================

class StrategyRegistry:
    """策略注册中心 -- JSON 文件存储"""

    def __init__(self, registry_file: str = "outputs/strategy_registry.json",
                 total_capital: float = 1_000_000):
        self.registry_file = Path(registry_file)
        self.registry_file.parent.mkdir(parents=True, exist_ok=True)
        self.total_capital = total_capital
        self.strategies: Dict[str, StrategyRecord] = {}
        self._load()

    def _load(self):
        if not self.registry_file.exists():
            return
        try:
            data = json.loads(self.registry_file.read_text(encoding="utf-8"))
            for name, raw in data.get("strategies", {}).items():
                kpi_raw = raw.pop("kpi", {})
                kpi = StrategyKPI(**kpi_raw)
                rec = StrategyRecord(
                    name=raw["name"], description=raw.get("description", ""),
                    stage=LifecycleStage(raw["stage"]),
                    capital_allocated=raw.get("capital_allocated", 0),
                    capital_pct=raw.get("capital_pct", 0),
                    kpi=kpi,
                    created_at=raw.get("created_at", ""),
                    last_updated=raw.get("last_updated", ""),
                    history=raw.get("history", []),
                )
                self.strategies[name] = rec
        except Exception as e:
            print(f"[WARN] 加载注册中心失败: {e}")

    def save(self):
        data = {
            "total_capital": self.total_capital,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "strategies": {
                name: {**asdict(rec), "stage": rec.stage.value, "kpi": asdict(rec.kpi)}
                for name, rec in self.strategies.items()
            },
        }
        self.registry_file.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                                       encoding="utf-8")

    def register(self, name: str, description: str = "",
                 initial_stage: LifecycleStage = LifecycleStage.INCUBATING) -> StrategyRecord:
        """注册一个新策略"""
        if name in self.strategies:
            return self.strategies[name]
        rec = StrategyRecord(
            name=name, description=description, stage=initial_stage,
            capital_allocated=0, capital_pct=0,
            history=[{"ts": datetime.now().isoformat(timespec="seconds"),
                      "from": None, "to": initial_stage.value, "reason": "新建"}],
        )
        self.strategies[name] = rec
        self.save()
        return rec

    def update_kpi(self, name: str, **kpi_kwargs):
        """更新某个策略的 KPI 数据"""
        if name not in self.strategies:
            raise KeyError(f"策略 {name} 未注册")
        rec = self.strategies[name]
        for k, v in kpi_kwargs.items():
            if hasattr(rec.kpi, k):
                setattr(rec.kpi, k, v)
        rec.last_updated = datetime.now().strftime("%Y-%m-%d")
        self.save()

    # ------------------------------------------------------------------
    # 自动生命周期迁移
    # ------------------------------------------------------------------
    def evaluate_and_migrate(self) -> List[dict]:
        """
        遍历所有策略, 根据 KPI 自动迁移阶段
        
        返回: 迁移记录列表
        """
        migrations = []
        for name, rec in self.strategies.items():
            new_stage = self._decide_next_stage(rec)
            if new_stage and new_stage != rec.stage:
                old_stage = rec.stage
                reason = self._explain_migration(rec, old_stage, new_stage)
                rec.stage = new_stage
                rec.kpi.days_since_promotion = 0
                # 自动调整资金
                rec.capital_pct = self._suggest_capital_pct(new_stage)
                rec.capital_allocated = self.total_capital * rec.capital_pct
                rec.history.append({
                    "ts":     datetime.now().isoformat(timespec="seconds"),
                    "from":   old_stage.value,
                    "to":     new_stage.value,
                    "reason": reason,
                })
                migrations.append({
                    "name":   name,
                    "from":   old_stage.value,
                    "to":     new_stage.value,
                    "reason": reason,
                })
        if migrations:
            self.save()
        return migrations

    def _decide_next_stage(self, rec: StrategyRecord) -> Optional[LifecycleStage]:
        """根据 KPI 决定下一阶段"""
        kpi = rec.kpi
        st = rec.stage

        # 强退役: 任何阶段单日亏损 > 3% (这里用 30 日回撤代理)
        if kpi.rolling_30d_maxdd > 0.20:
            return LifecycleStage.RETIRED

        # 升级路径
        if st == LifecycleStage.INCUBATING:
            if kpi.walk_forward_is_oos_ratio >= 0.7:
                return LifecycleStage.PAPER

        elif st == LifecycleStage.PAPER:
            if (kpi.days_since_promotion >= 20
                    and kpi.rolling_30d_return > 0
                    and kpi.rolling_30d_maxdd < 0.05):
                return LifecycleStage.PROBATION

        elif st == LifecycleStage.PROBATION:
            if (kpi.days_since_promotion >= 20
                    and kpi.rolling_30d_sharpe > 0.5):
                return LifecycleStage.PRODUCTION

        elif st == LifecycleStage.PRODUCTION:
            # 退役: 连续低 Sharpe 14 天
            if kpi.consecutive_low_sharpe_days >= 14:
                return LifecycleStage.RETIRED

        return None   # 不变

    def _explain_migration(self, rec: StrategyRecord,
                            old: LifecycleStage, new: LifecycleStage) -> str:
        kpi = rec.kpi
        if new == LifecycleStage.RETIRED:
            if kpi.rolling_30d_maxdd > 0.20:
                return f"30日最大回撤 {kpi.rolling_30d_maxdd:.1%} 超过 20%, 强制退役"
            return f"连续 {kpi.consecutive_low_sharpe_days} 天 Sharpe 低于阈值"
        if new == LifecycleStage.PAPER:
            return f"Walk-Forward IS/OOS 比例 {kpi.walk_forward_is_oos_ratio:.2f} >= 0.70, 通过孵化"
        if new == LifecycleStage.PROBATION:
            return (f"纸交易 {kpi.days_since_promotion} 天, "
                    f"收益 {kpi.rolling_30d_return:+.2%}, 回撤 {kpi.rolling_30d_maxdd:.2%}, 通过试用门槛")
        if new == LifecycleStage.PRODUCTION:
            return f"试用期 Sharpe {kpi.rolling_30d_sharpe:.2f} > 0.5, 升为主力"
        return f"{old.value} -> {new.value}"

    @staticmethod
    def _suggest_capital_pct(stage: LifecycleStage) -> float:
        return {
            LifecycleStage.INCUBATING:  0.0,
            LifecycleStage.PAPER:       0.0,
            LifecycleStage.PROBATION:   0.05,    # 5% 资金
            LifecycleStage.PRODUCTION:  0.30,    # 30% 资金 (主力)
            LifecycleStage.RETIRED:     0.0,
        }[stage]

    # ------------------------------------------------------------------
    # 报表
    # ------------------------------------------------------------------
    def summary(self) -> str:
        lines = [
            f"\n{'='*78}",
            f"  策略注册中心 -- 总资金 {self.total_capital:,.0f} 元",
            f"{'='*78}",
        ]
        # 按 stage 分组
        by_stage = {}
        for name, rec in self.strategies.items():
            by_stage.setdefault(rec.stage.value, []).append(rec)

        for stage_value in ["production", "probation", "paper", "incubating", "retired"]:
            recs = by_stage.get(stage_value, [])
            if not recs:
                continue
            lines.append(f"\n[{stage_value.upper()}]  {len(recs)} 个策略")
            for rec in recs:
                lines.append(
                    f"  {rec.name:<20s}  资金 {rec.capital_allocated:>10,.0f} ({rec.capital_pct:>5.1%})  "
                    f"Sharpe {rec.kpi.rolling_30d_sharpe:>+5.2f}  "
                    f"30d收益 {rec.kpi.rolling_30d_return:>+6.2%}  "
                    f"最大回撤 {rec.kpi.rolling_30d_maxdd:>5.2%}"
                )
                lines.append(f"    描述: {rec.description}")

        # 资金占用统计
        total_pct = sum(r.capital_pct for r in self.strategies.values())
        lines.append(f"\n[资金占用] 已分配 {total_pct:.1%}, 闲置 {1 - total_pct:.1%}")
        return "\n".join(lines)


# ============================================================
# A/B 测试 (升级版): 给两个策略分相同资金, 30 天比 Sharpe
# ============================================================

class ABTest:
    """
    简单 A/B 测试:
        - 两个策略 A / B 共享同一份候选股池
        - 各分配 5% 资金, 跑 30 天
        - 比较哪个 Sharpe 更高 -> 胜者升 PRODUCTION, 负者回 PAPER
    """

    def __init__(self, registry: StrategyRegistry):
        self.registry = registry

    def run_evaluation(self, strategy_a: str, strategy_b: str,
                       days_so_far: int = 30) -> dict:
        """评估 A/B 测试结果"""
        ra = self.registry.strategies[strategy_a]
        rb = self.registry.strategies[strategy_b]

        sa = ra.kpi.rolling_30d_sharpe
        sb = rb.kpi.rolling_30d_sharpe

        if days_so_far < 20:
            return {"verdict": "not_enough_data", "winner": None}

        if abs(sa - sb) < 0.2:
            return {"verdict": "tie", "winner": None,
                    "msg": f"Sharpe 差距 {abs(sa - sb):.2f} 小于 0.2, 不显著"}

        winner = strategy_a if sa > sb else strategy_b
        loser = strategy_b if winner == strategy_a else strategy_a

        return {
            "verdict": "decisive",
            "winner": winner,
            "loser": loser,
            "winner_sharpe": max(sa, sb),
            "loser_sharpe": min(sa, sb),
            "msg": f"{winner} (Sharpe {max(sa,sb):.2f}) 胜出 vs {loser} (Sharpe {min(sa,sb):.2f})",
        }


# ============================================================
# Demo
# ============================================================

def demo():
    print(f"\n{'='*78}")
    print(f"  CASE-24C 策略生命周期 + A/B 测试 demo")
    print(f"{'='*78}")

    # 初始化 5 个不同阶段的策略
    reg = StrategyRegistry("outputs/strategy_registry.json", total_capital=1_000_000)

    test_strategies = [
        ("dragon_first_board", "23-CASE-C 龙头首板战法",
         LifecycleStage.PRODUCTION,
         dict(rolling_30d_sharpe=1.2, rolling_30d_return=0.045,
              rolling_30d_maxdd=0.018, days_since_promotion=90,
              walk_forward_is_oos_ratio=0.85, total_trades=68)),

        ("multi_factor_csi300", "22-CASE-B 多因子选股 (沪深300)",
         LifecycleStage.PRODUCTION,
         dict(rolling_30d_sharpe=0.8, rolling_30d_return=0.022,
              rolling_30d_maxdd=0.035, days_since_promotion=60,
              walk_forward_is_oos_ratio=0.72, total_trades=45)),

        ("sector_rotation", "22-CASE-C 板块轮动 ETF",
         LifecycleStage.PROBATION,
         dict(rolling_30d_sharpe=0.65, rolling_30d_return=0.018,
              rolling_30d_maxdd=0.025, days_since_promotion=25,
              walk_forward_is_oos_ratio=0.78, total_trades=12)),

        ("dqn_ethan", "18 章 DQN 强化学习择时",
         LifecycleStage.PAPER,
         dict(rolling_30d_sharpe=0.4, rolling_30d_return=0.012,
              rolling_30d_maxdd=0.040, days_since_promotion=22,
              walk_forward_is_oos_ratio=0.55, total_trades=18)),

        ("breakout_naive", "新写的突破策略 (待孵化)",
         LifecycleStage.INCUBATING,
         dict(rolling_30d_sharpe=0.0, rolling_30d_return=0.0,
              rolling_30d_maxdd=0.0, walk_forward_is_oos_ratio=0.78,
              total_trades=0)),

        ("old_macd_naive", "老版 MACD 双均线 (即将退役)",
         LifecycleStage.PRODUCTION,
         dict(rolling_30d_sharpe=0.15, rolling_30d_return=-0.008,
              rolling_30d_maxdd=0.062, days_since_promotion=180,
              consecutive_low_sharpe_days=18, total_trades=22)),
    ]

    for name, desc, stage, kpi_kwargs in test_strategies:
        rec = reg.register(name, desc, initial_stage=stage)
        rec.stage = stage
        rec.capital_pct = StrategyRegistry._suggest_capital_pct(stage)
        rec.capital_allocated = reg.total_capital * rec.capital_pct
        for k, v in kpi_kwargs.items():
            setattr(rec.kpi, k, v)

    reg.save()

    # 1) 当前快照
    print(reg.summary())

    # 2) 跑自动迁移
    print(f"\n\n{'='*78}")
    print(f"  自动生命周期评估 + 迁移")
    print(f"{'='*78}")

    migrations = reg.evaluate_and_migrate()

    if not migrations:
        print("\n  无迁移 (所有策略 KPI 未触发阶段变更)")
    else:
        print(f"\n  本轮触发 {len(migrations)} 个迁移:")
        for m in migrations:
            print(f"    [{m['name']}]  {m['from']} -> {m['to']}")
            print(f"        理由: {m['reason']}")

    # 3) 迁移后的快照
    print(f"\n\n{'='*78}")
    print(f"  迁移后状态")
    print(f"{'='*78}")
    print(reg.summary())

    # 4) A/B 测试演示
    print(f"\n\n{'='*78}")
    print(f"  A/B 测试: dragon_first_board vs multi_factor_csi300")
    print(f"{'='*78}")
    ab = ABTest(reg)
    result = ab.run_evaluation("dragon_first_board", "multi_factor_csi300", days_so_far=30)
    print(f"  结果: {result}")


if __name__ == "__main__":
    demo()
