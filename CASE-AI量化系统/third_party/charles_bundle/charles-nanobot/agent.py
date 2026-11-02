#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Charles 投研 Agent -- nanobot 版

基于 nanobot 框架，利用其 Skills 渐进式加载机制:
  - AGENTS.md 定义核心身份（始终加载）
  - skills/investment-research/SKILL.md 定义五步法（按需加载）
  - 内置 web_search(Tavily) + exec(运行分析脚本) + 文件工具

运行方式:
    python agent.py
    python agent.py -m "帮我写一份中芯国际的研报"

依赖:
    pip install nanobot-ai==0.2.2   # PyPI 包名；代码里 import nanobot；不要装最新版

需要环境变量:
    DASHSCOPE_API_KEY  -- 通义千问 API
    TAVILY_API_KEY     -- Tavily 搜索（可选）
"""

import asyncio
import os
import sys
from datetime import datetime
from pathlib import Path

# Windows UTF-8 兼容
if sys.platform == "win32":
    os.environ.setdefault("PYTHONUTF8", "1")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

WORKSPACE = Path(__file__).resolve().parent
# 框架：pip 包 nanobot-ai==0.2.2（import 名 nanobot；不要装最新版，API 对不上）
from nanobot.agent.hook import AgentHook, AgentHookContext
from nanobot.agent.loop import AgentLoop
from nanobot.agent.tools.mcp import MCPProvider
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.config.loader import load_config
from nanobot.nanobot import Nanobot
from nanobot.session.manager import SessionManager


class CharlesHook(AgentHook):
    """显示工具调用过程"""

    async def before_execute_tools(self, ctx: AgentHookContext) -> None:
        for tc in ctx.tool_calls:
            args_str = str(tc.arguments)[:120]
            print(f"  [{tc.name}] {args_str}")


def _inject_time_context():
    """将当前日期写入 memory/MEMORY.md"""
    memory_file = WORKSPACE / "memory" / "MEMORY.md"
    memory_file.parent.mkdir(parents=True, exist_ok=True)

    today_str = datetime.now().strftime("%Y年%m月%d日")
    weekday_names = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
    weekday_str = weekday_names[datetime.now().weekday()]

    memory_file.write_text(
        f"# 当前时间上下文\n\n"
        f"今天是 {today_str} {weekday_str}。\n"
        f"2024年及之前 = 历史数据; 2025年 = 近期已发生; 2026年截至今天 = 已发生。\n"
        f"撰写研报时确保时间表述准确。\n",
        encoding="utf-8",
    )


def build_bot() -> Nanobot:
    """构建 Charles Agent"""
    dashscope_key = os.environ.get("DASHSCOPE_API_KEY", "")
    if not dashscope_key:
        print("[错误] 请设置环境变量 DASHSCOPE_API_KEY")
        sys.exit(1)

    config = load_config(WORKSPACE / "config.json")
    config.providers.dashscope.api_key = dashscope_key
    config.tools.web.search.api_key = os.environ.get("TAVILY_API_KEY", "")
    config.agents.defaults.workspace = str(WORKSPACE)

    # nanobot 0.3 起工具表由调用方持有; 会话目录不能放在 workspace 里面
    tools = ToolRegistry()
    mcp_provider = MCPProvider.from_config(config, tools)
    sessions_root = WORKSPACE.parent / "charles_runtime" / "sessions"
    session_manager = SessionManager(Path(config.agents.defaults.workspace), sessions_root=sessions_root)
    loop = AgentLoop.from_config(
        config,
        tool_registry=tools,
        session_manager=session_manager,
    )

    _inject_time_context()

    return Nanobot(loop, config=config, mcp_provider=mcp_provider)


async def run_interactive():
    """交互式运行"""
    bot = build_bot()
    loop = bot._loop

    skills = loop.context.skills.list_skills()
    skill_names = " | ".join(s["name"] for s in skills)
    print("=" * 60)
    print("  Charles 投研情报官 (nanobot 版)")
    print(f"  模型: {loop.model}")
    print(f"  技能: {skill_names}")
    print(f"  工作目录: {loop.workspace}")
    print("  输入问题后按回车, 输入 'quit' 退出")
    print("=" * 60)

    while True:
        try:
            user_input = input("\n你: ").strip()
            if not user_input:
                continue
            if user_input.lower() in ("quit", "exit", "q"):
                print("Charles: 再见，祝投资顺利!")
                break

            print("  [思考中...]")
            result = await bot.run(user_input, session_key="charles:interactive", hooks=[CharlesHook()])
            print(f"\nCharles: {result.content}")

        except KeyboardInterrupt:
            print("\n已退出")
            break
        except Exception as e:
            print(f"\n[错误] {type(e).__name__}: {e}")


async def run_single(message: str):
    """单次运行"""
    bot = build_bot()
    print(f"问题: {message}")
    print("  [思考中...]")
    result = await bot.run(message, session_key="charles:oneshot", hooks=[CharlesHook()])
    print(f"\nCharles: {result.content}")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Charles 投研情报官 (nanobot 版)")
    parser.add_argument("-m", "--message", default=None, help="单次提问（不传则进入交互模式）")
    args = parser.parse_args()

    if args.message:
        asyncio.run(run_single(args.message))
    else:
        asyncio.run(run_interactive())


if __name__ == "__main__":
    main()
