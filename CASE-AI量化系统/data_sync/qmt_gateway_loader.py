# -*- coding: gbk -*-
"""
QMT动态策略加载器
通过进程间共享内存实现外部策略代码的动态注入和执行
协议版本: v1.0
"""
import base64
import hashlib
import json
import mmap
import struct
import threading
import time
from typing import Optional, Dict, Callable, Tuple

# 配置常量
CONFIG = {
    'shared_memory_name': 'SentinelQmtLoaderV1',
    'memory_size': 1024 * 1024,  # 1MB
    'header_size': 24,
    'max_code_size': 512 * 1024,  # 512KB
    'magic_bytes': b'SQL1',
    'protocol_version': 1,
    'poll_interval': 2.0,
    'log_interval': 30.0
}

# 数据包头部结构
PACKET_HEADER_FORMAT = '<4sIQII'  # magic, version, sequence, length, checksum


class LoaderException(Exception):
    """加载器基础异常"""
    pass


class GatewayNotReady(LoaderException):
    """网关未就绪异常"""
    pass


class ProtocolError(LoaderException):
    """协议错误异常"""
    pass


class SharedMemoryReader:
    """共享内存读取器"""

    def __init__(self, memory_name: str, memory_size: int):
        self.memory_name = memory_name
        self.memory_size = memory_size

    def read_packet(self) -> bytes:
        """从共享内存读取数据包"""
        mem = mmap.mmap(-1, self.memory_size, tagname=self.memory_name)
        try:
            return self._extract_data(mem)
        finally:
            mem.close()

    def _extract_data(self, mem) -> bytes:
        """提取并验证数据"""
        header_size = CONFIG['header_size']

        # 读取头部
        header_before = bytes(mem[:header_size])
        magic, version, seq, length, checksum = struct.unpack(PACKET_HEADER_FORMAT, header_before)

        # 验证基本字段
        if not seq or not (seq & 1) or not length:
            raise GatewayNotReady('数据未发布或序列号无效')

        if magic != CONFIG['magic_bytes'] or version != CONFIG['protocol_version']:
            raise ProtocolError(f'协议不兼容: magic={magic}, version={version}')

        if not (0 < length <= self.memory_size - header_size):
            raise ProtocolError(f'数据长度无效: {length}')

        # 读取数据体
        data = bytes(mem[header_size:header_size + length])

        # 验证数据完整性
        header_after = bytes(mem[:header_size])
        if header_before != header_after:
            raise GatewayNotReady('读取过程中数据被修改')

        import binascii
        if binascii.crc32(data) & 0xffffffff != checksum:
            raise GatewayNotReady('数据校验失败')

        return data


class StrategyPacketParser:
    """策略数据包解析器"""

    def __init__(self, max_code_size: int):
        self.max_code_size = max_code_size

    def parse(self, raw_data: bytes) -> Tuple[bytes, Dict]:
        """解析策略数据包"""
        # 验证数据大小
        if not isinstance(raw_data, bytes):
            raise ProtocolError('数据类型错误')

        if not (0 < len(raw_data) <= CONFIG['memory_size'] - CONFIG['header_size']):
            raise ProtocolError(f'数据包大小无效: {len(raw_data)}')

        # 解析JSON
        try:
            packet = json.loads(raw_data.decode('utf-8'))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise ProtocolError(f'JSON解析失败: {e}')

        # 验证数据包结构
        required_fields = {'abi', 'version', 'revision', 'code_length', 'sha256', 'code_b64'}
        if not isinstance(packet, dict) or set(packet.keys()) != required_fields:
            raise ProtocolError(f'数据包格式错误，需要字段: {required_fields}')

        # 验证各字段
        self._validate_fields(packet)

        # 解码策略代码
        code = self._decode_code(packet)

        # 返回代码和元数据
        metadata = {
            'version': packet['version'],
            'revision': packet['revision']
        }

        return code, metadata

    def _validate_fields(self, packet: dict):
        """验证数据包字段"""
        # 验证ABI版本
        if not isinstance(packet['abi'], int) or packet['abi'] != 1:
            raise ProtocolError(f'不支持的ABI版本: {packet["abi"]}')

        # 验证代码长度
        code_length = packet['code_length']
        if not isinstance(code_length, int) or not (0 < code_length <= self.max_code_size):
            raise ProtocolError(f'代码长度无效: {code_length}')

        # 验证修订号
        revision = packet['revision']
        if not isinstance(revision, int) or not (1 <= revision <= 2**31 - 1):
            raise ProtocolError(f'修订号无效: {revision}')

        # 验证版本字符串
        version = packet['version']
        if not isinstance(version, str) or not (0 < len(version) <= 64):
            raise ProtocolError('版本字符串无效')
        if any(ord(c) < 32 for c in version):
            raise ProtocolError('版本字符串包含非法字符')

        # 验证编码字段
        if not isinstance(packet['code_b64'], str) or not isinstance(packet['sha256'], str):
            raise ProtocolError('代码编码字段类型错误')

    def _decode_code(self, packet: dict) -> bytes:
        """解码并验证策略代码"""
        try:
            code = base64.b64decode(packet['code_b64'], validate=True)
        except Exception as e:
            raise ProtocolError(f'Base64解码失败: {e}')

        # 验证代码长度
        if len(code) != packet['code_length']:
            raise ProtocolError(f'代码长度不匹配: 期望{packet["code_length"]}, 实际{len(code)}')

        # 验证SHA256
        actual_hash = hashlib.sha256(code).hexdigest()
        if actual_hash != packet['sha256']:
            raise ProtocolError(f'代码校验和不匹配')

        # 验证UTF-8编码
        try:
            code.decode('utf-8')
        except UnicodeDecodeError as e:
            raise ProtocolError(f'代码不是有效的UTF-8: {e}')

        return code


