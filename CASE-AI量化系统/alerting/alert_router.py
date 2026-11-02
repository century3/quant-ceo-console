# -*- coding: utf-8 -*-
# Author：@跟陈博士学AI
# 23-CASE-D 告警分级路由
"""
AlertRouter -- 告警分级路由

    INFO     控制台 + 钉钉聚合 (默认 30 分钟; 主循环传 300 秒)
    WARN     控制台 + 钉钉实时 (路由器已接, 主循环未调用)
    CRITICAL 控制台 + 钉钉 + 企微 + 邮件, 钉钉 markdown @all
    FATAL    同 CRITICAL, 失败重试 3 次 (路由器已接, 主循环未调用)

未配 DINGTALK_WEBHOOK / WECOM_WEBHOOK / SMTP_* 时只打控制台. 无短信渠道.
"""

from __future__ import annotations
import json
import logging
import os
import smtplib
import time
import threading
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from email.mime.text import MIMEText
from email.utils import formataddr
from enum import IntEnum
from typing import List, Optional

log = logging.getLogger("alert-router")


# ============================================================
# 告警级别
# ============================================================

class AlertLevel(IntEnum):
    INFO = 0       # 一般信息
    WARN = 1       # 警告 (需关注但不紧急)
    CRITICAL = 2   # 紧急 (立即处理)
    FATAL = 3      # 系统级故障


@dataclass
class AlertEvent:
    level: AlertLevel
    title: str
    message: str
    source: str = ""           # 来自哪个节点 (charles/zoe/kris/trader)
    timestamp: datetime = field(default_factory=datetime.now)

    def fmt(self) -> str:
        return (f"[{self.level.name}] {self.timestamp.strftime('%H:%M:%S')} "
                f"{('@' + self.source) if self.source else ''}: {self.title}\n  {self.message}")


# ============================================================
# 各渠道实现
# ============================================================

class ConsoleChannel:
    """控制台 -- 总是可用"""
    @staticmethod
    def send(event: AlertEvent) -> bool:
        prefix = {"INFO": "[INFO]", "WARN": "[WARN]", "CRITICAL": "[CRIT]", "FATAL": "[FATAL]"}
        print(f"{prefix.get(event.level.name, '[?]')} {event.fmt()}")
        return True


class DingTalkChannel:
    """钉钉自定义机器人"""
    def __init__(self, webhook: Optional[str] = None):
        self.webhook = webhook or os.environ.get("DINGTALK_WEBHOOK", "")

    def send(self, event: AlertEvent) -> bool:
        if not self.webhook:
            return False
        # CRITICAL 及以上用 markdown 加粗 + @所有人
        if event.level >= AlertLevel.CRITICAL:
            payload = {
                "msgtype": "markdown",
                "markdown": {
                    "title": event.title,
                    "text": (f"# **[{event.level.name}]** {event.title}\n\n"
                             f"{event.message}\n\n"
                             f"@all"),
                },
                "at": {"isAtAll": True},
            }
        else:
            payload = {
                "msgtype": "text",
                "text": {"content": f"[{event.level.name}] {event.title}\n{event.message}"},
            }
        try:
            req = urllib.request.Request(
                self.webhook, data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=5) as r:
                return json.loads(r.read()).get("errcode") == 0
        except Exception as e:
            log.error(f"[DingTalk] {e}")
            return False


class WeComChannel:
    """企业微信群机器人"""
    def __init__(self, webhook: Optional[str] = None):
        self.webhook = webhook or os.environ.get("WECOM_WEBHOOK", "")

    def send(self, event: AlertEvent) -> bool:
        if not self.webhook:
            return False
        prefix = "**" if event.level >= AlertLevel.CRITICAL else ""
        payload = {
            "msgtype": "markdown",
            "markdown": {
                "content": f"# {prefix}[{event.level.name}]{prefix} {event.title}\n\n{event.message}",
            },
        }
        try:
            req = urllib.request.Request(
                self.webhook, data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=5) as r:
                return json.loads(r.read()).get("errcode") == 0
        except Exception as e:
            log.error(f"[WeCom] {e}")
            return False


