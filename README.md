# 实盘作战与 CEO 控制台

A 股和美股共用的量化工作台。一个进程里放投研对话、晨会、回测、模拟盘和龙头战法。

浏览器默认打开 [http://localhost:7865](http://localhost:7865)，入口会转到实盘监控。

## 目录

| 目录 | 作用 |
|------|------|
| `CASE-AI量化系统/` | Web 控制台，`python app.py` 启动 |
| `CASE-龙头战法/` | 龙头战法的独立筛选和回测脚本，说明见该目录的 README |

## 启动

```powershell
cd CASE-AI量化系统
pip install -r requirements.txt
Copy-Item .env.example .env
python app.py
```

`.env` 留在本机，不进仓库。至少填上自己的 `WUCAI_SQL_PASSWORD`。投研对话还要在系统环境变量里设置 `DASHSCOPE_API_KEY`。`TRADER_DRY_RUN=1` 时不下真单。

日线在 MySQL 库 `wucai_trade`：A 股表 `trade_stock_daily`，美股表 `trade_stock_us_daily`。

## 页面

| 路径 | 内容 |
|------|------|
| `/live` | 模拟盘与实盘监控、持仓、信号 |
| `/backtest` | 单股历史回测 |
| `/morning` | 晨会。A 股走申万，美股走等权行业分组 |
| `/chat` | Charles 投研对话 |
| `/system` | 组件健康检查 |

## 策略

策略在 `CASE-AI量化系统/lib/strategy_registry.py` 注册，`config/strategies.yaml` 按股票绑定。回测、模拟盘、实盘走同一套评估。

当前包括：`macd_1d`、`ma20_hold`、`multi_factor_top`、`dragon_picker_v1`、`dragon_picker_v2`、`rsi_reversal`、`boll_revert`、`bias_revert`、`turtle_donchian`、`grid_classic`、`chan_buy1`、`chan_buy2`、`chan_buy3`、`chan_buy2_ml`、`ml_xgb`、`ml_lgb`。

美股代码用 `COHR.US` 这种后缀。日线只读 `trade_stock_us_daily`，下单单位是 1 股，不套 A 股的涨跌停、印花税和 T+1。模拟盘只在对应市场的交易时段轮询。

## 不入库的内容

账户、数据库密码和 API Key 在本机 `.env`。模拟盘股数和成本在 `config/mock_positions.yaml` 与 `outputs/live_state.json`。课件 PDF、券商研报和财报原文也不在这个仓库里。
