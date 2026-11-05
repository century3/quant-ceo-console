# -*- coding: utf-8 -*-
# Author：@跟陈博士学AI
# 晨会分析路由 -- REST + SSE
"""
GET  /api/morning/cache         -- 读最近一次缓存
GET  /api/morning/stream?...    -- SSE 流式跑工作流, 推送进度
"""

from __future__ import annotations
import asyncio
import importlib
import json
import queue
import sys
import threading
import time
from datetime import datetime

from fastapi import APIRouter, Query
from sse_starlette.sse import EventSourceResponse

from lib.paths import setup_sys_path, OUTPUTS_DIR, PROJECT_ROOT
setup_sys_path()

# 晨会分析直接跑 21 目录的最新工作流，不再用本项目里的 morning_brief 副本
CASE_D_DIR = (
    PROJECT_ROOT.parents[1]
    / "21-投资晨会-人机协作"
    / "CASE-D-投资晨会工作流"
)

router = APIRouter()

# 缓存目录
CACHE_DIR = OUTPUTS_DIR.parent / "data" / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)


# ------------------------------------------------------------
def _cache_file(market: str):
    return CACHE_DIR / f"morning_latest_{market}.json"


def _load_latest_cache(market: str) -> dict:
    fp = _cache_file(market)
    if not fp.exists():
        return {}
    try:
        return json.loads(fp.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_cache(state: dict):
    market = state.get("market") or "cn"
    cache = {
        "saved_at":      datetime.now().isoformat(timespec="seconds"),
        "market":        market,
        "industry_rank": state.get("industry_rank", []),
        "picked_stocks": state.get("picked_stocks", []),
        "messages":      state.get("messages", []),
        "report_html":   state.get("report_html", ""),
    }
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    (CACHE_DIR / f"morning_{market}_{ts}.json").write_text(
        json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    _cache_file(market).write_text(
        json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


# ------------------------------------------------------------
@router.get("/cache")
def get_cache(market: str = Query("cn")):
    """读最近一次晨会缓存。cn 与 us 分开存放。"""
    if market not in ("cn", "us"):
        return {"error": "market 只能是 cn 或 us"}
    cache = _load_latest_cache(market)
    if not cache:
        label = "美股" if market == "us" else "A股"
        return {"error": f"暂无{label}缓存. 先点 '一键触发晨会' 跑一次"}
    return cache


# ------------------------------------------------------------
NODE_LABELS = {
    "industry":     "[1/4] industry -- 读库算板块强度与拐点",
    "stock_picker": "[2/4] stock_picker -- Top 板块成分股多因子（读日K表）",
    "report":       "[3/4] report -- 拼装晨报 HTML",
    "push":         "[4/4] push -- 推送到钉钉/微信",
}
NODE_EST = {
    "industry":     "读 MySQL 板块/指数表并排名（通常较快，视数据量）",
    "stock_picker": "读个股日 K 算因子（取决于候选只数）",
    "report":       "~1 秒",
    "push":         "~1 秒",
}
NODE_ORDER = ["industry", "stock_picker", "report", "push"]


class _SafeStream:
    """控制台窗口关掉后，print 会抛 OSError 22。晨会日志写失败时丢掉，不中断计算。"""

    def __init__(self, raw):
        self._raw = raw

    def write(self, data):
        try:
            return self._raw.write(data)
        except OSError:
            return 0

    def flush(self):
        try:
            self._raw.flush()
        except OSError:
            return None

    def __getattr__(self, name):
        return getattr(self._raw, name)


def _prepend_case_d() -> None:
    """让 import graph / graph_us 命中 21 目录，而不是别的同名模块。"""
    if not (CASE_D_DIR / "graph.py").is_file():
        raise FileNotFoundError(f"找不到晨会代码: {CASE_D_DIR}")
    for folder in (CASE_D_DIR, CASE_D_DIR / "lib"):
        folder_s = str(folder)
        if folder_s in sys.path:
            sys.path.remove(folder_s)
        sys.path.insert(0, folder_s)


@router.get("/stream")
async def stream(
    top_industries: int = Query(3),
    top_stocks: int = Query(5),
    sample_per_industry: int = Query(15),
    lookback: int = Query(90),
    market: str = Query("cn"),
):
    """SSE 流式触发晨会, 边跑边推中间状态"""
    if market not in ("cn", "us"):
        async def _bad():
            yield {
                "event": "error_event",
                "data": json.dumps({"error": "market 只能是 cn 或 us"}),
            }
        return EventSourceResponse(_bad())

    initial_state = {
        "trigger_time":     datetime.now().isoformat(timespec="seconds"),
        "top_n_industries": top_industries,
        "top_n_stocks":     top_stocks,
        "sample_stocks":    sample_per_industry,
        "lookback_days":    lookback,
        "market":           market,
        "messages":         [],
    }

    accumulated_state = dict(initial_state)
    accumulated_state["messages"] = []
    chunk_queue: "queue.Queue" = queue.Queue()

    def worker():
        # graph.py 会把 CASE-D 插到 sys.path 最前。跑完恢复，避免盖住龙头策略的 db_config
        saved_path = list(sys.path)
        saved_out, saved_err = sys.stdout, sys.stderr
        sys.stdout = _SafeStream(saved_out)
        sys.stderr = _SafeStream(saved_err)
        try:
            _prepend_case_d()
            mod_name = "graph_us" if market == "us" else "graph"
            graph = importlib.import_module(mod_name).build_graph()
            for chunk in graph.stream(initial_state, stream_mode="updates"):
                chunk_queue.put(("update", chunk))
            chunk_queue.put(("done", None))
        except Exception as e:
            chunk_queue.put(("error", e))
        finally:
            sys.stdout = saved_out
            sys.stderr = saved_err
            sys.path[:] = saved_path

    th = threading.Thread(target=worker, daemon=True)
    th.start()

    async def event_gen():
        current_node = NODE_ORDER[0]
        started_at = time.time()

        # 立刻发一次
        yield {
            "event": "progress",
            "data": json.dumps({
                "current_node": current_node,
                "estimate":     NODE_EST.get(current_node, ""),
                "message":      "准备启动 4 节点工作流",
            }),
        }

        last_progress_at = time.time()
        while True:
            # 拿一条 chunk (不阻塞太久, 1 秒内即返回)
            try:
                kind, payload = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: chunk_queue.get(timeout=1.0)
                )
            except queue.Empty:
                kind, payload = None, None

            if kind == "update":
                chunk = payload
                node_name = list(chunk.keys())[0]
                delta = chunk[node_name]

                # 累积
                for k, v in delta.items():
                    if k == "messages" and isinstance(v, list):
                        accumulated_state.setdefault("messages", []).extend(v)
                    else:
                        accumulated_state[k] = v

                # 推断下一个节点
                if node_name in NODE_ORDER:
                    idx = NODE_ORDER.index(node_name)
                    if idx + 1 < len(NODE_ORDER):
                        current_node = NODE_ORDER[idx + 1]

                yield {
                    "event": "node_done",
                    "data": json.dumps({
                        "node":          node_name,
                        "node_label":    NODE_LABELS.get(node_name, node_name),
                        "industry_rank": accumulated_state.get("industry_rank", []),
                        "picked_stocks": accumulated_state.get("picked_stocks", []),
                        "messages":      accumulated_state.get("messages", []),
                    }),
                }
                last_progress_at = time.time()
                continue

            if kind == "done":
                # 落盘缓存
                try:
                    _save_cache(accumulated_state)
                except Exception:
                    pass
                yield {
                    "event": "done",
                    "data": json.dumps({
                        "industry_rank": accumulated_state.get("industry_rank", []),
                        "picked_stocks": accumulated_state.get("picked_stocks", []),
                        "messages":      accumulated_state.get("messages", []),
                        "report_html":   accumulated_state.get("report_html", ""),
                    }),
                }
                break

            if kind == "error":
                yield {
                    "event": "error_event",
                    "data": json.dumps({
                        "error": f"{type(payload).__name__}: {payload}",
                    }),
                }
                break

            # 心跳 (每 1.5 秒)
            now = time.time()
            if now - last_progress_at > 1.5:
                last_progress_at = now
                yield {
                    "event": "progress",
                    "data": json.dumps({
                        "current_node": current_node,
                        "estimate":     NODE_EST.get(current_node, ""),
                        "message":      f"已运行 {now - started_at:.1f} 秒",
                    }),
                }

    return EventSourceResponse(event_gen())
