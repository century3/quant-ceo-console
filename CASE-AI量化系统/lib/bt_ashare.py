# -*- coding: utf-8 -*-
# Author：@跟陈博士学AI
"""A 股摩擦成本 + 次日开盘撮合 -- 从第 5 讲 1b-摩擦成本与成交约束.py copy 后裁剪

保留:
    - AShareCommission (佣金 / 印花税 / 过户费)
    - apply_friction (滑点 + 部分成交 + 当根开盘成交)
    - AShareFillMixin (涨跌停 / 一字板 / T+1)

删掉:
    - DoubleMAWithFill 演示策略与 __main__
    - 改成给 RegistryBridge 当 mixin, 策略仍走 strategy_registry
"""
import backtrader as bt


class AShareCommission(bt.CommInfoBase):
    """A 股成本: 佣金双边、印花税只收卖、过户费双边"""
    params = (
        ('commission', 0.00015),   # 佣金万 1.5
        ('stamp_duty', 0.0005),    # 印花税千 0.5, 仅卖出
        ('transfer_fee', 0.00001), # 过户费万 0.1
        ('min_comm', 5.0),         # 佣金最低 5 元
        ('stocklike', True),
        ('commtype', bt.CommInfoBase.COMM_PERC),
    )

    def _getcommission(self, size, price, pseudoexec):
        turnover = abs(size) * price
        comm = max(turnover * self.p.commission, self.p.min_comm)
        transfer = turnover * self.p.transfer_fee
        stamp = turnover * self.p.stamp_duty if size < 0 else 0.0
        return comm + transfer + stamp


def apply_friction(cerebro, slip_perc=0.001, commission=0.00015, us: bool = False):
    """挂到现有 cerebro 上: 成本拆开 + 滑点 + 部分成交

    Cerebro 需 cheat_on_open=True, 在 next_open 里买/平, 按当根开盘成交。
    美股只按成交额收佣金，不收 A 股印花税，也没有最低 5 元。
    """
    if us:
        cerebro.broker.setcommission(commission=commission)
    else:
        cerebro.broker.addcommissioninfo(AShareCommission(commission=commission))
    cerebro.broker.set_slippage_perc(perc=slip_perc, slip_open=True, slip_match=True)
    cerebro.broker.set_filler(bt.broker.fillers.FixedBarPerc(perc=10.0))


class AShareFillMixin:
    """从 1b DoubleMAWithFill 抽出的涨跌停 / 一字板 / T+1"""

    def _limit_prices(self):
        prev = float(self.data.close[-1])
        return prev * (1 + self.p.limit_pct), prev * (1 - self.p.limit_pct)

    def _is_one_word(self):
        """一字板: 开=高=低=收, 当天没有对手盘空间"""
        o, h, l, c = (float(self.data.open[0]), float(self.data.high[0]),
                      float(self.data.low[0]), float(self.data.close[0]))
        return abs(o - h) < 1e-8 and abs(h - l) < 1e-8 and abs(l - c) < 1e-8

    def _can_buy(self):
        if float(self.p.limit_pct) <= 0:
            return float(self.data.open[0]) > 0
        limit_up, _ = self._limit_prices()
        return float(self.data.open[0]) < limit_up * 0.999 and not (
            self._is_one_word() and float(self.data.close[0]) >= limit_up * 0.999
        )

    def _can_sell(self):
        if float(self.p.limit_pct) <= 0:
            return float(self.data.open[0]) > 0
        _, limit_down = self._limit_prices()
        return float(self.data.open[0]) > limit_down * 1.001 and not (
            self._is_one_word() and float(self.data.close[0]) <= limit_down * 1.001
        )
