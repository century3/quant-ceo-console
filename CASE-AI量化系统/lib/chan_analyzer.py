# -*- coding: utf-8 -*-
# Author：@跟陈博士学AI
"""
缠论分析引擎

核心算法:
  1. K线包含关系处理（合并）
  2. 顶底分型识别
  3. 笔的自动化识别
  4. 中枢识别
  5. 三类买卖点信号检测
  6. 可视化

使用:
    from chan_analyzer import ChanAnalyzer

    analyzer = ChanAnalyzer(df)
    analyzer.analyze()
    analyzer.summary()
    analyzer.plot(title='缠论分析')
"""

import pandas as pd
import numpy as np
import talib
import os


class ChanAnalyzer:
    """
    缠论分析器

    将原始K线数据经过 合并 -> 分型 -> 笔 -> 中枢 -> 信号 的完整流程，
    输出可用于策略回测的买卖信号。
    """

    def __init__(self, df):
        """
        参数:
            df: DataFrame, 索引为日期(DatetimeIndex), 列需包含 open/high/low/close/volume
        """
        self.raw_df = df.copy()
        self.merged_df = None
        self.fractals = []
        self.confirmed_fractals = []
        self.bi_list = []
        self.zhongshu_list = []
        self.signals = []
        self._macd_hist = None

    def analyze(self):
        """执行完整缠论分析流程: 合并K线 -> 分型 -> 笔 -> 中枢 -> 信号"""
        self._prepare_macd()
        self.merged_df = self._merge_klines()
        self.fractals = self._identify_fractals()
        self.bi_list = self._identify_bi()
        self.zhongshu_list = self._identify_zhongshu()
        self.signals = self._detect_signals()
        return self

    # ============================================================
    # Step 1: K线包含关系处理
    # ============================================================

    def _merge_klines(self):
        """
        处理K线包含关系

        包含关系: 两根K线的高低点存在完全包含（一根K线的高低点范围完全覆盖另一根）
        合并规则:
          - 上升趋势（前K线高点上升）: 取更高的高点、更高的低点
          - 下降趋势（前K线高点下降）: 取更低的高点、更低的低点

        额外记录 high_date / low_date: 该合并K线的最高点/最低点
        实际来源于哪一根原始K线的日期，用于笔端点的精确定位
        """
        df = self.raw_df
        if len(df) < 3:
            result = df.copy()
            result['high_date'] = result.index
            result['low_date'] = result.index
            return result

        merged = []
        for i in range(len(df)):
            row = {
                'date': df.index[i],
                'open': float(df['open'].iloc[i]),
                'high': float(df['high'].iloc[i]),
                'low': float(df['low'].iloc[i]),
                'close': float(df['close'].iloc[i]),
                'volume': float(df['volume'].iloc[i]),
                'high_date': df.index[i],
                'low_date': df.index[i],
            }

            if len(merged) < 2:
                merged.append(row)
                continue

            prev = merged[-1]

            inclusion = (
                (row['high'] >= prev['high'] and row['low'] <= prev['low']) or
                (prev['high'] >= row['high'] and prev['low'] <= row['low'])
            )

            if inclusion:
                prev_prev = merged[-2]
                is_up = prev['high'] >= prev_prev['high']

                if is_up:
                    new_high = max(prev['high'], row['high'])
                    new_low = max(prev['low'], row['low'])
                    h_date = row['high_date'] if row['high'] >= prev['high'] else prev['high_date']
                    l_date = row['low_date'] if row['low'] >= prev['low'] else prev['low_date']
                    merged[-1] = {
                        'date': prev['date'],
                        'open': prev['open'],
                        'high': new_high,
                        'low': new_low,
                        'close': row['close'],
                        'volume': prev['volume'] + row['volume'],
                        'high_date': h_date,
                        'low_date': l_date,
                    }
                else:
                    new_high = min(prev['high'], row['high'])
                    new_low = min(prev['low'], row['low'])
                    h_date = row['high_date'] if row['high'] <= prev['high'] else prev['high_date']
                    l_date = row['low_date'] if row['low'] <= prev['low'] else prev['low_date']
                    merged[-1] = {
                        'date': prev['date'],
                        'open': prev['open'],
                        'high': new_high,
                        'low': new_low,
                        'close': row['close'],
                        'volume': prev['volume'] + row['volume'],
                        'high_date': h_date,
                        'low_date': l_date,
                    }
            else:
                merged.append(row)

        result = pd.DataFrame(merged)
        if not result.empty:
            result['date'] = pd.to_datetime(result['date'])
            result['high_date'] = pd.to_datetime(result['high_date'])
            result['low_date'] = pd.to_datetime(result['low_date'])
            result.set_index('date', inplace=True)
        return result

    # ============================================================
    # Step 2: 分型识别
    # ============================================================

    def _identify_fractals(self):
        """
        在合并后的K线上识别顶底分型

        顶分型: 中间K线的高点 > 两侧高点，且中间K线的低点 > 两侧低点
        底分型: 中间K线的低点 < 两侧低点，且中间K线的高点 < 两侧高点

        返回: list[dict], 包含 index/date/type/price/raw_date
          - date: 合并K线的日期（用于算法内部的 index 计算）
          - raw_date: 极值实际出现的原始K线日期（用于精确绑定到蜡烛图）
        """
        df = self.merged_df
        if df is None or len(df) < 3:
            return []

        has_raw_dates = 'high_date' in df.columns and 'low_date' in df.columns

        fractals = []
        for i in range(1, len(df) - 1):
            h_prev = df['high'].iloc[i - 1]
            h_curr = df['high'].iloc[i]
            h_next = df['high'].iloc[i + 1]
            l_prev = df['low'].iloc[i - 1]
            l_curr = df['low'].iloc[i]
            l_next = df['low'].iloc[i + 1]

            if (h_curr > h_prev and h_curr > h_next and
                    l_curr > l_prev and l_curr > l_next):
                raw_date = df['high_date'].iloc[i] if has_raw_dates else df.index[i]
                fractals.append({
                    'index': i,
                    'date': df.index[i],
                    'raw_date': raw_date,
                    'type': 'top',
                    'price': float(h_curr),
                })
            elif (l_curr < l_prev and l_curr < l_next and
                  h_curr < h_prev and h_curr < h_next):
                raw_date = df['low_date'].iloc[i] if has_raw_dates else df.index[i]
                fractals.append({
                    'index': i,
                    'date': df.index[i],
                    'raw_date': raw_date,
                    'type': 'bottom',
                    'price': float(l_curr),
                })

        return fractals

    # ============================================================
    # Step 3: 笔识别
    # ============================================================

    def _identify_bi(self, min_gap=4):
        """
        基于分型生成笔

        严格遵循缠论定义:
          1. 顶底分型必须交替出现
          2. 相同类型分型只保留极值（更高的顶、更低的底）
          3. 两个相邻分型之间至少 min_gap 根合并K线（对应"至少5根K线含端点"）

        算法: 贪心前向扫描
          - 遇到同类型: 保留更极端的（替换）
          - 遇到异类型且 gap 足够: 确认成笔端点
          - 遇到异类型但 gap 不够: 跳过（不破坏已确认的结构）
        """
        if len(self.fractals) < 2:
            return []

        confirmed = [self.fractals[0]]

        for f in self.fractals[1:]:
            last = confirmed[-1]

            if f['type'] == last['type']:
                if ((f['type'] == 'top' and f['price'] > last['price']) or
                        (f['type'] == 'bottom' and f['price'] < last['price'])):
                    confirmed[-1] = f
            else:
                gap = f['index'] - last['index']
                if gap >= min_gap:
                    confirmed.append(f)

        self.confirmed_fractals = list(confirmed)

        # 生成笔
        bi_list = []
        for i in range(1, len(confirmed)):
            prev_f = confirmed[i - 1]
            curr_f = confirmed[i]

            if prev_f['type'] == curr_f['type']:
                continue

            direction = 'up' if prev_f['type'] == 'bottom' else 'down'

            start_raw = prev_f.get('raw_date', prev_f['date'])
            end_raw = curr_f.get('raw_date', curr_f['date'])

            bi_list.append({
                'start_index': prev_f['index'],
                'end_index': curr_f['index'],
                'start_date': prev_f['date'],
                'end_date': curr_f['date'],
                'start_raw_date': start_raw,
                'end_raw_date': end_raw,
                'start_price': prev_f['price'],
                'end_price': curr_f['price'],
                'direction': direction,
            })

        return bi_list

    # ============================================================
    # Step 4: 中枢识别
    # ============================================================

    def _identify_zhongshu(self, min_bi=3, max_extend=4):
        """
        识别中枢

        中枢: 至少 min_bi 笔的价格区间有重叠
        ZG = min(各笔的高点) = 重叠区间的上沿
        ZD = max(各笔的低点) = 重叠区间的下沿
        有效条件: ZG > ZD（确实存在重叠区间）

        max_extend: 中枢在初始 min_bi 基础上最多再扩展的笔数，
                    防止长期震荡区间吞噬所有笔导致无法产生信号
        """
        if len(self.bi_list) < min_bi:
            return []

        zhongshu_list = []
        i = 0

        while i <= len(self.bi_list) - min_bi:
            group = self.bi_list[i:i + min_bi]
            highs = [max(b['start_price'], b['end_price']) for b in group]
            lows = [min(b['start_price'], b['end_price']) for b in group]

            zg = min(highs)
            zd = max(lows)

            if zg > zd:
                end = i + min_bi
                extend_count = 0
                while end < len(self.bi_list) and extend_count < max_extend:
                    nb = self.bi_list[end]
                    nh = max(nb['start_price'], nb['end_price'])
                    nl = min(nb['start_price'], nb['end_price'])
                    if nh > zd and nl < zg:
                        end += 1
                        extend_count += 1
                    else:
                        break

                zhongshu_list.append({
                    'ZG': zg,
                    'ZD': zd,
                    'center': (zg + zd) / 2,
                    'start_index': group[0]['start_index'],
                    'end_index': self.bi_list[end - 1]['end_index'],
                    'start_date': group[0]['start_date'],
                    'end_date': self.bi_list[end - 1]['end_date'],
                    'bi_count': end - i,
                })
                i = end
            else:
                i += 1

        return zhongshu_list

    # ============================================================
    # Step 5: 信号检测
    # ============================================================

    def _prepare_macd(self):
        """预计算MACD柱状图，用于背驰判断"""
        close = self.raw_df['close'].values.astype(float)
        if len(close) >= 35:
            _, _, self._macd_hist = talib.MACD(close, fastperiod=12, slowperiod=26, signalperiod=9)
        else:
            self._macd_hist = np.zeros(len(close))

    def _calc_macd_area(self, start_date, end_date):
        """计算指定区间内MACD柱状图的绝对面积（衡量走势力度）"""
        mask = (self.raw_df.index >= start_date) & (self.raw_df.index <= end_date)
        indices = np.where(mask)[0]
        if len(indices) == 0:
            return 0.0
        segment = self._macd_hist[indices]
        return float(np.nansum(np.abs(segment)))

    def _detect_signals(self):
        """检测所有类型的买卖点信号"""
        signals = []
        signals.extend(self._detect_third_buy())
        first_buys = self._detect_first_buy()
        signals.extend(first_buys)
        signals.extend(self._detect_second_buy(first_buys))
        signals.extend(self._detect_third_sell())
        signals.sort(key=lambda s: s['date'])
        return signals

    def _detect_third_buy(self):
        """
        第三类买点: 突破中枢后回踩不进入中枢

        流程:
          1. 中枢完成后，寻找向上突破ZG的笔
          2. 突破后出现向下回踩笔
          3. 回踩笔的最低价 > ZG（不进入中枢区间）
          4. 确认三买信号
        """
        signals = []
        used_dates = set()

        for zs in self.zhongshu_list:
            zg = zs['ZG']
            zd = zs['ZD']

            post_bis = [b for b in self.bi_list if b['start_index'] >= zs['end_index']]
            state = 'WAIT_BREAKOUT'

            for bi in post_bis:
                if state == 'WAIT_BREAKOUT':
                    if bi['direction'] == 'up' and bi['end_price'] > zg:
                        state = 'WAIT_PULLBACK'
                elif state == 'WAIT_PULLBACK':
                    if bi['direction'] == 'down':
                        pullback_low = bi['end_price']
                        sig_date = bi.get('end_raw_date', bi['end_date'])
                        if pullback_low > zg and sig_date not in used_dates:
                            signals.append({
                                'date': sig_date,
                                'type': 'third_buy',
                                'price': pullback_low,
                                'zhongshu_zg': zg,
                                'zhongshu_zd': zd,
                            })
                            used_dates.add(sig_date)
                            break
                        else:
                            break
                    elif bi['direction'] == 'up':
                        pass

        return signals

    def _detect_first_buy(self):
        """
        第一类买点: 下跌趋势中的背驰（MACD面积法）

        条件:
          1. 至少2个中枢依次下移（下跌趋势）
          2. b段（前中枢→后中枢）的MACD面积 > c段（后中枢之后）的MACD面积
          3. c段出现底分型确认
        """
        signals = []

        if len(self.zhongshu_list) < 2:
            return signals

        for j in range(1, len(self.zhongshu_list)):
            prev_zs = self.zhongshu_list[j - 1]
            curr_zs = self.zhongshu_list[j]

            if not (curr_zs['ZD'] < prev_zs['ZD'] and curr_zs['ZG'] < prev_zs['ZG']):
                continue

            b_area = self._calc_macd_area(prev_zs['end_date'], curr_zs['start_date'])

            post_down = [b for b in self.bi_list
                         if b['start_index'] >= curr_zs['end_index'] and b['direction'] == 'down']
            if not post_down:
                continue

            c_bi = post_down[0]
            c_area = self._calc_macd_area(curr_zs['end_date'], c_bi['end_date'])

            if b_area > 0 and c_area < b_area * 0.8:
                sig_date = c_bi.get('end_raw_date', c_bi['end_date'])
                signals.append({
                    'date': sig_date,
                    'type': 'first_buy',
                    'price': c_bi['end_price'],
                    'zhongshu_zg': curr_zs['ZG'],
                    'zhongshu_zd': curr_zs['ZD'],
                    'divergence_ratio': round(c_area / max(b_area, 0.001), 2),
                })

        return signals

    def _detect_second_buy(self, first_buys):
        """
        第二类买点: 一买后首次回调不破一买低点

        条件:
          1. 一买信号已出现
          2. 价格反弹后再次回调
          3. 回调低点 > 一买低点
        """
        signals = []
        for fb in first_buys:
            post_bis = [b for b in self.bi_list if b['start_date'] > fb['date']]
            saw_up = False
            for bi in post_bis:
                if bi['direction'] == 'up':
                    saw_up = True
                elif bi['direction'] == 'down' and saw_up:
                    if bi['end_price'] > fb['price']:
                        sig_date = bi.get('end_raw_date', bi['end_date'])
                        signals.append({
                            'date': sig_date,
                            'type': 'second_buy',
                            'price': bi['end_price'],
                            'first_buy_price': fb['price'],
                        })
                    break

        return signals

    def _detect_third_sell(self):
        """
        第三类卖点: 跌破中枢后反弹不进入中枢（三买的镜像）

        条件:
          1. 中枢完成后，出现向下跌破ZD的笔
          2. 跌破后出现向上反弹笔
          3. 反弹笔的最高价 < ZD
        """
        signals = []
        used_dates = set()

        for zs in self.zhongshu_list:
            zg = zs['ZG']
            zd = zs['ZD']

            post_bis = [b for b in self.bi_list if b['start_index'] >= zs['end_index']]
            state = 'WAIT_BREAKDOWN'

            for bi in post_bis:
                if state == 'WAIT_BREAKDOWN':
                    if bi['direction'] == 'down' and bi['end_price'] < zd:
                        state = 'WAIT_BOUNCE'
                elif state == 'WAIT_BOUNCE':
                    if bi['direction'] == 'up':
                        bounce_high = bi['end_price']
                        sig_date = bi.get('end_raw_date', bi['end_date'])
                        if bounce_high < zd and sig_date not in used_dates:
                            signals.append({
                                'date': sig_date,
                                'type': 'third_sell',
                                'price': bounce_high,
                                'zhongshu_zg': zg,
                                'zhongshu_zd': zd,
                            })
                            used_dates.add(sig_date)
                            break
                        else:
                            break

        return signals

    # ============================================================
    # 信号映射（用于Backtrader回测）
    # ============================================================

    def get_signal_df(self):
        """
        将信号映射回原始DataFrame，添加信号列

        新增列:
          chan_signal:  0=无信号, 1=一买, 2=二买, 3=三买, -3=三卖
          chan_zg:      最近中枢的ZG（向前填充）
          chan_zd:      最近中枢的ZD（向前填充）
          weekly_trend: 占位列（多周期策略使用，默认0）
        """
        df = self.raw_df.copy()
        df['chan_signal'] = 0
        df['chan_zg'] = np.nan
        df['chan_zd'] = np.nan
        df['weekly_trend'] = 0

        signal_map = {
            'first_buy': 1, 'second_buy': 2, 'third_buy': 3, 'third_sell': -3
        }

        for sig in self.signals:
            date = sig['date']
            if date in df.index:
                df.loc[date, 'chan_signal'] = signal_map.get(sig['type'], 0)
                if 'zhongshu_zg' in sig:
                    df.loc[date, 'chan_zg'] = sig['zhongshu_zg']
                if 'zhongshu_zd' in sig:
                    df.loc[date, 'chan_zd'] = sig['zhongshu_zd']

        for zs in self.zhongshu_list:
            mask = (df.index >= zs['start_date']) & (df.index <= zs['end_date'])
            df.loc[mask, 'chan_zg'] = df.loc[mask, 'chan_zg'].fillna(zs['ZG'])
            df.loc[mask, 'chan_zd'] = df.loc[mask, 'chan_zd'].fillna(zs['ZD'])

        df['chan_zg'] = df['chan_zg'].ffill()
        df['chan_zd'] = df['chan_zd'].ffill()

        return df

    # ============================================================
    # 分析摘要
    # ============================================================

    def summary(self):
        """打印分析结果摘要"""
        top_count = sum(1 for f in self.fractals if f['type'] == 'top')
        bot_count = sum(1 for f in self.fractals if f['type'] == 'bottom')
        up_count = sum(1 for b in self.bi_list if b['direction'] == 'up')
        down_count = sum(1 for b in self.bi_list if b['direction'] == 'down')

        print("=" * 60)
        print("缠论分析摘要")
        print("=" * 60)
        print(f"  原始K线:   {len(self.raw_df)} 根")
        print(f"  合并后K线: {len(self.merged_df)} 根 (合并了 {len(self.raw_df) - len(self.merged_df)} 根)")
        print(f"  分型:      {len(self.fractals)} 个 (顶分型 {top_count}, 底分型 {bot_count})")
        print(f"  笔:        {len(self.bi_list)} 笔 (上升 {up_count}, 下降 {down_count})")
        print(f"  中枢:      {len(self.zhongshu_list)} 个")

        if self.bi_list:
            up_bis = [b for b in self.bi_list if b['direction'] == 'up']
            down_bis = [b for b in self.bi_list if b['direction'] == 'down']
            if up_bis:
                avg_up = np.mean([abs(b['end_price'] - b['start_price']) for b in up_bis])
                print(f"  上升笔均幅: {avg_up:.2f}")
            if down_bis:
                avg_down = np.mean([abs(b['end_price'] - b['start_price']) for b in down_bis])
                print(f"  下降笔均幅: {avg_down:.2f}")

        if self.zhongshu_list:
            print("\n  中枢列表:")
            for i, zs in enumerate(self.zhongshu_list):
                print(f"    [{i+1}] {zs['start_date'].strftime('%Y-%m-%d')} ~ "
                      f"{zs['end_date'].strftime('%Y-%m-%d')} | "
                      f"ZG={zs['ZG']:.2f} ZD={zs['ZD']:.2f} | "
                      f"包含{zs['bi_count']}笔")

        print(f"\n  信号:      {len(self.signals)} 个")
        sig_names = {
            'first_buy': '一买', 'second_buy': '二买',
            'third_buy': '三买', 'third_sell': '三卖',
        }
        for sig in self.signals:
            name = sig_names.get(sig['type'], sig['type'])
            extra = ''
            if 'divergence_ratio' in sig:
                extra = f" | 背驰比={sig['divergence_ratio']}"
            print(f"    {sig['date'].strftime('%Y-%m-%d')} | {name} | "
                  f"价格={sig['price']:.2f}{extra}")
        print("=" * 60)

    # ============================================================
    # 可视化
    # ============================================================

    @staticmethod
    def _draw_candlestick(ax, df, width_ratio=0.8):
        """
        在指定ax上绘制标准K线蜡烛图 (连续x轴, 无周末间隙)

        参数:
            ax: matplotlib Axes
            df: DataFrame, 需包含 open/high/low/close, 索引为日期
            width_ratio: 蜡烛体宽度与间距的比例

        返回:
            date_to_x: dict, 将日期映射到x轴整数位置,
                       用于在同一ax上添加分型/笔/中枢等标记
        """
        import matplotlib.ticker as mticker

        n = len(df)
        date_to_x = {}
        for i, dt in enumerate(df.index):
            date_to_x[dt] = i
            if hasattr(dt, 'date'):
                date_to_x[dt.date()] = i

        body_width = width_ratio

        opens = df['open'].values.astype(float)
        highs = df['high'].values.astype(float)
        lows = df['low'].values.astype(float)
        closes = df['close'].values.astype(float)

        price_range = highs.max() - lows.min()
        min_body = price_range * 0.002

        for i in range(n):
            o, h, l, c = opens[i], highs[i], lows[i], closes[i]

            if c >= o:
                color = '#e74c3c'
                body_bottom = o
                body_height = max(c - o, min_body)
            else:
                color = '#27ae60'
                body_bottom = c
                body_height = max(o - c, min_body)

            ax.plot([i, i], [l, h], color=color, linewidth=0.8, zorder=1)
            ax.bar(i, body_height, bottom=body_bottom, width=body_width,
                   color=color, edgecolor=color, linewidth=0.5, zorder=2)

        total_days = (df.index[-1] - df.index[0]).days if n > 1 else 365
        if n <= 60:
            step = max(1, n // 15)
        elif n <= 200:
            step = max(1, n // 12)
        else:
            step = max(1, n // 10)
        tick_positions = list(range(0, n, step))
        if (n - 1) not in tick_positions:
            tick_positions.append(n - 1)

        if total_days <= 180:
            tick_labels = [df.index[i].strftime('%m-%d') for i in tick_positions]
        else:
            tick_labels = [df.index[i].strftime('%Y-%m') for i in tick_positions]

        ax.set_xticks(tick_positions)
        ax.set_xticklabels(tick_labels, rotation=45, ha='right', fontsize=8)
        ax.set_xlim(-1, n)

        return date_to_x

    def plot(self, title='', save_path=None, show_bi=True, show_zhongshu=True,
             show_signals=True, show_fractals=True, show_all_fractals=False):
        """
        绘制缠论分析图表（K线蜡烛图）

        上图: K线蜡烛图 + 分型标记 + 笔连线 + 中枢方框 + 买卖点
        下图: 成交量

        参数:
            title: 图表标题
            save_path: 保存路径，如 'outputs/xxx.png'
            show_bi: 是否显示笔
            show_zhongshu: 是否显示中枢
            show_signals: 是否显示信号
            show_fractals: 是否显示分型标记（默认只显示确认分型）
            show_all_fractals: True=显示所有分型, False=只显示参与成笔的确认分型
        """
        import matplotlib.pyplot as plt
        import matplotlib.patches as patches
        import matplotlib.dates as mdates
        import matplotlib
        matplotlib.rcParams['font.sans-serif'] = ['SimHei']
        matplotlib.rcParams['axes.unicode_minus'] = False

        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(18, 10),
                                        gridspec_kw={'height_ratios': [4, 1]})

        df = self.raw_df

        # ---- 上图: K线蜡烛图 + 缠论结构 ----
        d2x = self._draw_candlestick(ax1, df)

        def _lookup_x(date_val):
            """在date_to_x中查找日期对应的x位置, 支持模糊匹配"""
            if date_val in d2x:
                return d2x[date_val]
            if hasattr(date_val, 'date'):
                return d2x.get(date_val.date())
            for k, v in d2x.items():
                if hasattr(k, 'date') and k.date() == date_val:
                    return v
                if k == date_val:
                    return v
            return None

        # 分型标记（默认只显示确认分型，减少噪音）
        if show_fractals:
            frac_list = self.fractals if show_all_fractals else self.confirmed_fractals
            frac_label_suffix = '全部' if show_all_fractals else '确认'
            if frac_list:
                for f in frac_list:
                    rd = f.get('raw_date', f['date'])
                    x = _lookup_x(rd)
                    if x is None:
                        continue
                    if f['type'] == 'top':
                        ax1.scatter(x, f['price'], marker='v', color='#e74c3c',
                                    s=50, zorder=5, alpha=0.7)
                    else:
                        ax1.scatter(x, f['price'], marker='^', color='#2ecc71',
                                    s=50, zorder=5, alpha=0.7)

        # 笔连线
        if show_bi and self.bi_list:
            for bi in self.bi_list:
                color = '#e74c3c' if bi['direction'] == 'up' else '#27ae60'
                sx = _lookup_x(bi.get('start_raw_date', bi['start_date']))
                ex = _lookup_x(bi.get('end_raw_date', bi['end_date']))
                if sx is not None and ex is not None:
                    ax1.plot([sx, ex], [bi['start_price'], bi['end_price']],
                             color=color, linewidth=1.8, alpha=0.85, zorder=4)

        # 中枢方框
        if show_zhongshu and self.zhongshu_list:
            for zs in self.zhongshu_list:
                xl = _lookup_x(zs['start_date'])
                xr = _lookup_x(zs['end_date'])
                if xl is not None and xr is not None:
                    rect = patches.Rectangle(
                        (xl, zs['ZD']),
                        xr - xl,
                        zs['ZG'] - zs['ZD'],
                        linewidth=1.5,
                        edgecolor='#3498db',
                        facecolor='#3498db',
                        alpha=0.15,
                        zorder=3,
                    )
                    ax1.add_patch(rect)
                    ax1.text(xl, zs['ZG'],
                             f" ZG={zs['ZG']:.1f}\n ZD={zs['ZD']:.1f}",
                             fontsize=7, color='#2c3e50', va='bottom')

        # 信号标记
        if show_signals and self.signals:
            sig_names = {
                'first_buy': '一买', 'second_buy': '二买',
                'third_buy': '三买', 'third_sell': '三卖',
            }
            sig_colors = {
                'first_buy': '#8e44ad', 'second_buy': '#e67e22',
                'third_buy': '#e74c3c', 'third_sell': '#27ae60',
            }
            for sig in self.signals:
                sig_x = _lookup_x(sig['date'])
                if sig_x is None:
                    continue
                marker = '^' if 'buy' in sig['type'] else 'v'
                color = sig_colors.get(sig['type'], '#333333')
                ax1.scatter(sig_x, sig['price'], marker=marker, color=color,
                            s=200, zorder=7, edgecolors='black', linewidths=1)
                ax1.annotate(sig_names.get(sig['type'], sig['type']),
                             (sig_x, sig['price']),
                             textcoords="offset points", xytext=(10, 10 if 'buy' in sig['type'] else -15),
                             fontsize=9, fontweight='bold', color=color,
                             bbox=dict(boxstyle='round,pad=0.2', facecolor='white', alpha=0.7))

        ax1.set_title(title or '缠论分析', fontsize=14, fontweight='bold')
        ax1.set_ylabel('价格')
        handles, labels = ax1.get_legend_handles_labels()
        if handles:
            ax1.legend(loc='upper left', fontsize=8)
        ax1.grid(True, alpha=0.3)

        # ---- 下图: 成交量 (使用与K线图相同的连续x轴) ----
        vol_x = list(range(len(df)))
        vol_colors = ['#e74c3c' if df['close'].iloc[i] >= df['open'].iloc[i] else '#27ae60'
                      for i in range(len(df))]
        ax2.bar(vol_x, df['volume'], color=vol_colors, alpha=0.6, width=0.8)
        n = len(df)
        total_days = (df.index[-1] - df.index[0]).days if n > 1 else 365
        step = max(1, n // 12)
        tick_pos = list(range(0, n, step))
        if (n - 1) not in tick_pos:
            tick_pos.append(n - 1)
        if total_days <= 180:
            tick_lbl = [df.index[i].strftime('%m-%d') for i in tick_pos]
        else:
            tick_lbl = [df.index[i].strftime('%Y-%m') for i in tick_pos]
        ax2.set_xticks(tick_pos)
        ax2.set_xticklabels(tick_lbl, rotation=45, ha='right', fontsize=8)
        ax2.set_xlim(-1, n)
        ax2.set_ylabel('成交量')
        ax2.set_xlabel('日期')
        ax2.grid(True, alpha=0.3)

        plt.tight_layout()

        if save_path:
            os.makedirs(os.path.dirname(save_path) if os.path.dirname(save_path) else 'outputs',
                        exist_ok=True)
            plt.savefig(save_path, dpi=150, bbox_inches='tight')
            print(f"  图表已保存: {save_path}")

        plt.close()
        return fig

    def plot_compare_merge(self, save_path=None):
        """
        对比K线合并前后的分型识别差异

        左图: 原始K线（蜡烛图） + 简单分型标记
        右图: 合并后K线（蜡烛图） + 正式分型标记
        """
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
        import matplotlib
        matplotlib.rcParams['font.sans-serif'] = ['SimHei']
        matplotlib.rcParams['axes.unicode_minus'] = False

        fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(20, 7))

        raw = self.raw_df
        merged = self.merged_df

        # ---- 左图: 原始K线蜡烛图 + "伪分型" ----
        d2x_raw = self._draw_candlestick(ax1, raw)

        for i in range(1, len(raw) - 1):
            h_p, h_c, h_n = raw['high'].iloc[i-1], raw['high'].iloc[i], raw['high'].iloc[i+1]
            l_p, l_c, l_n = raw['low'].iloc[i-1], raw['low'].iloc[i], raw['low'].iloc[i+1]
            if h_c > h_p and h_c > h_n and l_c > l_p and l_c > l_n:
                ax1.scatter(i, h_c,
                            marker='v', color='#e74c3c', s=40, alpha=0.6, zorder=5)
            elif l_c < l_p and l_c < l_n and h_c < h_p and h_c < h_n:
                ax1.scatter(i, l_c,
                            marker='^', color='#2ecc71', s=40, alpha=0.6, zorder=5)

        ax1.set_title(f'合并前 (原始{len(raw)}根K线)', fontsize=12, fontweight='bold')
        ax1.set_ylabel('价格')
        ax1.grid(True, alpha=0.3)

        # ---- 右图: 合并后K线蜡烛图 + 正式分型 ----
        d2x_merged = self._draw_candlestick(ax2, merged)

        for f in self.fractals:
            x = d2x_merged.get(f['date'])
            if x is None:
                continue
            if f['type'] == 'top':
                ax2.scatter(x, f['price'],
                            marker='v', color='#e74c3c', s=60, zorder=5)
            else:
                ax2.scatter(x, f['price'],
                            marker='^', color='#2ecc71', s=60, zorder=5)

        ax2.set_title(f'合并后 ({len(merged)}根K线, 合并{len(raw)-len(merged)}根)', fontsize=12, fontweight='bold')
        ax2.set_ylabel('价格')
        ax2.grid(True, alpha=0.3)

        plt.tight_layout()

        if save_path:
            os.makedirs(os.path.dirname(save_path) if os.path.dirname(save_path) else 'outputs',
                        exist_ok=True)
            plt.savefig(save_path, dpi=150, bbox_inches='tight')
            print(f"  图表已保存: {save_path}")

        plt.close()
        return fig
