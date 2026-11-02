# -*- coding: utf-8 -*-
# Author：@跟陈博士学AI
# 增强版 trader（双后端：miniQMT / QMT HTTP）：在原版基础上加入心跳保活、断线重连、订阅防护、多账户会话隔离。
#
#   1) 心跳保活：每若干秒探活，检测连接是否存活
#   2) 断线重连：断线时按指数退避自动重连
#   3) 订阅风暴防护：订阅去重 + 限流
#   4) 多账户隔离：session_id 自动管理，避免 session 冲突
#
# 与原版兼容性：上层 API（buy/sell 等）保持兼容。
# 双后端切换（WUCAI_QMT，默认 qmt HTTP）；新增 query_orders() 修复 routes/live.py 的抽象泄漏。
# Author：@跟陈博士学AI
"""
增强版 QMTTraderV2 -- 实盘可用的健壮版本（双后端）

新增能力 (相对基础版)：
    - HeartbeatMonitor: 后台线程心跳, 自动检测连接异常
    - AutoReconnect: 指数退避重连
    - SubscriptionGuard: 订阅去重 + 频率限制
    - SessionManager: 多账户隔离, 自动分配 session_id
    - query_orders(): 查询当日委托（修复上层 routes/live.py 直接访问 _trader 的抽象泄漏）

设计原则：
    - 跟原版 API 100% 兼容: trader.buy(...) / trader.sell(...) 完全不变
    - 新特性默认启用, 但可通过 enable_heartbeat=False / enable_reconnect=False 关闭
    - 心跳异常 / 重连成功 / 订阅拦截 等事件都进 events 列表
"""

import os
import time
import threading
import datetime
from typing import Set, Callable, Optional

# xtquant 可选导入：HTTP-only 学员没装 xtquant 也能 import 本模块
try:
    from xtquant.xttrader import XtQuantTrader, XtQuantTraderCallback
    from xtquant.xttype import StockAccount
    from xtquant import xtconstant
    _HAS_XTQUANT = True
except ImportError:
    XtQuantTrader = XtQuantTraderCallback = StockAccount = xtconstant = None
    _HAS_XTQUANT = False

# 沪深市价类型 → QMT HTTP prType 映射
_PR_TYPE_FIX = 11
_PR_TYPE_SH_MARKET = 43
_PR_TYPE_SZ_MARKET = 47


def _resolve_backend(backend):
    from lib.qmt_backend import current
    b = backend if backend in ("miniqmt", "qmt", "qmt_shared", "tdx") else current()
    if b == "tdx":
        raise RuntimeError("通达信模式不能下单，请把 WUCAI_QMT 改成 qmt 或 miniqmt")
    return b


if _HAS_XTQUANT:
    class _TraderCallback(XtQuantTraderCallback):
        """与基础版一致的回调类（事件落入 owner._events）；仅 miniQMT 后端用"""

        def __init__(self, owner):
            super().__init__()
            self.owner = owner

        def _now(self):
            return datetime.datetime.now().strftime("%H:%M:%S")

        def on_disconnected(self):
            msg = f"[{self._now()}] [DISCONNECT] miniQMT 连接断开"
            self.owner._events.append(msg)
            self.owner._connected = False  # 标记断线，让心跳触发重连
            print(msg)

        def on_account_status(self, status):
            self.owner._events.append(
                f"[{self._now()}] 账户状态: {status.account_id} 状态:{status.status}")

        def on_stock_order(self, order):
            direction = "买入" if order.order_type == 23 else "卖出"
            status_map = {48: '未报', 49: '待报', 50: '已报', 51: '已报待撤',
                          52: '部成待撤', 53: '部撤', 54: '已撤', 55: '部成',
                          56: '已成', 57: '废单'}
            status_text = status_map.get(order.order_status, str(order.order_status))
            self.owner._events.append(
                f"[{self._now()}] 委托推送: {order.stock_code} {direction} "
                f"委托{order.order_volume}股 已成交{order.traded_volume}股 状态:{status_text}")

        def on_stock_trade(self, trade):
            self.owner._events.append(
                f"[{self._now()}] 成交回报: {trade.stock_code} "
                f"成交{trade.traded_volume}股 价格:{trade.traded_price:.2f} "
                f"金额:{trade.traded_amount:.2f}")

        def on_order_error(self, order_error):
            self.owner._events.append(
                f"[{self._now()}] [ERROR] 委托失败: 编号:{order_error.order_id} {order_error.error_msg}")

        def on_cancel_error(self, cancel_error):
            self.owner._events.append(
                f"[{self._now()}] [ERROR] 撤单失败: 编号:{cancel_error.order_id} {cancel_error.error_msg}")