class StrategyExecutor:
    """策略执行器"""

    def __init__(self, qmt_context, vendor_functions: Dict[str, Callable], logger: Callable):
        self.qmt_context = qmt_context
        self.vendor_functions = vendor_functions.copy()
        self.logger = logger
        self.strategy_namespace = None
        self.owner_thread_id = threading.get_ident()

    def is_loaded(self) -> bool:
        """检查策略是否已加载"""
        return self.strategy_namespace is not None

    def load_and_execute(self, code: bytes, metadata: Dict):
        """加载并执行策略代码"""
        self._check_thread()

        if self.is_loaded():
            self.shutdown()

        # 构建执行命名空间
        namespace = self._build_namespace(metadata)

        try:
            # 编译并执行代码
            code_str = code.decode('utf-8')
            compiled = compile(code_str, '<dynamic-strategy>', 'exec')
            exec(compiled, namespace)

            # 验证必需的回调函数
            self._validate_callbacks(namespace)

            # 保存命名空间
            self.strategy_namespace = namespace

            # 调用初始化回调
            namespace['init_loaded'](self.qmt_context)

            # 记录日志
            self.logger(f'[系统] 策略已加载: {metadata["version"]} (修订 {metadata["revision"]})')

        except Exception as e:
            self.logger(f'[错误] 策略加载失败: {e}')
            self.strategy_namespace = None
            raise

    def _build_namespace(self, metadata: Dict) -> dict:
        """构建策略执行的命名空间"""
        namespace = self.vendor_functions.copy()
        namespace.update({
            '__name__': 'dynamic_gateway_strategy',
            '__file__': '<qmt-gateway-strategy>',
            '__metadata__': metadata.copy()
        })
        return namespace

    def _validate_callbacks(self, namespace: dict):
        """验证必需的回调函数"""
        required_callbacks = ['init_loaded', '_native_gateway_tick', 'handlebar', 'stop']
        for callback_name in required_callbacks:
            if not callable(namespace.get(callback_name)):
                raise ProtocolError(f'缺少必需的回调函数: {callback_name}')

    def tick(self):
        """执行定时回调"""
        self._check_thread()
        if self.strategy_namespace:
            self.strategy_namespace['_native_gateway_tick'](self.qmt_context)

    def handle_bar(self):
        """执行K线回调"""
        self._check_thread()
        if self.strategy_namespace:
            self.strategy_namespace['handlebar'](self.qmt_context)

    def shutdown(self):
        """关闭策略"""
        self._check_thread()
        if self.strategy_namespace:
            try:
                self.strategy_namespace['stop'](self.qmt_context)
            except Exception as e:
                self.logger(f'[错误] 策略停止时出错: {e}')
            finally:
                self.strategy_namespace = None

    def _check_thread(self):
        """检查线程安全"""
        if threading.get_ident() != self.owner_thread_id:
            raise RuntimeError('策略执行器只能在创建线程中调用')


