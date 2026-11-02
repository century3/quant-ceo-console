# -*- coding: utf-8 -*-
"""模拟盘持仓的最新新闻和关键词舆情事件。

A 股新闻走投研技能里的东方财富抓取。美股走 Yahoo 头条。
事件分类和交易信号沿用 sentiment-analysis 技能的关键词表，不调用大模型。
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.request import Request, urlopen

from lib.paths import OUTPUTS_LIVE_STATE

_SKILL_DIR = (
    Path(__file__).resolve().parents[1]
    / "third_party" / "charles_bundle" / "charles-nanobot"
    / "skills" / "sentiment-analysis" / "scripts"
)
_NEWS_LIMIT = 8

# 与技能里中文类别对齐，用来扫英文标题
_EN_KEYWORDS = {
    "利好": {
        "回购增持": ["buyback", "share repurchase", "repurchase program"],
        "业绩预增": ["beats estimates", "raises guidance", "profit rose", "revenue growth", "tops estimates"],
        "大额订单": ["awarded", "contract win", "strategic partnership", "partnership"],
        "分红送转": ["dividend"],
    },
    "利空": {
        "业绩预减": ["misses estimates", "cuts guidance", "profit fell", "revenue decline", "net loss"],
        "违规处罚": ["investigation", "sec charges", "penalty", "regulatory fine"],
        "股东减持": ["insider selling", "secondary offering"],
        "诉讼仲裁": ["lawsuit", "litigation", "sued"],
        "退市风险": ["delisting", "delisted"],
    },
    "政策": {
        "监管政策": ["new regulation", "regulatory"],
    },
}


def _load_skill(name: str):
    path = _SKILL_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"holdings_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def held_positions() -> list[dict]:
    """当前模拟盘里股数大于 0 的持仓。没有运行状态时读 mock 配置。"""
    rows = []
    if OUTPUTS_LIVE_STATE.is_file():
        try:
            state = json.loads(OUTPUTS_LIVE_STATE.read_text(encoding="utf-8"))
            rows = list(state.get("positions") or [])
        except Exception:
            rows = []
    if not rows:
        from lib.live_simulator import load_mock_config
        rows = list(load_mock_config().get("positions") or [])
    out = []
    for row in rows:
        code = str(row.get("code") or "").strip().upper()
        volume = int(row.get("volume") or 0)
        if not code or volume <= 0:
            continue
        out.append({
            "code": code,
            "name": str(row.get("name") or code),
            "volume": volume,
        })
    return out


def _records_from_frame(df) -> list[dict]:
    import pandas as pd
    records = df.to_dict(orient="records")
    out = []
    for record in records:
        clean = {}
        for key, value in record.items():
            if isinstance(value, pd.Timestamp):
                clean[key] = value.strftime("%Y-%m-%d %H:%M:%S")
            elif pd.isna(value):
                clean[key] = ""
            else:
                clean[key] = value
        out.append(clean)
    return out


def _normalize_news(record: dict) -> dict:
    title = str(record.get("新闻标题") or record.get("title") or "").strip()
    content = str(record.get("新闻内容") or record.get("content") or "").strip()
    published = str(record.get("发布时间") or record.get("time") or "").strip()
    source = str(record.get("文章来源") or record.get("source") or "").strip()
    url = str(record.get("新闻链接") or record.get("url") or "").strip()
    summary = content.replace("\n", " ").strip()
    if len(summary) > 140:
        summary = summary[:140] + "..."
    return {
        "title": title,
        "time": published,
        "source": source,
        "url": url,
        "summary": summary,
        "text": f"{title} {content}".strip(),
    }


def _fetch_a_share(code: str, days: int) -> list[dict]:
    fetcher = _load_skill("news_fetcher")
    symbol = code.split(".")[0]
    frame = fetcher.fetch_stock_news(symbol)
    if frame is None or frame.empty:
        return []
    frame = fetcher.filter_by_date(frame, days)
    frame = fetcher.deduplicate(frame)
    return [_normalize_news(row) for row in _records_from_frame(frame)]


def _fetch_us(code: str, days: int) -> list[dict]:
    ticker = code[:-3] if code.endswith(".US") else code
    url = f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={ticker}&region=US&lang=en-US"
    request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(request, timeout=20) as response:
        payload = response.read()
    root = ET.fromstring(payload)
    cutoff = datetime.now().astimezone() - timedelta(days=days)
    rows = []
    for item in root.findall("./channel/item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        published_raw = (item.findtext("pubDate") or "").strip()
        description = _plain(item.findtext("description") or "")
        published = ""
        if published_raw:
            try:
                stamp = parsedate_to_datetime(published_raw)
                if stamp.tzinfo is None:
                    stamp = stamp.astimezone()
                if stamp < cutoff:
                    continue
                published = stamp.astimezone().strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                published = published_raw
        if not title:
            continue
        rows.append(_normalize_news({
            "title": title,
            "content": description,
            "time": published,
            "source": "Yahoo",
            "url": link,
        }))
    return rows


def _plain(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", text).strip()


def _translate_us_news(rows: list[dict]) -> None:
    """把美股标题和摘要译成简体中文，并留下英文原标题给事件匹配。"""
    if not rows:
        return
    api_key = os.environ.get("DASHSCOPE_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("未设置 DASHSCOPE_API_KEY，无法把美股新闻译成中文")

    import httpx
    from openai import OpenAI

    payload = [
        {"i": index, "title": row.get("title") or "", "summary": row.get("summary") or ""}
        for index, row in enumerate(rows)
    ]
    # 本机代理会把通义请求转丢，直连 dashscope
    client = OpenAI(
        api_key=api_key,
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        http_client=httpx.Client(trust_env=False, timeout=60),
    )
    response = client.chat.completions.create(
        model="qwen3.8-flash",
        temperature=0.1,
        timeout=40,
        extra_body={"enable_thinking": False},
        messages=[{
            "role": "user",
            "content": (
                "把下面 JSON 里的美股新闻标题和摘要译成简体中文。"
                "公司名、产品名、股票代码可以保留英文。"
                "只返回 JSON 数组，每项含 i、title、summary，不要解释。\n"
                + json.dumps(payload, ensure_ascii=False)
            ),
        }],
    )
    content = (response.choices[0].message.content or "").strip()
    if content.startswith("```"):
        content = content.split("```", 2)[1]
        if content.startswith("json"):
            content = content[4:]
        content = content.strip()
    start = content.find("[")
    end = content.rfind("]")
    if start < 0 or end < start:
        raise RuntimeError("美股新闻翻译没有返回 JSON 数组")
    translated = json.loads(content[start:end + 1])
    by_index = {int(item["i"]): item for item in translated}
    if set(by_index) != set(range(len(rows))):
        raise RuntimeError("美股新闻翻译条数和原文不一致")
    for index, row in enumerate(rows):
        item = by_index[index]
        title = str(item.get("title") or "").strip()
        summary = str(item.get("summary") or "").strip()
        if not title:
            raise RuntimeError(f"第 {index} 条美股新闻没有中文标题")
        row["title_en"] = row.get("title") or ""
        row["title"] = title
        row["summary"] = summary


def _keyword_stands(text: str, keyword: str) -> bool:
    """「不减持」「未亏损」里的词不算命中。"""
    start = 0
    while True:
        index = text.find(keyword, start)
        if index < 0:
            return False
        prev = text[index - 1] if index else ""
        if prev not in ("不", "未"):
            return True
        start = index + len(keyword)


def _events_for_news(news: dict) -> list[dict]:
    detector = _load_skill("event_detector")
    # 只看标题。正文里顺带出现的「亏损」「减持」会把好消息标成利空。
    text = news.get("title") or ""
    english = news.get("title_en") or ""
    found = []
    for item in detector.keyword_based_detection(text):
        matched = [word for word in item.get("matched_keywords") or [] if _keyword_stands(text, word)]
        if not matched:
            continue
        item = dict(item)
        item["matched_keywords"] = matched
        found.append(item)
    lowered = (english or text).lower()
    for event_type, categories in _EN_KEYWORDS.items():
        for category, keywords in categories.items():
            matched = [word for word in keywords if word in lowered]
            if not matched:
                continue
            if any(item["event_type"] == event_type and item["event_category"] == category for item in found):
                continue
            found.append({
                "event_type": event_type,
                "event_category": category,
                "matched_keywords": matched,
            })
    events = []
    for item in found:
        signal = detector.generate_signal(item["event_type"], item["event_category"])
        events.append({
            "event_type": item["event_type"],
            "event_category": item["event_category"],
            "matched_keywords": item.get("matched_keywords") or [],
            "signal": signal.get("signal") or "",
            "signal_reason": signal.get("reason") or "",
            "title": news.get("title") or "",
            "time": news.get("time") or "",
            "url": news.get("url") or "",
        })
    return events


def collect_holding(position: dict, days: int) -> dict:
    """抓一只持仓的新闻，并标出命中的舆情事件。"""
    code = position["code"]
    item = {
        "code": code,
        "name": position.get("name") or code,
        "volume": position.get("volume") or 0,
        "news": [],
        "events": [],
        "error": "",
    }
    try:
        if code.endswith(".US"):
            news = _fetch_us(code, days)
            _translate_us_news(news)
        else:
            news = _fetch_a_share(code, days)
    except Exception as exc:
        item["error"] = f"{type(exc).__name__}: {exc}"
        return item
    news = [row for row in news if row.get("title")][:_NEWS_LIMIT]
    events = []
    for row in news:
        events.extend(_events_for_news(row))
        row.pop("text", None)
        row.pop("title_en", None)
    item["news"] = news
    item["events"] = events
    return item


def collect_holdings(days: int = 7, progress=None) -> dict:
    """按当前模拟盘持仓逐只抓新闻。progress(item, index, total) 在每只完成后回调。"""
    days = max(1, min(int(days or 7), 30))
    positions = held_positions()
    holdings = []
    total = len(positions)
    for index, position in enumerate(positions, start=1):
        item = collect_holding(position, days)
        holdings.append(item)
        if progress:
            progress(item, index, total)
    return {
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        "days": days,
        "holdings": holdings,
    }