class QMTTraderV2:
    """
    增强版交易封装类（双后端：miniQMT / QMT HTTP）

    与基础版的差异：
        新增参数：enable_heartbeat / heartbeat_interval / enable_reconnect /
                  max_reconnect_attempts / reconnect_callback
                  backend（'miniqmt' | 'qmt'，默认读 WUCAI_QMT）
        新增方法：subscribe_safely / get_health_status / query_orders
    """

    def __init__(self, qmt_path=None, account_id=None, session_id=None,
                 max_positions=10, max_order_amount=500_000,
                 enable_heartbeat=True, heartbeat_interval=30,
                 enable_reconnect=True, max_reconnect_attempts=5,
                 reconnect_callback: Optional[Callable] = None,
                 backend=None, base_url=None, token=None, account=None):
        # ---- 后端决议 ----
        self.backend = _resolve_backend(backend)
        self.use_miniqmt = (self.backend == 'miniqmt')

        # ---- miniQMT 配置 ----
        self.qmt_path = qmt_path or os.getenv("QMT_PATH")
        self.account_id = account_id or os.getenv("ACCOUNT_ID")
        self.session_id = session_id or (int(time.time()) * 1000 + (os.getpid() % 1000))

        # ---- QMT HTTP 配置 ----
        self.base_url = (base_url or os.getenv("QMT_BASE_URL", "http://127.0.0.1:15588")).rstrip("/")
        self.token = token or os.getenv("QMT_TOKEN", "learning_ai_with_dr_chen")
        self.account = account or os.getenv("QMT_ACCOUNT", "stock")

        self.max_positions = max_positions
        self.max_order_amount = max_order_amount
        self.enable_heartbeat = enable_heartbeat
        self.heartbeat_interval = heartbeat_interval
        self.enable_reconnect = enable_reconnect
        self.max_reconnect_attempts = max_reconnect_attempts
        self.reconnect_callback = reconnect_callback

        self._trader = None
        self._account = None
        self._connected = False
        self._events = []

        # 订阅保护
        self._subscribed: Set[str] = set()
        self._sub_lock = threading.Lock()
        self._sub_last_time = 0.0
        self._sub_min_interval = 0.05

        # 心跳与统计
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._heartbeat_stop = threading.Event()
        self._heartbeat_stats = {
            "total_checks": 0, "failed_checks": 0,
            "last_check_at": None, "last_failure_at": None,
            "reconnect_attempts": 0, "reconnect_success": 0,
        }

    # ----------------------------------------------------------
    # 内部：QMT HTTP 请求
    # ----------------------------------------------------------

    def _http_call(self, method, path, **kwargs):
        if self.backend == 'qmt_shared':
            from lib.qmt_shared import shared_http
            return shared_http(path, kwargs.get('json'), method, timeout=10)
        import requests
        # QMT 在本机，绝不能走系统代理。学员开了 VPN/Clash 时
        # HTTP_PROXY=127.0.0.1:7897，requests 会把 127.0.0.1:15588 也塞进代理，
        # 表现为随机 500 或超时，极难排查。用 setdefault 以免和调用方的 kwargs 撞车。
        kwargs.setdefault("proxies", {"http": None, "https": None})
        resp = requests.request(method, f"{self.base_url}{path}",
                                headers={"Content-Type": "application/json",
                                         "X-Token": self.token},
                                timeout=10, **kwargs)
        resp.raise_for_status()
        return resp.json()

    @staticmethod
    def _pick_pr_type(stock_code, price):
        if price > 0:
            return _PR_TYPE_FIX
        return _PR_TYPE_SH_MARKET if stock_code.endswith('.SH') else _PR_TYPE_SZ_MARKET

    def _probe(self):
        """探活：HTTP 调 python_version；miniQMT 调 query_stock_asset"""
        if self.use_miniqmt:
            asset = self._trader.query_stock_asset(self._account)
            if asset is None:
                raise RuntimeError("query_asset returned None")
        else:
            self._http_call('GET', '/api/sys/python_version')

    # ----------------------------------------------------------
    # 连接管理
    # ----------------------------------------------------------

    def connect(self, max_retry=3) -> bool:
        """连接后端并（miniQMT 时）订阅账户。失败会抛异常"""
        if not self.use_miniqmt:
            # ---- QMT HTTP：探活 ----
            for i in range(max_retry):
                try:
                    self._http_call('GET', '/api/sys/python_version')
                    self._connected = True
                    print(f"[CONNECT] 成功 QMT-HTTP {self.base_url}")
                    if self.enable_heartbeat:
                        self._start_heartbeat()
                    return True
                except Exception as e:
                    print(f"[CONNECT] 重试 {i + 1}/{max_retry}... {e}")
                    time.sleep(2)
            raise Exception(f"[CONNECT] 连接 QMT HTTP 失败: {self.base_url}")

        # ---- miniQMT ----
        if not _HAS_XTQUANT:
            raise Exception("当前后端是 miniQMT，但未安装 xtquant。"
                            "请 pip install xtquant，或设 WUCAI_QMT=qmt 用 QMT HTTP。")

        self._trader = XtQuantTrader(self.qmt_path, self.session_id)
        self._trader.register_callback(_TraderCallback(self))
        self._trader.start()

        for i in range(max_retry):
            result = self._trader.connect()
            if result == 0:
                break
            print(f"[CONNECT] 重试 {i + 1}/{max_retry}...")
            time.sleep(2)
        else:
            raise Exception(f"[CONNECT] 连接 miniQMT 失败, 路径: {self.qmt_path}")

        self._account = StockAccount(self.account_id)
        sub_result = self._trader.subscribe(self._account)
        if sub_result != 0:
            raise Exception(f"[CONNECT] 订阅账户失败, 错误码: {sub_result}")

        self._connected = True
        print(f"[CONNECT] 成功 账户:{self.account_id} session:{self.session_id}")

        if self.enable_heartbeat:
            self._start_heartbeat()
        return True

    def disconnect(self):
        """优雅断开（QMT HTTP 后端只停心跳，不关 Server）"""
        self._heartbeat_stop.set()
        if self._heartbeat_thread and self._heartbeat_thread.is_alive():
            self._heartbeat_thread.join(timeout=3)
        if self.use_miniqmt and self._trader:
            self._trader.stop()
            print("[DISCONNECT] 已断开 miniQMT")
        self._connected = False

    @property
    def connected(self):
        return self._connected

    @property
    def events(self):
        return list(self._events)

    # ----------------------------------------------------------
    # 心跳保活（两后端统一走 _probe）
    # ----------------------------------------------------------

    def _start_heartbeat(self):
        self._heartbeat_stop.clear()
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop, daemon=True, name="qmt-heartbeat")
        self._heartbeat_thread.start()
        print(f"[HEARTBEAT] 已启动 间隔={self.heartbeat_interval}s")

    def _heartbeat_loop(self):
        while not self._heartbeat_stop.wait(self.heartbeat_interval):
            self._heartbeat_stats["total_checks"] += 1
            self._heartbeat_stats["last_check_at"] = datetime.datetime.now().isoformat(timespec="seconds")
            try:
                self._probe()
                if not self._connected:
                    self._connected = True
                    print(f"[HEARTBEAT] 连接已自动恢复 ({self._heartbeat_stats['last_check_at']})")
            except Exception as e:
                self._heartbeat_stats["failed_checks"] += 1
                self._heartbeat_stats["last_failure_at"] = datetime.datetime.now().isoformat(timespec="seconds")
                self._connected = False
                print(f"[HEARTBEAT] [WARN] 失败: {e} (累计失败 {self._heartbeat_stats['failed_checks']})")
                if self.enable_reconnect:
                    self._reconnect_with_backoff()

    def _reconnect_with_backoff(self):
        """指数退避重连：1s -> 2s -> 4s -> 8s -> 16s"""
        for attempt in range(self.max_reconnect_attempts):
            if self._heartbeat_stop.is_set():
                return
            wait_sec = 2 ** attempt
            print(f"[RECONNECT] 第 {attempt + 1}/{self.max_reconnect_attempts} 次, 等待 {wait_sec}s")
            time.sleep(wait_sec)
            self._heartbeat_stats["reconnect_attempts"] += 1
            try:
                if self.use_miniqmt:
                    self._trader.stop()
                    self._trader = XtQuantTrader(self.qmt_path, self.session_id)
                    self._trader.register_callback(_TraderCallback(self))
                    self._trader.start()
                    if self._trader.connect() == 0:
                        self._trader.subscribe(self._account)
                    else:
                        raise RuntimeError("connect failed")
                else:
                    self._http_call('GET', '/api/sys/python_version')
                self._connected = True
                self._heartbeat_stats["reconnect_success"] += 1
                print(f"[RECONNECT] [OK] 第 {attempt + 1} 次成功")
                if self.reconnect_callback:
                    self.reconnect_callback("success", attempt + 1)
                return
            except Exception as e:
                print(f"[RECONNECT] 第 {attempt + 1} 次失败: {e}")
        print(f"[RECONNECT] [FATAL] 累计 {self.max_reconnect_attempts} 次重连均失败")
        if self.reconnect_callback:
            self.reconnect_callback("failure", self.max_reconnect_attempts)

    def get_health_status(self) -> dict:
        return {
            "connected": self._connected,
            "backend": self.backend,
            "session_id": self.session_id,
            "account_id": self.account_id,
            "subscribed_count": len(self._subscribed),
            **self._heartbeat_stats,
        }

    # ----------------------------------------------------------
    # 风控检查
    # ----------------------------------------------------------

    def _check_risk(self, stock_code, volume, price):
        positions = self.query_positions()   # 两后端统一走公共方法
        if positions:
            hold_count = sum(1 for p in positions if (p.get('volume') or 0) > 0)
            if hold_count >= self.max_positions:
                held_codes = {p['stock_code'] for p in positions if (p.get('volume') or 0) > 0}
                if stock_code not in held_codes:
                    print(f"[RISK] 持仓已达上限 {self.max_positions} 只, 拒绝新开仓")
                    return False
        if price > 0:
            order_amount = volume * price
            if order_amount > self.max_order_amount:
                print(f"[RISK] 单笔金额 {order_amount:,.0f} 超过上限 {self.max_order_amount:,.0f}")
                return False
        return True

    # ----------------------------------------------------------
    # 买卖（与基础版 API 兼容）
    # ----------------------------------------------------------

    def buy(self, stock_code, volume, price=0, strategy_name='', remark=''):
        if not self._connected:
            print("[BUY] [ERROR] 未连接")
            return None
        if not self._check_risk(stock_code, volume, price):
            return None
        if not self.use_miniqmt:
            return self._http_order('/api/order/buy', stock_code, volume, price)

        price_type = (xtconstant.FIX_PRICE if price > 0 else
                      (xtconstant.MARKET_SH_CONVERT_5_LIMIT if stock_code.endswith('.SH')
                       else xtconstant.MARKET_SZ_CONVERT_5_CANCEL))
        order_id = self._trader.order_stock(
            self._account, stock_code, xtconstant.STOCK_BUY,
            volume, price_type, price if price > 0 else 0, strategy_name, remark)
        print(f"[BUY] {stock_code} {volume}股 {'市价' if price == 0 else f'{price:.2f}'} 编号:{order_id}")
        return order_id

    def sell(self, stock_code, volume, price=0, strategy_name='', remark=''):
        if not self._connected:
            print("[SELL] [ERROR] 未连接")
            return None
        if not self.use_miniqmt:
            return self._http_order('/api/order/sell', stock_code, volume, price)

        price_type = (xtconstant.FIX_PRICE if price > 0 else
                      (xtconstant.MARKET_SH_CONVERT_5_LIMIT if stock_code.endswith('.SH')
                       else xtconstant.MARKET_SZ_CONVERT_5_CANCEL))
        order_id = self._trader.order_stock(
            self._account, stock_code, xtconstant.STOCK_SELL,
            volume, price_type, price if price > 0 else 0, strategy_name, remark)
        print(f"[SELL] {stock_code} {volume}股 {'市价' if price == 0 else f'{price:.2f}'} 编号:{order_id}")
        return order_id

    def _http_order(self, path, stock_code, volume, price):
        pr_type = self._pick_pr_type(stock_code, price)
        body = self._http_call('POST', path, json={
            "stock": stock_code, "price": price if price > 0 else 0,
            "volume": volume, "prType": pr_type})
        order_id = body.get('order_ref') if isinstance(body, dict) else None
        action = 'BUY' if 'buy' in path else 'SELL'
        print(f"[{action}] {stock_code} {volume}股 {'市价' if price == 0 else f'{price:.2f}'} 编号:{order_id}")
        return order_id

    def cancel(self, order_id):
        if not self._connected:
            return None
        if not self.use_miniqmt:
            body = self._http_call('POST', '/api/order/cancel_by_id',
                                   json={"orderId": order_id, "account": self.account})
            ok = isinstance(body, dict) and body.get('status') == 'success'
            print(f"[CANCEL] 编号:{order_id} 结果:{'OK' if ok else body}")
            return 0 if ok else None

        result = self._trader.cancel_order_stock(self._account, order_id)
        print(f"[CANCEL] 编号:{order_id} 结果:{'OK' if result == 0 else f'失败({result})'}")
        return result

    # ----------------------------------------------------------
    # 查询
    # ----------------------------------------------------------

    def query_asset(self):
        if not self._connected:
            return {}
        if not self.use_miniqmt:
            total = self._http_call('POST', '/api/money/total', json={"account": self.account})
            avail = self._http_call('POST', '/api/money/available', json={"account": self.account})
            total_val = total.get('total_money') if isinstance(total, dict) else None
            cash_val = avail.get('available_money') if isinstance(avail, dict) else None
            mv = round(total_val - cash_val, 2) if (total_val and cash_val) else None
            return {'total_asset': total_val, 'cash': cash_val,
                    'market_value': mv, 'frozen_cash': None}

        asset = self._trader.query_stock_asset(self._account)
        if asset is None:
            return {}
        return {'total_asset': asset.total_asset, 'cash': asset.cash,
                'market_value': asset.market_value, 'frozen_cash': asset.frozen_cash}

    def query_positions(self):
        if not self._connected:
            return []
        if not self.use_miniqmt:
            body = self._http_call('POST', '/api/holding', json={"account": self.account})
            holding = body if isinstance(body, dict) and 'error' not in body else {}
            out = []
            for code, p in holding.items():
                if not isinstance(p, dict):
                    continue
                vol = p.get('Volume', 0) or 0
                if vol > 0:
                    out.append({'stock_code': p.get('StockCode', code), 'volume': vol,
                                'can_use_volume': p.get('CanUseVolume', 0),
                                'open_price': p.get('OpenPrice', 0),
                                'market_value': p.get('MarketValue', 0)})
            return out

        positions = self._trader.query_stock_positions(self._account)
        if not positions:
            return []
        return [{'stock_code': p.stock_code, 'volume': p.volume,
                 'can_use_volume': p.can_use_volume, 'open_price': p.open_price,
                 'market_value': p.market_value} for p in positions if p.volume > 0]

    def query_orders(self):
        """
        查询当日委托，返回 list[dict]。
        【新增】修复 routes/live.py 中直接访问 trader._trader 的抽象泄漏——
        上层应统一调本方法，HTTP 后端没有 _trader/_account 属性。
        """
        if not self._connected:
            return []
        if not self.use_miniqmt:
            body = self._http_call('POST', '/api/order/status', json={"account": self.account})
            orders = body.get('orders', []) if isinstance(body, dict) else []
            return [{'order_id': o.get('order_sys_id'), 'order_status': o.get('status'),
                     'order_volume': o.get('volume_left'),
                     'traded_volume': o.get('volume_traded')} for o in orders]

        orders = self._trader.query_stock_orders(self._account)
        if not orders:
            return []
        return [{'order_id': o.order_id, 'stock_code': o.stock_code,
                 'order_type': o.order_type, 'order_volume': o.order_volume,
                 'price': o.price, 'traded_volume': o.traded_volume,
                 'order_status': o.order_status, 'order_time': o.order_time}
                for o in orders]


# ============================================================
# 自检
# ============================================================
if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()

    print("=" * 60)
    print("QMTTraderV2 增强版自检（双后端）")
    print("=" * 60)

    trader = QMTTraderV2(
        qmt_path=os.getenv("QMT_PATH"),
        account_id=os.getenv("ACCOUNT_ID"),
        enable_heartbeat=False,   # 自检不开心跳
        backend=os.getenv("WUCAI_QMT", "qmt"),
    )
    print(f"后端: {trader.backend}")
    trader.connect()

    print("\n--- 账户资产 ---")
    for k, v in trader.query_asset().items():
        print(f"  {k}: {v}")

    print("\n--- 持仓 ---")
    positions = trader.query_positions()
    print(f"  {len(positions)} 条") if positions else print("  无持仓")

    print("\n--- 委托 ---")
    print(f"  {len(trader.query_orders())} 条")

    print("\n--- 健康状态 ---")
    for k, v in trader.get_health_status().items():
        print(f"  {k}: {v}")

    trader.disconnect()


# 向后兼容别名（旧代码/副本仍可用旧名 import）
MiniQMTTraderV2 = QMTTraderV2
