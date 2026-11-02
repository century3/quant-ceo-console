# -*- coding: utf-8 -*-
# Author：@跟陈博士学AI
# 投研对话 Tab -- 跑 nanobot 版 Charles, qwen-agent 风格
"""
Tab 1: 投研对话 (Charles, nanobot 框架)

样式参考 qwen-agent 的实现:
    - 工具调用 + 工具结果配对显示, 按发生顺序紧挨着
    - 用原生 <details><summary> 折叠, CSS 美化 summary
    - 右侧用 gr.HTML 展示插件清单
    - 流式: 通过 nanobot AgentHook 把 token / 工具事件推到队列, generator 边收边渲染

工程要点:
    - nanobot 的 bot.run() 是一次性的, 真流式要走底层 loop.process_direct(on_stream=...)
    - nanobot 是 asyncio, 我们在后台线程跑一个独立 event loop, 用 queue.Queue 跨线程通信
    - Charles 第一次调 build_bot() 要加载 skills + provider, 较重 -> 全局缓存

环境变量:
    DASHSCOPE_API_KEY -- 通义千问 API (必填)
    TAVILY_API_KEY    -- Tavily 搜索 (可选)

Charles 加载路径固定为 CASE-AI 根下 third_party/charles_bundle/charles-nanobot/agent.py 。
框架用 pip 包 nanobot-ai==0.2.2（import 名 nanobot；不要装最新版）。
"""

from __future__ import annotations

import asyncio
import json
import os
import queue
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

import gradio as gr

from lib.paths import setup_sys_path
setup_sys_path()


DEFAULT_THREAD = "ui:single"


# 等待 / 工作中的 loader (三点跳动 CSS 动画) -- 替代静态 "..."
LOADER_THINKING = (
    '<span class="qa-loader">Charles 正在思考'
    '<span class="qa-loader-dots"><i></i><i></i><i></i></span>'
    '</span>'
)
LOADER_WORKING = (
    '<span class="qa-loader">Charles 正在工作'
    '<span class="qa-loader-dots"><i></i><i></i><i></i></span>'
    '</span>'
)


# ============================================================
# qwen-agent 风格的模板 (跟 qwen_agent/gui/utils.py 同款)
# ============================================================

THINK_TPL = """
<details open class="qa-think">
<summary>Thinking ...</summary>

<div style="color: gray;">{thought}</div>
</details>
"""

TOOL_CALL_TPL = """
<details class="qa-toolcall">
<summary>Start calling tool "{tool_name}" ...</summary>

```
{tool_input}
```
</details>
"""

TOOL_OUTPUT_TPL = """
<details class="qa-tooloutput">
<summary>Finished tool calling.</summary>

```
{tool_output}
```
</details>
"""


# ============================================================
# Charles cover
# ============================================================

CHARLES_COVER_HTML = """
<div style="padding:14px 16px;border-radius:10px;background:#fff;border:1px solid rgb(229,231,235);">
  <div style="font-size:20px;font-weight:600;color:#1f2937;margin-bottom:6px;">Charles</div>
  <div style="font-size:12.5px;color:#6b7280;line-height:1.7;">
    投研主管 · 国泰君安五步法<br/>
    擅长基本面分析 / 行业对比 / 估值
  </div>
</div>
"""


# nanobot 版 Charles 的内置工具清单 (参考 charles-nanobot/agent.py + skills)
CHARLES_TOOLS = [
    "web_search (Tavily)",
    "exec (脚本执行)",
    "read_file / write_file",
    "skill: investment-research (五步法)",
    "skill: web-search",
    "skill: report-writing",
]


PLUGINS_HTML = (
    '<div style="margin-top:14px;">'
    '<div style="font-size:12px;color:#6b7280;font-weight:600;margin-bottom:8px;">插件</div>'
    '<div style="font-size:12.5px;color:#4b5563;line-height:2;">'
    + "".join(
        f'<div><span style="color:#9ca3af;margin-right:6px;">&#9745;</span>{t}</div>'
        for t in CHARLES_TOOLS
    )
    + "</div></div>"
)


PROMPT_SUGGESTIONS = [
    ["帮我用国泰君安五步法分析贵州茅台 (600519)"],
    ["中芯国际 (688981) 最近的财务数据怎么样? 估值合理吗?"],
    ["比亚迪 vs 长城汽车横向对比, 谁更值得投资?"],
    ["最近两周新能源板块的核心利好和利空有哪些?"],
    ["分析下宁德时代 (300750) 的护城河和竞争对手"],
    ["写一份 600519 贵州茅台的多空辩论 (一段牛市观点 + 一段熊市观点)"],
]