class EmailChannel:
    """邮件 -- 用 QQ/163 SMTP, 高级别告警必备"""
    def __init__(self, host: Optional[str] = None, port: Optional[int] = None,
                 user: Optional[str] = None, password: Optional[str] = None,
                 to_addr: Optional[str] = None):
        self.host = host or os.environ.get("SMTP_HOST", "")
        self.port = int(port or os.environ.get("SMTP_PORT", 465))
        self.user = user or os.environ.get("SMTP_USER", "")
        self.password = password or os.environ.get("SMTP_PASSWORD", "")
        self.to_addr = to_addr or os.environ.get("ALERT_EMAIL_TO", "")

    def send(self, event: AlertEvent) -> bool:
        if not self.host or not self.user or not self.password or not self.to_addr:
            return False
        try:
            msg = MIMEText(
                f"<h2>[{event.level.name}] {event.title}</h2>"
                f"<p><b>时间:</b> {event.timestamp.strftime('%Y-%m-%d %H:%M:%S')}</p>"
                f"<p><b>来源:</b> {event.source or '系统'}</p>"
                f"<p><b>详情:</b></p><pre>{event.message}</pre>",
                "html", "utf-8",
            )
            msg["From"] = formataddr(("AI 量化告警", self.user))
            msg["To"] = self.to_addr
            msg["Subject"] = f"[{event.level.name}] {event.title}"

            with smtplib.SMTP_SSL(self.host, self.port, timeout=10) as smtp:
                smtp.login(self.user, self.password)
                smtp.sendmail(self.user, [self.to_addr], msg.as_string())
            return True
        except Exception as e:
            log.error(f"[Email] {e}")
            return False


# ============================================================
# 主路由器
# ============================================================

class AlertRouter:
    """告警分级路由器. INFO 入队聚合; WARN 及以上实时; 未配 webhook 只打控制台."""

    def __init__(self,
                 ding_webhook: Optional[str] = None,
                 wecom_webhook: Optional[str] = None,
                 enable_email: bool = True,
                 info_aggregate_seconds: int = 1800):
        self.console = ConsoleChannel()
        self.dingtalk = DingTalkChannel(ding_webhook)
        self.wecom = WeComChannel(wecom_webhook)
        self.email = EmailChannel() if enable_email else None

        # INFO 聚合队列
        self.info_queue: deque = deque()
        self.info_queue_lock = threading.Lock()
        self.info_aggregate_seconds = info_aggregate_seconds
        self._aggregator_stop = threading.Event()
        self._aggregator_thread = threading.Thread(target=self._aggregator_loop,
                                                    daemon=True, name="alert-aggregator")
        self._aggregator_thread.start()

        # 统计
        self.stats = {"INFO": 0, "WARN": 0, "CRITICAL": 0, "FATAL": 0}

    def alert(self, level: str, title: str, message: str = "",
              source: str = "") -> dict:
        """主入口"""
        try:
            level_enum = AlertLevel[level.upper()] if isinstance(level, str) else level
        except KeyError:
            level_enum = AlertLevel.INFO

        event = AlertEvent(level=level_enum, title=title, message=message, source=source)
        self.stats[event.level.name] += 1

        # 控制台 -- 所有级别都打
        self.console.send(event)

        result = {"console": True}

        if level_enum == AlertLevel.INFO:
            # INFO 入队列, 不立即推
            with self.info_queue_lock:
                self.info_queue.append(event)
            return result

        # WARN 及以上立即推
        result["dingtalk"] = self.dingtalk.send(event)

        if level_enum >= AlertLevel.CRITICAL:
            result["wecom"] = self.wecom.send(event)
            if self.email:
                result["email"] = self.email.send(event)

        if level_enum == AlertLevel.FATAL:
            # FATAL 失败重试 3 次
            for ch_name in ("dingtalk", "wecom", "email"):
                if not result.get(ch_name):
                    for _ in range(3):
                        time.sleep(1)
                        if ch_name == "dingtalk" and self.dingtalk.send(event):
                            result[ch_name] = True
                            break
                        elif ch_name == "wecom" and self.wecom.send(event):
                            result[ch_name] = True
                            break

        return result

    # ----- INFO 聚合 -----
    def _aggregator_loop(self):
        while not self._aggregator_stop.wait(self.info_aggregate_seconds):
            self._flush_info_batch()

    def _flush_info_batch(self):
        with self.info_queue_lock:
            if not self.info_queue:
                return
            events = list(self.info_queue)
            self.info_queue.clear()

        # 拼成一条聚合消息
        n = len(events)
        title = f"过去 {self.info_aggregate_seconds // 60} 分钟事件汇总 ({n} 条)"
        lines = [f"- {e.timestamp.strftime('%H:%M:%S')} {e.title}" for e in events[-20:]]
        message = "\n".join(lines)
        if n > 20:
            message = f"(只显示最新 20 条, 总计 {n} 条)\n" + message

        agg_event = AlertEvent(
            level=AlertLevel.INFO,
            title=title, message=message, source="aggregator",
        )
        self.dingtalk.send(agg_event)

    def flush_now(self):
        """手动 flush (调试用)"""
        self._flush_info_batch()

    def shutdown(self):
        """优雅退出 (把队列里剩下的推完)"""
        self._aggregator_stop.set()
        self._flush_info_batch()
        if self._aggregator_thread.is_alive():
            self._aggregator_thread.join(timeout=2)


