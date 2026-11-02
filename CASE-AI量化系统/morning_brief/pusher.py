# -*- coding: utf-8 -*-
# Author：@跟陈博士学AI
# 钉钉 / 企业微信推送（晨会内嵌）
from __future__ import annotations
import json
import logging
import os
import urllib.request
from typing import Optional

log = logging.getLogger("pusher")


def push_dingtalk(title: str, content: str,
                  webhook: Optional[str] = None) -> bool:
    """钉钉自定义机器人 markdown 推送"""
    webhook = webhook or os.environ.get("DINGTALK_WEBHOOK", "")
    if not webhook:
        log.warning("[PUSH] DINGTALK_WEBHOOK 未配置, 跳过钉钉推送")
        return False
    payload = {
        "msgtype": "markdown",
        "markdown": {"title": title, "text": content},
    }
    try:
        req = urllib.request.Request(
            webhook, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            ret = json.loads(r.read())
            ok = ret.get("errcode") == 0
            if ok:
                log.info(f"[PUSH] 钉钉推送成功: {title}")
            else:
                log.error(f"[PUSH] 钉钉推送失败: {ret}")
            return ok
    except Exception as e:
        log.error(f"[PUSH] 钉钉推送异常: {e}")
        return False


def push_wecom(title: str, content: str,
               webhook: Optional[str] = None) -> bool:
    """企业微信群机器人 markdown 推送"""
    webhook = webhook or os.environ.get("WECOM_WEBHOOK", "")
    if not webhook:
        log.warning("[PUSH] WECOM_WEBHOOK 未配置, 跳过企业微信推送")
        return False
    payload = {
        "msgtype": "markdown",
        "markdown": {"content": f"# {title}\n\n{content}"},
    }
    try:
        req = urllib.request.Request(
            webhook, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            ret = json.loads(r.read())
            ok = ret.get("errcode") == 0
            if ok:
                log.info(f"[PUSH] 企业微信推送成功: {title}")
            else:
                log.error(f"[PUSH] 企业微信推送失败: {ret}")
            return ok
    except Exception as e:
        log.error(f"[PUSH] 企业微信推送异常: {e}")
        return False


def push_console(title: str, content: str) -> bool:
    """控制台打印 -- 总是可用, 没配 webhook 时的兜底"""
    print()
    print("=" * 70)
    print(f"  [推送] {title}")
    print("=" * 70)
    print(content)
    print("=" * 70)
    return True


def push_all(title: str, content: str) -> dict:
    """同时尝试所有渠道"""
    return {
        "console":  push_console(title, content),
        "dingtalk": push_dingtalk(title, content),
        "wecom":    push_wecom(title, content),
    }
