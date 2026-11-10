# -*- coding: utf-8 -*-
# Author：@跟陈博士学AI
# 25-AI量化系统 主入口 -- FastAPI + 挂载 Gradio (投研对话)
"""
单进程统一服务:
    FastAPI       -- 主框架 + REST + SSE
    Tailwind CSS  -- 前端 (CDN)
    Alpine.js     -- 前端交互 (CDN)
    Plotly.js     -- 图表 (CDN)
    Gradio        -- 仅用于投研对话, 挂载到 /chat (复用 pages/tab1_chat.py)

URL 结构:
    /            -- 默认重定向到 /live
    /chat/*      -- Gradio 投研对话
    /morning     -- 晨会分析 HTML (读库)
    /live        -- 实盘监控 HTML
    /backtest    -- 回测 HTML
    /review      -- 复盘归因 HTML (Brinson + Walk-Forward + 生命周期)
    /system      -- 系统状态 HTML
    /api/*       -- REST API
    /static/*    -- 静态资源 (CSS/JS)

启动:
    python app.py        -- 默认 7865 端口
"""

import os
import socket
import sys
from pathlib import Path

# Windows UTF-8
if sys.platform == "win32":
    os.environ.setdefault("PYTHONUTF8", "1")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")

# 加载唯一 .env（路径见 lib.paths.ENV_FILE）
THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS_DIR))
from dotenv import load_dotenv
from lib.paths import ENV_FILE, setup_sys_path
load_dotenv(ENV_FILE)
setup_sys_path()

import gradio as gr
import uvicorn
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import RedirectResponse, HTMLResponse

from routes import morning, live, review, system as sys_route, backtest, dragon, holdings_news
from routes.live import _SIM as _LIVE_SIM
from lib.live_simulator import merge_watch_codes


# ============================================================
# Gradio 投研对话 -- 仅 Tab 1, 不带原来的多 Tab 外壳
# ============================================================

def build_chat_only_gradio():
    """只挂 Charles 投研对话, 不要顶部导航 (导航交给 FastAPI)"""
    from pages import tab1_chat
    with gr.Blocks(
        title="投研对话",
        theme=gr.themes.Soft(
            primary_hue="indigo",
            font=[gr.themes.GoogleFont("Inter"), "Microsoft YaHei", "sans-serif"],
        ),
        analytics_enabled=False,
        css="""
.gradio-container { max-width: 100% !important; padding: 16px !important; }
""",
    ) as app:
        tab1_chat.build_tab()
    return app


# ============================================================
# FastAPI 主应用
# ============================================================

api = FastAPI(title="AI 量化系统", docs_url="/api/docs", redoc_url=None)

# 静态资源
api.mount("/static", StaticFiles(directory=str(THIS_DIR / "static")), name="static")

# Jinja2 模板
templates = Jinja2Templates(directory=str(THIS_DIR / "templates"))

# REST 路由
api.include_router(morning.router,   prefix="/api/morning",  tags=["morning"])
api.include_router(holdings_news.router, prefix="/api/holdings-news", tags=["holdings-news"])
api.include_router(live.router,      prefix="/api/live",     tags=["live"])
api.include_router(sys_route.router, prefix="/api/system",   tags=["system"])
api.include_router(backtest.router,  prefix="/api/backtest", tags=["backtest"])
api.include_router(review.router,    prefix="/api/review",   tags=["review"])
api.include_router(dragon.router,    prefix="/api/dragon",   tags=["dragon"])


# ------------- 页面路由 -------------

@api.get("/", response_class=HTMLResponse)
def root():
    return RedirectResponse(url="/live")


@api.get("/chat", response_class=HTMLResponse)
def page_chat(request: Request):
    return templates.TemplateResponse("chat.html",
                                      {"request": request, "active": "chat"})


@api.get("/morning", response_class=HTMLResponse)
def page_morning(request: Request):
    return templates.TemplateResponse("morning.html",
                                      {"request": request, "active": "morning"})


@api.get("/holdings-news", response_class=HTMLResponse)
def page_holdings_news(request: Request):
    return templates.TemplateResponse("holdings_news.html",
                                      {"request": request, "active": "holdings-news"})


