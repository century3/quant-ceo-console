# -*- coding: utf-8 -*-
"""持仓舆情路由。读模拟盘持仓，抓新闻并做关键词事件识别。"""
from __future__ import annotations

import asyncio
import json
import queue
import threading
from datetime import datetime

from fastapi import APIRouter, Query
from sse_starlette.sse import EventSourceResponse

from lib.paths import OUTPUTS_DIR, setup_sys_path

setup_sys_path()

router = APIRouter()

CACHE_DIR = OUTPUTS_DIR.parent / "data" / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
_CACHE_FILE = CACHE_DIR / "holdings_news_latest.json"


def _load_cache() -> dict:
    if not _CACHE_FILE.is_file():
        return {}
    try:
        return json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_cache(payload: dict) -> None:
    _CACHE_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


@router.get("/cache")
def get_cache():
    cache = _load_cache()
    if not cache:
        return {"error": "暂无缓存。先点「刷新持仓舆情」"}
    return cache


@router.get("/stream")
async def stream(days: int = Query(7)):
    """逐只推送进度，结束后写入缓存。"""
    days = max(1, min(int(days or 7), 30))
    chunk_queue: "queue.Queue" = queue.Queue()

    def worker():
        try:
            from lib.holdings_news import collect_holdings

            def progress(item, index, total):
                chunk_queue.put(("stock", {
                    "index": index,
                    "total": total,
                    "holding": item,
                }))

            payload = collect_holdings(days, progress=progress)
            chunk_queue.put(("done", payload))
        except Exception as exc:
            chunk_queue.put(("error", exc))

    threading.Thread(target=worker, daemon=True).start()

    async def event_gen():
        yield {
            "event": "progress",
            "data": json.dumps({
                "message": "开始读取模拟盘持仓",
                "saved_at": datetime.now().isoformat(timespec="seconds"),
            }),
        }
        while True:
            try:
                kind, payload = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: chunk_queue.get(timeout=1.0)
                )
            except queue.Empty:
                yield {
                    "event": "progress",
                    "data": json.dumps({"message": "正在抓取新闻"}),
                }
                continue
            if kind == "stock":
                yield {"event": "stock", "data": json.dumps(payload, ensure_ascii=False)}
                continue
            if kind == "done":
                _save_cache(payload)
                yield {"event": "done", "data": json.dumps(payload, ensure_ascii=False)}
                break
            yield {
                "event": "error_event",
                "data": json.dumps({"error": f"{type(payload).__name__}: {payload}"}),
            }
            break

    return EventSourceResponse(event_gen())
