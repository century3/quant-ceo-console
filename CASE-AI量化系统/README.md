# CASE-AI量化系统

自包含 Web 工作台：`python app.py`，浏览器打开控制台（默认 `7865`）。

全部配置集中在**本目录根**下唯一 `.env`。投研对话 Charles 在 `third_party/charles_bundle/charles-nanobot/`；框架用 `pip install nanobot-ai==0.2.2`（不要装最新版）。

---

## 一、目录结构（平铺）

```
CASE-AI量化系统/                    ← 工作台 = 整个 CASE
  app.py                          ← 入口: python app.py
  .env.example                    ← 配置样例（复制成 .env）
  requirements.txt
  README.md

  live_trading/                   ← 主循环 + state
    live_loop.py / state_store.py
  alerting/                       ← 告警分级路由
    alert_router.py
  dragon_strategy/                ← 龙头战法（本目录内 dragon_picker / dragon_backtest 等）
    dragon_picker.py
  morning_brief/                  ← 晨会 LangGraph（内嵌）
    graph.py / pusher.py / lib/{rotation_runner, factor_runner, db_config}.py
  third_party/                    ← Charles（charles-nanobot；框架 pip 装 nanobot-ai==0.2.2）

  routes/                         ← REST API（FastAPI）
    live.py / system.py / dragon.py / morning.py / backtest.py
  templates/                      ← Jinja2 模板（Tailwind + Alpine）
    base.html / live.html / system.html / chat.html / morning.html / backtest.html
  static/                         ← 静态资源
    css/main.css   js/main.js
  pages/                          ← Gradio 子应用
    tab1_chat.py                  ← 投研对话（Charles）

  lib/                            ← 工作台胶水
    paths.py                      ← 路径常量（全部指向同级目录）
    live_simulator.py             ← 启动后台 LiveTradingLoop, 提供 /api 启停
    strategy_registry.py          ← 策略注册中心（名单见仓库根 README）
    backtest_data.py / backtest_engine.py / backtest_metrics.py   ← 回测能力

  config/                         ← 运行时配置（yaml 热加载）
    mock_positions.yaml / strategies.yaml / watch_pool.yaml
  outputs/                        ← 落盘
    live_state.json               ← 共享状态契约
    live_approvals.json
  data/cache/                     ← 晨会缓存
```

---

## 二、怎么进入、怎么启动

### 1. 装依赖

```powershell
cd 22-实盘作战与CEO控制台\CASE-AI量化系统
pip install -r requirements.txt
```

国内推荐镜像：`pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple`。

### 2. 配 .env（可选）

```powershell
Copy-Item .env.example .env
notepad .env       # 按需填 QMT_PATH / DASHSCOPE_API_KEY / 钉钉 webhook / WUCAI_SQL_*
```

最小可跑：`python app.py`，dry-run 不下真单；不配 DASHSCOPE 时 `/chat` 在首次对话将报错（需通义 Key）。  
晨会、龙头 MySQL 候选、回测 MySQL 路径需 `WUCAI_SQL_*`；不配则相关页无数据或降级。

### 3. 启动

```powershell
python app.py
```

### 4. 工作台 Tab

| Tab | 路径 | 说明 |
|-----|------|------|
| **实盘监控** | `/live/sim`、`/live/real` | 持仓 / 盈亏 / 信号订单 / 龙头候选 / 事件流 / 一键操作 |
| **回测** | `/backtest` | 单股历史回测 |
| **投研对话** | `/chat` | Gradio Charles；依赖 `DASHSCOPE_API_KEY` 与 `third_party/charles_bundle` |
| **晨会分析** | `/morning` | LangGraph 流水线，读 `wucai_trade.*` |
| **系统状态** | `/system` | 组件健康检查 |

## 三、核心子目录

### live_trading/  —— 主循环 + state

每分钟一次：拉行情 → 评估持仓 → 信号 → 风控 → 下单 → 落盘 state。
共享状态写到 `outputs/live_state.json`，Web 通过它看到一切。

```powershell
# 单跑一次（不启动 Web）
python live_trading\live_loop.py --once --stocks 600519.SH,513100.SH
# 长跑
python live_trading\live_loop.py --interval 60 --stocks 600519.SH,513100.SH
```

### alerting/  —— 告警多级路由

四级渠道已接。主循环实际打 INFO / CRITICAL；INFO 聚合主循环 5 分钟。未配 webhook 只打控制台。

```powershell
python alerting\alert_router.py     # 模拟 8 条不同级别事件
```

### dragon_strategy/

龙头筛选（v1/v2）与 MySQL 回测辅助；`/api/dragon/candidates` 使用其中逻辑。完整独立演练与更多脚本见仓库内 **`CASE-龙头战法/`**（可复制，非运行本工作台必需）。

### morning_brief/

LangGraph：`industry` → `stock_picker` → `report` → `push`。读 MySQL `wucai_trade.*`。

### pages/tab1_chat.py

Gradio 投研：`/gradio-chat`，由 `chat.html` iframe 嵌入；Charles 源码在 **`third_party/charles_bundle`**，须配置 `DASHSCOPE_API_KEY`。

### lib/  —— 工作台胶水

- `paths.py`：所有路径常量都指向**同级**子目录
- `live_simulator.py`：把 `LiveTradingLoop` 包成后台线程
- `strategy_registry.py`：策略注册中心（名单见仓库根 README）
- `backtest_*.py`：回测引擎与指标

### config/  —— 运行时配置（yaml 热加载）

Web 端添加股票 / 切换策略时直接改这里，不用重启进程。

### QMT共享内存后端

在 `.env` 中设置 `WUCAI_QMT=qmt_shared`，并在大 QMT 中运行本项目的 `lib/qmt_gateway_loader.py` 即可使用共享内存行情和交易接口，不需要 `CY_QMT_API.py` 或15588端口。账户查询和实盘交易还需配置 `ACCOUNT_ID`。客户端实现位于本项目 `lib/qmt_shared.py`，不再引用第4节或其他目录。