@api.get("/live", response_class=HTMLResponse)
def page_live():
    # 默认进模拟盘 (后台一直跑, 看着安全); 实盘走 /live/real
    return RedirectResponse(url="/live/sim")


def _live_page_ctx(request: Request, view_mode: str) -> dict:
    ctx = {"request": request, "active": "live", "view_mode": view_mode}
    ctx.update(live.tool_ctx())
    return ctx


@api.get("/live/sim", response_class=HTMLResponse)
def page_live_sim(request: Request):
    return templates.TemplateResponse("live.html", _live_page_ctx(request, "sim"))


@api.get("/live/real", response_class=HTMLResponse)
def page_live_real(request: Request):
    return templates.TemplateResponse("live.html", _live_page_ctx(request, "real"))


@api.get("/backtest", response_class=HTMLResponse)
def page_backtest(request: Request):
    return templates.TemplateResponse("backtest.html",
                                      {"request": request, "active": "backtest"})


@api.get("/review", response_class=HTMLResponse)
def page_review(request: Request):
    ctx = {"request": request, "active": "review"}
    ctx.update(live.tool_ctx())
    return templates.TemplateResponse("review.html", ctx)


@api.get("/system", response_class=HTMLResponse)
def page_system(request: Request):
    return templates.TemplateResponse("system.html",
                                      {"request": request, "active": "system"})


# ------------- 挂载 Gradio 到 /gradio-chat/ (供 /chat 页面 iframe 嵌入) -------------

gradio_app = build_chat_only_gradio()
api = gr.mount_gradio_app(api, gradio_app, path="/gradio-chat")


# ------------- 启动时自动开启模拟盘 engine -------------
# 注意: 不能用 @api.on_event("startup") -- gradio mount 之后 hook 会被包装丢掉.
# 走 main() 里同步调一次: _SIM.start() 内部是后台线程, 不阻塞 uvicorn.

def _auto_start_sim():
    """程序启动后自动跑模拟盘 (dry_run=True), 不需要用户手动点启动.
    监控池为空 (无 mock 持仓 / watch_pool / per_stock) 时跳过, 等用户添加股票后再手动启动."""
    try:
        merged = merge_watch_codes([])
        if not merged:
            print("[live] 监控池为空, 跳过自动启动模拟盘 -- 添加股票后请手动启动", flush=True)
            return
        msg = _LIVE_SIM.start(watch_stocks=merged, dry_run=True, cycle_seconds=60)
        print(f"[live] 自动启动模拟盘:\n{msg}", flush=True)
    except Exception as e:
        print(f"[live] 自动启动模拟盘失败 (不影响 web 运行): {type(e).__name__}: {e}", flush=True)


# ============================================================
# 启动
# ============================================================

def _find_free_port(start_port: int, host: str = "0.0.0.0", max_tries: int = 50) -> int:
    """从 start_port 起找空闲端口 (Windows 不能用 SO_REUSEADDR)"""
    for offset in range(max_tries):
        port = start_port + offset
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind((host, port))
                return port
        except OSError:
            continue
    raise RuntimeError(f"在 {start_port}-{start_port+max_tries-1} 找不到空闲端口")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="AI 量化交易系统 (FastAPI + Gradio)")
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("DASHBOARD_PORT", 7865)))
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--no-auto-port", action="store_true")
    args = parser.parse_args()

    desired_port = args.port
    actual_port = desired_port
    if not args.no_auto_port:
        actual_port = _find_free_port(desired_port, host=args.host)
        if actual_port != desired_port:
            print(f"[INFO] 端口 {desired_port} 已被占用, 自动切换到 {actual_port}")

    print()
    print("=" * 70)
    print("  AI 量化交易系统启动 (FastAPI + Tailwind + Alpine + Gradio)")
    print("=" * 70)
    print(f"  Web UI:    http://localhost:{actual_port}  (默认进入 /live)")
    print(f"  Gradio:    http://localhost:{actual_port}/gradio-chat/  (内嵌于 /chat)")
    print(f"  默认 dry-run, 不会真下单")
    print("=" * 70)
    print()

    # 自动启动模拟盘 (后台线程, 不阻塞 uvicorn)
    _auto_start_sim()

    uvicorn.run(api, host=args.host, port=actual_port,
                log_level="info", access_log=False)


if __name__ == "__main__":
    main()