class GatewayLoaderService:
    """网关加载服务"""

    def __init__(self, qmt_context, vendor_functions: Dict[str, Callable], logger: Callable = print):
        self.qmt_context = qmt_context
        self.logger = logger

        # 初始化组件
        self.reader = SharedMemoryReader(
            CONFIG['shared_memory_name'],
            CONFIG['memory_size']
        )
        self.parser = StrategyPacketParser(CONFIG['max_code_size'])
        self.executor = StrategyExecutor(qmt_context, vendor_functions, logger)

        # 线程控制
        self.stop_event = threading.Event()
        self.data_lock = threading.Lock()
        self.worker_thread = None
        self.pending_strategy = None
        self.last_received_revision = 0
        self.is_started = False

    def start(self):
        """启动加载服务"""
        if self.is_started or self.stop_event.is_set():
            raise RuntimeError('服务已启动或已停止')

        # 注册定时器
        self.qmt_context.run_time('_native_gateway_tick', '20nMilliSecond', '1970-01-01 00:00:00', 'SH')

        # 启动后台监听线程
        self.worker_thread = threading.Thread(
            target=self._monitor_loop,
            name='qmt-gateway-monitor',
            daemon=True
        )
        self.worker_thread.start()
        self.is_started = True

    def _monitor_loop(self):
        """后台监听循环"""
        last_log_time = -float('inf')

        while not self.stop_event.is_set():
            error_stage = None
            error_detail = ''

            try:
                raw_data = self.reader.read_packet()
                error_stage = 'parse'
                code, metadata = self.parser.parse(raw_data)

                with self.data_lock:
                    if metadata['revision'] > self.last_received_revision and not self.stop_event.is_set():
                        self.last_received_revision = metadata['revision']
                        self.pending_strategy = (code, metadata)
                        self.logger(
                            f'[系统] 收到策略: {metadata["version"]} '
                            f'(修订 {metadata["revision"]})'
                        )
                error_stage = None

            except GatewayNotReady:
                error_stage = 'waiting'
            except Exception as e:
                error_detail = str(e)

            current_time = time.monotonic()
            if current_time - last_log_time >= CONFIG['log_interval'] and not self.stop_event.is_set():
                last_log_time = current_time
                if error_stage == 'parse':
                    self.logger(f'[错误] 数据包解析失败: {error_detail}')
                elif error_stage == 'waiting':
                    self.logger('[系统] 等待外部网关发布策略数据 (IPC)')

            self.stop_event.wait(CONFIG['poll_interval'])

    def _load_pending(self):
        with self.data_lock:
            pending = self.pending_strategy
            self.pending_strategy = None

        if not pending or self.stop_event.is_set():
            return

        code, metadata = pending
        try:
            self.executor.load_and_execute(code, metadata)
        except Exception as e:
            self.logger(f'[错误] 策略加载失败: {e}')

    def process_tick(self):
        """处理定时器tick"""
        if self.pending_strategy:
            self._load_pending()

        if not self.stop_event.is_set():
            self.executor.tick()

    def process_handlebar(self):
        """处理K线回调"""
        if self.pending_strategy:
            self._load_pending()

        if not self.stop_event.is_set():
            self.executor.handle_bar()

    def shutdown(self):
        """关闭服务"""
        self.stop_event.set()
        self.executor.shutdown()

        with self.data_lock:
            self.pending_strategy = None

        if self.worker_thread and self.worker_thread.is_alive():
            self.worker_thread.join(timeout=0.1)


# 全局服务实例
_service_instance: Optional[GatewayLoaderService] = None


def init(ContextInfo):
    """初始化加载器（QMT调用）"""
    global _service_instance

    if _service_instance is not None:
        raise RuntimeError('加载器已初始化')

    # 收集QMT提供的交易和数据函数
    available_functions = {}
    function_names = [
        'get_trade_detail_data',
        'get_industry_name_of_stock',
        'passorder',
        'cancel',
        'get_ipo_data',
        'get_new_purchase_limit'
    ]

    for name in function_names:
        if name in globals():
            available_functions[name] = globals()[name]

    # 创建服务实例
    _service_instance = GatewayLoaderService(ContextInfo, available_functions)

    try:
        _service_instance.start()
    except Exception:
        _service_instance = None
        raise


def _get_service(context) -> Optional[GatewayLoaderService]:
    """获取服务实例并验证上下文"""
    if _service_instance and context is not _service_instance.qmt_context:
        raise RuntimeError('上下文不匹配')
    return _service_instance


def _native_gateway_tick(ContextInfo):
    """定时器回调（QMT调用）"""
    service = _get_service(ContextInfo)
    if service:
        service.process_tick()


def handlebar(ContextInfo):
    """K线回调（QMT调用）"""
    service = _get_service(ContextInfo)
    if service:
        service.process_handlebar()


def stop(ContextInfo):
    """停止回调（QMT调用）"""
    global _service_instance

    service = _get_service(ContextInfo)
    if service:
        try:
            service.shutdown()
        finally:
            _service_instance = None