# ============================================================
# nanobot Charles 加载 (延迟 + 缓存)
# ============================================================

# Charles 加载路径（third_party 内 charles-nanobot/agent.py, 依赖 pip 版 nanobot）
_CHARLES_AGENT_PY = (
    Path(__file__).resolve().parents[1] / "third_party" / "charles_bundle" / "charles-nanobot" / "agent.py"
)


def _resolve_charles_path() -> Path | None:
    """返回 agent.py 路径；文件不存在则为 None."""
    p = _CHARLES_AGENT_PY
    return p if p.is_file() else None


def _load_charles_module(agent_py_path: Path):
    """import nanobot 版 charles agent.py, 返回模块 (带 build_bot 函数)"""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        f"charles_nanobot_agent_{agent_py_path.parent.name}", agent_py_path,
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if not hasattr(mod, "build_bot"):
        raise AttributeError(f"{agent_py_path} 缺少 build_bot() 函数")
    return mod


# 后台 event loop (跨线程跑 nanobot 异步流程)
class _BackgroundLoop:
    """单例: 在后台 daemon 线程里跑一个 asyncio event loop, 复用给所有请求"""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._init()
        return cls._instance

    def _init(self):
        self.loop: asyncio.AbstractEventLoop | None = None
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="charles-asyncio")
        self._thread.start()
        self._ready.wait(timeout=5)

    def _run(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self._ready.set()
        self.loop.run_forever()

    def submit(self, coro) -> asyncio.Future:
        """把协程丢到后台 loop 里跑, 返回 concurrent.futures.Future (线程安全)"""
        return asyncio.run_coroutine_threadsafe(coro, self.loop)


_CHARLES_CACHE: dict = {
    "bot": None,
    "thread_id": DEFAULT_THREAD,
    "load_err": None,
    "history": [],  # 投研对话气泡; 切走其他 Tab 再回来要从这里灌回 UI
}


def _snapshot_history(history: list) -> list:
    """拷贝一份 messages, 避免后续改同一条 dict 把缓存带跑。"""
    if not history:
        return []
    out = []
    for m in history:
        if isinstance(m, dict):
            out.append({"role": m.get("role"), "content": m.get("content")})
        else:
            out.append(m)
    return out


def _save_ui_history(history: list) -> None:
    _CHARLES_CACHE["history"] = _snapshot_history(history)


def _clean_restored_history(history: list) -> list:
    """F5 后灌回气泡: 去掉还在转圈的 loader, 空回复不占一条假消息。"""
    history = _snapshot_history(history)
    if not history:
        return []
    last = history[-1]
    if last.get("role") != "assistant":
        return history
    raw = str(last.get("content") or "")
    if "qa-loader" not in raw:
        return history
    content = raw.replace(LOADER_THINKING, "").replace(LOADER_WORKING, "").strip()
    plain = content
    for a, b in (("<span", " "), ("</span>", " "), ("<i>", " "), ("</i>", " ")):
        plain = plain.replace(a, b)
    plain = " ".join(plain.split())
    if (not plain) or plain.startswith("(已用时"):
        history.pop()
    else:
        last["content"] = content
    return history


def restore_ui_history(stored=None):
    """F5 后从 BrowserState 灌回 Chatbot。"""
    return _clean_restored_history(stored or [])


def persist_ui_history(history: list):
    """Chatbot 一变就写入 BrowserState, F5 后还能灌回来。"""
    snap = _snapshot_history(history)
    _CHARLES_CACHE["history"] = snap
    return snap


def _ensure_charles_bot():
    """加载 nanobot Charles, 失败抛 RuntimeError 带可读原因"""
    if _CHARLES_CACHE["bot"] is not None:
        return _CHARLES_CACHE["bot"]
    if _CHARLES_CACHE["load_err"]:
        raise RuntimeError(_CHARLES_CACHE["load_err"])

    if not os.environ.get("DASHSCOPE_API_KEY", "").strip():
        msg = ("环境变量 DASHSCOPE_API_KEY 未设置. "
               "请在系统环境变量或 .env 中配置后再启动 app.py.")
        _CHARLES_CACHE["load_err"] = msg
        raise RuntimeError(msg)

    p = _resolve_charles_path()
    if p is None:
        msg = ("找不到 nanobot Charles: "
               f"{_CHARLES_AGENT_PY} （请确认 third_party/charles_bundle 下已有 charles-nanobot, 且已 pip install nanobot-ai==0.2.2）")
        _CHARLES_CACHE["load_err"] = msg
        raise RuntimeError(msg)

    print(f"[chat] 加载 nanobot Charles: {p}", flush=True)
    try:
        mod = _load_charles_module(p)
        bot = mod.build_bot()
    except Exception as e:
        msg = f"加载 Charles 失败: {type(e).__name__}: {e}\n{traceback.format_exc()}"
        _CHARLES_CACHE["load_err"] = msg
        raise RuntimeError(msg) from e

    _CHARLES_CACHE["bot"] = bot
    print(f"[chat] Charles 已加载, model={bot._loop.model}", flush=True)
    return bot


# ============================================================
# 渲染 (跟旧版 qwen-agent 风格一致)
# ============================================================

def _format_tool_args(raw_args) -> str:
    try:
        if isinstance(raw_args, dict):
            return json.dumps(raw_args, ensure_ascii=False, indent=2)
        return str(raw_args)
    except Exception:
        return str(raw_args)


def _truncate(s, max_len: int = 800) -> str:
    s = str(s).strip()
    if len(s) > max_len:
        return s[:max_len] + "\n...(已截断, 共 " + str(len(s)) + " 字符)"
    return s


def _render_steps(steps: list, final_text: str = "") -> str:
    """按 qwen-agent 风格渲染思考过程 + 最终回复

    steps 元素:
        {"type": "tool", "name": "web_search", "args": "...", "output": "..." or None}
    """
    parts = []
    for step in steps:
        if step["type"] == "tool":
            parts.append(TOOL_CALL_TPL.format(
                tool_name=step["name"],
                tool_input=step["args"],
            ))
            if step.get("output") is not None:
                parts.append(TOOL_OUTPUT_TPL.format(
                    tool_output=_truncate(step["output"], 800),
                ))
    if final_text:
        parts.append("\n" + final_text)
    return "".join(parts)


# ============================================================
# AgentHook -- 收集工具事件 + 流式 token 推到队列
# ============================================================

class _StreamCollectorHook:
    """nanobot AgentHook: 把工具调用事件推到 thread-safe queue

    跟 nanobot AgentHook 接口签名对齐, 不强 import nanobot (留给 build_bot 时再加载).
    LLM token 流不走 hook, 走 process_direct(on_stream=...) 直接回调.
    """

    def __init__(self, q: queue.Queue):
        self._q = q
        self._tool_idx_by_id: dict[str, int] = {}
        self._tool_count = 0

    # nanobot AgentHook ABI -- token 流式走 process_direct(on_stream=...), hook 只收工具事件
    def wants_streaming(self) -> bool:
        return False

    async def before_run(self, context) -> None:
        pass

    async def after_run(self, context) -> None:
        pass

    async def on_error(self, context) -> None:
        pass

    async def on_finally(self, context) -> None:
        pass

    async def emit_reasoning(self, reasoning_content=None) -> None:
        pass

    async def emit_reasoning_end(self) -> None:
        pass

    async def before_iteration(self, context) -> None:
        pass

    async def on_stream(self, context, delta: str) -> None:
        pass

    async def on_stream_end(self, context, *, resuming: bool) -> None:
        pass

    async def before_execute_tools(self, context) -> None:
        # context.tool_calls: list[ToolCallRequest(id, name, arguments, ...)]
        for tc in context.tool_calls:
            self._tool_count += 1
            tc_id = getattr(tc, "id", f"_auto_{self._tool_count}")
            self._tool_idx_by_id[tc_id] = self._tool_count - 1
            self._q.put(("tool_call", {
                "id": tc_id,
                "name": getattr(tc, "name", "?"),
                "args": _format_tool_args(getattr(tc, "arguments", {})),
            }))

    async def after_iteration(self, context) -> None:
        # context.tool_results 是按 tool_calls 同序排列的工具返回
        # 注意: 一次 iteration 可能既有工具调用又有最终回复, 我们只关心工具结果在这里 flush
        results = getattr(context, "tool_results", None) or []
        calls = getattr(context, "tool_calls", None) or []
        for call, res in zip(calls, results):
            tc_id = getattr(call, "id", None)
            content = res
            # nanobot tool_results 元素结构因 provider 而异, 统一转字符串
            if hasattr(res, "content"):
                content = getattr(res, "content")
            elif isinstance(res, dict):
                content = res.get("content", res)
            self._q.put(("tool_output", {
                "id": tc_id,
                "output": str(content),
            }))

    def finalize_content(self, context, content):
        return content


# ============================================================
# 流式 chat -- generator
# ============================================================

def chat_with_charles_stream(message: str, history: list):
    """流式跟 Charles 对话, 按 qwen-agent 风格渲染"""
    def _commit():
        # 边流边落盘: 用户中途切走 Tab, 回来也能看到当时的最后一帧
        _save_ui_history(history)
        return history, ""

    if not message or not message.strip():
        yield _commit()
        return

    history = history or []
    history.append({"role": "user", "content": message})
    history.append({"role": "assistant", "content": LOADER_THINKING})
    yield _commit()

    # 1) 加载 Charles (失败直接把错误显示给用户)
    try:
        bot = _ensure_charles_bot()
    except Exception as e:
        history[-1]["content"] = f"[ERROR] {e}"
        yield _commit()
        return

    # 2) 后台 event loop 跑 nanobot, hook 把事件推队列
    bg = _BackgroundLoop()
    q: queue.Queue = queue.Queue()
    hook = _StreamCollectorHook(q)

    async def _on_stream(delta: str) -> None:
        """LLM token 流式回调 -- 直接推到队列"""
        if delta:
            q.put(("token", delta))

    async def _runner():
        loop_obj = bot._loop
        # 临时把 hook 注入 loop._extra_hooks (process_direct 没有 extra_hooks 参数,
        # 但 _run_agent_loop 会读 self._extra_hooks 拼到 _LoopHookChain)
        prev_hooks = loop_obj._extra_hooks
        loop_obj._extra_hooks = [hook]
        try:
            response = await loop_obj.process_direct(
                message,
                session_key=_CHARLES_CACHE["thread_id"],
                on_stream=_on_stream,
            )
            content = (response.content if response else None) or ""
            q.put(("final", content))
        except Exception as e:
            q.put(("error", f"{type(e).__name__}: {e}\n{traceback.format_exc()}"))
        finally:
            loop_obj._extra_hooks = prev_hooks
            q.put(("done", None))

    fut = bg.submit(_runner())

    steps: list = []
    tool_idx_by_id: dict[str, int] = {}
    final_text = ""
    streaming_buf = ""   # 当前 iteration 的流式 token
    last_yield = time.time()
    started_at = time.time()

    def _compose() -> str:
        """把 steps + 当前流式缓冲 + 最终文本拼成 markdown"""
        body = _render_steps(steps)
        # 流式缓冲: 只在还没拿到 final 时显示 (final 来了会覆盖)
        if final_text:
            return body + "\n" + final_text
        if streaming_buf:
            return body + "\n" + streaming_buf
        return body + "\n\n" + LOADER_WORKING

    while True:
        try:
            kind, payload = q.get(timeout=0.3)
        except queue.Empty:
            # 心跳: 每 0.3 秒刷一次进度, 让用户看到"还在跑"
            now = time.time()
            if now - last_yield > 1.0:
                last_yield = now
                history[-1]["content"] = _compose() + (
                    f"\n\n<span style='color:#9ca3af;font-size:11px;'>(已用时 "
                    f"{int(now - started_at)}s)</span>"
                )
                yield _commit()
            continue

        if kind == "done":
            break
        if kind == "error":
            history[-1]["content"] = (_compose()
                                       + f"\n\n[ERROR] {payload}")
            yield _commit()
            # 等 done
            continue
        if kind == "token":
            streaming_buf += payload
        elif kind == "tool_call":
            # 一旦有工具调用, 当前流式缓冲提交 (作为「思考片段」并入 steps 之前)
            # 注意 nanobot 一般是 token 流先, 然后工具调用; 不强求合并, 直接追加 step
            steps.append({
                "type": "tool",
                "name": payload["name"],
                "args": payload["args"],
                "output": None,
                "_id": payload["id"],
            })
            tool_idx_by_id[payload["id"]] = len(steps) - 1
            streaming_buf = ""   # 工具调用后开新 iteration, 缓冲清空
        elif kind == "tool_output":
            tc_id = payload["id"]
            if tc_id in tool_idx_by_id:
                steps[tool_idx_by_id[tc_id]]["output"] = payload["output"]
            else:
                # 找最后一个 output 还是 None 的 step 兜底
                for s in reversed(steps):
                    if s["type"] == "tool" and s.get("output") is None:
                        s["output"] = payload["output"]
                        break
        elif kind == "final":
            final_text = payload or ""

        # 节流 yield
        now = time.time()
        if now - last_yield > 0.25:
            last_yield = now
            history[-1]["content"] = _compose()
            yield _commit()

    # 收尾
    if final_text:
        history[-1]["content"] = _render_steps(steps, final_text)
    elif streaming_buf:
        history[-1]["content"] = _render_steps(steps, streaming_buf)
    else:
        history[-1]["content"] = (_render_steps(steps)
                                   + '\n\n<span style="color:#9ca3af;font-size:12px;font-style:italic;">'
                                   '(Charles 没有返回最终文本回复, 请展开上方工具结果查看)</span>')
    yield _commit()


def new_session():
    """主动开新对话: 换 Charles session, 同时清掉 UI 缓存。"""
    _CHARLES_CACHE["thread_id"] = f"ui:{datetime.now().strftime('%H%M%S')}"
    _CHARLES_CACHE["history"] = []
    return [], []


# ============================================================
# Tab UI
# ============================================================

def build_tab():
    with gr.Row(equal_height=False):
        # ===== 左侧 (scale=4): 主对话区 =====
        with gr.Column(scale=4):
            chatbot = gr.Chatbot(
                label=None,
                height=600,
                show_copy_button=True,
                avatar_images=(None, None),
                type="messages",
                show_label=False,
                render_markdown=True,
                sanitize_html=False,
                elem_classes=["qa-chatbot"],
                placeholder="<div style='text-align:center;color:#aaa;padding:60px;'>"
                            "<div style='font-size:22px;margin-bottom:8px;'>跟 Charles 聊聊吧</div>"
                            "<div style='font-size:13px;'>右侧有推荐对话, 点一下就能开聊</div>"
                            "</div>",
            )

            with gr.Row():
                msg_input = gr.Textbox(
                    placeholder="例: 帮我用国泰君安五步法分析 600519 ... (Enter 发送, Shift+Enter 换行)",
                    lines=2, scale=8, show_label=False, container=False,
                )
                send_btn = gr.Button("发送", variant="primary", scale=1, min_width=80)

            with gr.Row():
                new_session_btn = gr.Button("新对话", variant="secondary", size="sm")
                gr.Markdown(
                    "<span style='color:#888;font-size:11px;'>切页或 F5 都会保留当前对话; "
                    "点「新对话」才清空并换一套上下文</span>",
                    container=False,
                )

        # ===== 右侧 (scale=1): Cover + 插件 + 推荐对话 =====
        with gr.Column(scale=1, min_width=260):
            gr.HTML(CHARLES_COVER_HTML)
            gr.HTML(PLUGINS_HTML)

            with gr.Accordion("推荐对话", open=True):
                with gr.Column(elem_classes=["qa-no-label"]):
                    gr.Examples(
                        examples=PROMPT_SUGGESTIONS,
                        inputs=[msg_input],
                        label="",
                    )

            with gr.Accordion("使用提示", open=False):
                gr.Markdown("""
- **首次提问** 30-90 秒 (加载 nanobot + skills + 多轮 LLM 调用)
- **思考过程** 实时显示在回复气泡内, 工具调用/返回是配对折叠的, 点开看详情
- **切页 / F5** 对话留在浏览器里, 刷新不会丢; 点「新对话」才清空
- **新对话** 切换 session_key, 完全隔离上下文
- **底层**: nanobot 框架 + qwen 模型 + Charles 投研 skills (五步法 / 估值 / 行业对比)
""")

    # F5 后 Gradio iframe 会重建, 对话存在浏览器 localStorage
    stored = gr.BrowserState([], storage_key="wucai-charles-chat")

    msg_input.submit(chat_with_charles_stream,
                     inputs=[msg_input, chatbot],
                     outputs=[chatbot, msg_input])
    send_btn.click(chat_with_charles_stream,
                   inputs=[msg_input, chatbot],
                   outputs=[chatbot, msg_input])
    new_session_btn.click(new_session, outputs=[chatbot, stored])
    chatbot.change(persist_ui_history, inputs=chatbot, outputs=stored)

    root = gr.context.Context.root_block
    if root is not None:
        root.load(
            restore_ui_history,
            inputs=stored,
            outputs=chatbot,
            show_progress="hidden",
            api_name=False,
        )