# ============================================================
# 单例 (推荐用法)
# ============================================================

_default_router: Optional[AlertRouter] = None


def get_router() -> AlertRouter:
    global _default_router
    if _default_router is None:
        _default_router = AlertRouter()
    return _default_router


def alert(level: str, title: str, message: str = "", source: str = "") -> dict:
    """快捷函数"""
    return get_router().alert(level, title, message, source)


# ============================================================
# Demo
# ============================================================

def demo():
    print("\n" + "=" * 70)
    print("  CASE-23D 告警分级路由 demo")
    print("=" * 70)

    router = AlertRouter(info_aggregate_seconds=3)   # demo 用 3 秒聚合一次

    # 模拟一个完整盘中场景
    print("\n--- 模拟一段盘中事件 (8 条不同级别) ---\n")

    test_events = [
        ("INFO",     "Zoe 信号触发", "MACD 金叉 -> BUY 600519.SH", "zoe"),
        ("INFO",     "持仓更新", "买入 100 股 600519.SH @ 1407.24", "trader"),
        ("WARN",     "风控降仓", "Kris 触发 ATR 仓位上限, 建议降至 70%", "kris"),
        ("INFO",     "信号触发", "MACD 金叉 -> BUY 002594.SZ", "zoe"),
        ("CRITICAL", "单日亏损告警", "今日累计亏损 -1.8%, 接近熔断线 -2.0%", "kris"),
        ("INFO",     "信号触发", "RSI 超卖 -> BUY 513100.SH", "zoe"),
        ("FATAL",    "QMT 连接断开", "心跳失败 5 次, 已尝试重连 3 次均失败", "trader"),
        ("INFO",     "策略状态", "暂停所有买入, 等待人工恢复", "system"),
    ]

    for level, title, msg, source in test_events:
        result = router.alert(level, title, msg, source)
        print(f"  -> 推送结果: {result}\n")
        time.sleep(0.5)

    # 等聚合周期
    print("\n--- 等待 5 秒, 让 INFO 聚合推送 ---")
    time.sleep(5)

    print(f"\n[统计]")
    for level, count in router.stats.items():
        print(f"  {level:<10s}: {count} 条")

    router.shutdown()
    print("\n[OK] Demo 完成")


if __name__ == "__main__":
    demo()
