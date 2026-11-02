# CASE-龙头战法（A 股化首板）

第 22 讲 Part 2 的高赔率日内策略，灵感来自 Ross Cameron 的
Gap and Go，做了 A 股 ±10% 涨跌停限制下的本土化改造。

## v1 5 大筛选法则

1. 当日涨幅 > 5%
2. 涨幅榜前 50
3. 流通市值 50–200 亿（最佳带；30–500 亿可接受）
4. 量比 > 2
5. 价格 < 30 元（排除 ST/退市）

## v2 升级（已落地）：板块共振 + 3 条硬规则补丁

> 改动 < 100 行，用 `trade_sector_daily`（21 讲已采集），无新数据依赖。

6. 涨幅 < 9.5%（**排除涨停板**：T+1 高开会让回测虚胖）
7. 上市天数 >= 60 个交易日（**排除次新股**：波动巨大 + 形态不可信）
8. **板块共振硬过滤**：所在 `sector_2` 当日涨幅 >= 0.5% 且上涨家数占比 >= 40%
9. `dragon_score` 加一项「板块共振分」（板块涨幅 + 板块齐涨家数加权）

金融含义：A 股龙头本质是题材资金，**孤雁难成龙**。一只股涨 7% 但所属
`sector_2` 当日跌，多半是诱多；站在强势板块 + 板块内多家齐涨，才是机构资金共识。

## 目录

```
CASE-龙头战法/
  dragon_strategy/
    dragon_picker.py       # 候选筛选 + 入场出场参数（mock + 在线两用）
    dragon_backtest.py     # 用 21 章 wucai_trade.* 做 T+1 历史回测（全市场聚合统计）
    dragon_replay_one.py   # 单标的逐日复盘（教学用，给定一只股 + 区间，逐日看 v1/v2 触发与未来真实涨幅）
    db_config.py           # 复用第 21 讲数据准备的 MySQL 同库连接
  outputs/                 # 回测产物 (CSV) 写到这里
  requirements.txt
  .env.example             # 配置 WUCAI_SQL_*，与 21 讲数据准备同库
```

## Demo（三层递进）

```powershell
pip install -r requirements.txt

# 1) 选股逻辑 demo（mock 数据, 5 秒看清 v1 + v2 法则）
python dragon_strategy\dragon_picker.py

# 2) 全市场历史回测（读 wucai_trade.*, 看 v1 vs v2 在真实数据上的取舍）
#    v1 对照
python dragon_strategy\dragon_backtest.py --start 2025-01-01 --end 2025-06-30 `
    --top 5 --hold 3 --no-sector-resonance --max-change 0.10 --min-listed-days 0
#    v2 默认
python dragon_strategy\dragon_backtest.py --start 2025-01-01 --end 2025-06-30 --top 5 --hold 3

# 3) 单标的逐日复盘（教学用, 看"为什么是它/为什么 v2 拦掉"）
python dragon_strategy\dragon_replay_one.py --code 002384.SZ `
    --start 2025-09-01 --end 2025-12-31 --hold 3 `
    --max-price 200 --mcap-high 5000e8
```

### 真实数据对照（2025H1, Top 5 / hold 3, 本地 wucai_trade）

| 口径 | 信号 | 胜率 | 均收 | 累计 | 年化 | Sharpe | MDD |
|------|-----|------|------|------|------|--------|-----|
| v1（关共振 / 不限涨停 / 不限次新） | 77 | 54.5% | +1.30% | +12.6% | +231% | 1.94 | -18.5% |
| v2（默认全开） | **6** | **83.3%** | **+5.10%** | +0.14% | +18.9% | 0.72 | **-6.84%** |

**取舍**：v2 把胜率从 54%→83%、单笔均收从 1.3%→5.1%、MDD 从 -18%→-6.8%；但信号数从 77→6 笔（共振太严），导致累计净值反而 v1 高。这正是 23 讲 Walk-Forward 要解决的「质量 vs 频次」问题。

## 历史回测口径（A 股 T+1 友好）

每个交易日 T:

1. 读全市场 T 与 T-5..T-1 日 K, 拼 `day_change_pct / volume_ratio / price / float_market_cap`；
2. v2: join `trade_sector_daily` 给每只股注入 `sector_change_pct / sector_rise_ratio`；
3. 跑 `filter_dragon_candidates`（v1 5 法则 + v2 3 条硬规则补丁），按 `dragon_score` 排序取 Top K；
4. 模拟 **T+1 开盘买入、T+H 收盘卖出**（H ∈ {1, 3, 5}，规避 T+1 限制）；
5. 汇总：胜率 / 平均收益 / 中位 / 等权累计净值 / 年化 / Sharpe / 最大回撤；
6. 按板块（默认 `sector_2`）汇总，看龙头是否集中在少数题材；
7. 输出 CSV 含 `sector_chg / sector_rise`，方便复盘看共振有没有起作用。

> 选股口径与 `dragon_picker.py` 完全一致（backtest 直接复用 picker 函数），仅把数据源换成 MySQL。
> 流通市值 = `float_shares × close`（来自 `trade_stock_status.float_shares`），近似业内做法。

## 用法

```python
from dragon_strategy.dragon_picker import filter_dragon_candidates, DragonEntryExit

# v2 默认带板块共振 (要求传入 sector_change_pct / sector_rise_ratio / listed_days)
candidates = filter_dragon_candidates(
    stocks_today,
    min_change=0.05, max_price=30, mcap_range=(30e8, 500e8),
    min_volume_ratio=2.0,
    require_sector_resonance=True,   # 板块共振硬过滤; 关掉就退化成 v1
    max_change=0.095,                # 排除涨停板
    min_listed_days=60,              # 排除次新股
)
entry = DragonEntryExit(capital=1_000_000).calc_entry(candidates[0])
```

## v3 升级路线（留给第 23 讲「持续进化」）

- **封板信号**：接入涨停板盘口（封单 / 首封时间 / 炸板率），对应业内首板/打板战法（要新采集 Level-2，本期未做）。
- **Walk-Forward 权重学习**：把现有「经验拍脑袋」的 5+v2 项权重改为按滚动窗口拟合（23 章「参数调优」一节展开）。
- **入场细化**：分时强度判定（早盘 30 分钟 / 尾盘 15 分钟），止损接 ATR 而非固定 2%。
- **资金流增强**：`trade_stock_fund_flow.main_net_pct / super_net_pct` 替代量比（注意学员侧只有 8 个月历史）。

## 与 CASE-CEO工作台 的关系

- 候选名单由 **CASE-CEO工作台** 的 `/api/dragon/candidates` 提供，后端调用本 CASE 的 `dragon_picker.filter_dragon_candidates`（CASE-CEO工作台 内部已带一份 `dragon_strategy/dragon_picker.py` 副本，避免学员只拷一个目录跑不起来）。
- `/live` 页面右栏有「龙头战法候选」面板：刷新候选 → 一键加入监控池 → 自动绑 `dragon_picker` 策略。表格里能直接看到「板块」和「板块涨」两列，是 v2 共振的可视化。
- mock 模式默认开 v2 共振过滤；xtdata 模式（盘中实时）默认关共振，因为 `trade_sector_daily` 是收盘后才算出来的。前端勾选 xtdata 时会有提示。
- 历史回测（`dragon_backtest.py`）只在本 CASE 跑，CSV 写到 `outputs/`。
- **铁律提醒**：纪律性远比技术重要，FTC 起诉指出 99% 学员复制 Ross Cameron 后亏损。
