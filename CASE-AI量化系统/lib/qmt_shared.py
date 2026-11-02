# -*- coding: utf-8 -*-
"""本项目自带的 QMT 共享内存客户端，配合同目录的 qmt_gateway_loader.py 使用。"""
import base64
import binascii
import hashlib
import json
import mmap
import os
import struct
import threading
import time

HEADER_FORMAT = '<4sIQII'
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)
STRATEGY_MEMORY_NAME = 'SentinelQmtLoaderV1'
STRATEGY_MEMORY_SIZE = 1024 * 1024
COMMAND_MEMORY_NAME = 'SentinelQmtCommandV1'
COMMAND_MEMORY_SIZE = 1024 * 1024
RESULT_MEMORY_NAME = 'SentinelQmtResultV1'
RESULT_MEMORY_SIZE = 16 * 1024 * 1024


QMT_SERVER_SOURCE = r'''
import binascii
import json
import math
import mmap
import struct
import time
from datetime import datetime, timedelta

HEADER_FORMAT = '<4sIQII'
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)
COMMAND_SIZE = 1024 * 1024
RESULT_SIZE = 16 * 1024 * 1024
_command = None
_result = None
_command_sequence = 0
_result_sequence = 0
_last_command_sequence = 0
_industry_cache = None


def _read_packet(mem, size):
    header = bytes(mem[:HEADER_SIZE])
    try:
        magic, version, sequence, length, checksum = struct.unpack(HEADER_FORMAT, header)
    except struct.error:
        return None
    if not sequence or not sequence & 1:
        return None
    if magic != b'SQL1' or version != 1:
        return None
    if not 0 < length <= size - HEADER_SIZE:
        return None
    body = bytes(mem[HEADER_SIZE:HEADER_SIZE + length])
    if header != bytes(mem[:HEADER_SIZE]):
        return None
    if binascii.crc32(body) & 0xffffffff != checksum:
        return None
    return sequence, json.loads(body.decode('utf-8'))


def _write_result(packet):
    global _result_sequence
    body = json.dumps(packet, ensure_ascii=False, default=str,
                      separators=(',', ':')).encode('utf-8')
    if len(body) > RESULT_SIZE - HEADER_SIZE:
        body = json.dumps({
            'request_id': packet.get('request_id'),
            'status': 'error',
            'error': '返回数据超过16MB，请缩小批次或日期范围',
        }, ensure_ascii=False).encode('utf-8')
    checksum = binascii.crc32(body) & 0xffffffff
    writing = _result_sequence + 1 if _result_sequence & 1 else _result_sequence
    published = writing + 1
    _result.seek(0)
    _result.write(struct.pack(HEADER_FORMAT, b'SQL1', 1, writing, len(body), checksum))
    _result.seek(HEADER_SIZE)
    _result.write(body)
    _result.flush()
    _result.seek(0)
    _result.write(struct.pack(HEADER_FORMAT, b'SQL1', 1, published, len(body), checksum))
    _result.flush()
    _result_sequence = published


def _json_num(value):
    if value is None:
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return value


def _serialize_financial(raw):
    result = {}
    if not isinstance(raw, dict):
        return result
    for stock, field_map in raw.items():
        if not isinstance(field_map, dict) or not field_map:
            result[stock] = []
            continue
        timestamps = set()
        for series in field_map.values():
            if isinstance(series, dict):
                timestamps.update(series.keys())
        short_names = {str(name).split('.')[-1] for name in field_map}
        filter_core = 'revenue_inc' in short_names or 'tot_assets' in short_names
        records = []
        for timestamp in sorted(timestamps):
            try:
                dt = datetime.utcfromtimestamp(int(timestamp) / 1000.0) + timedelta(hours=8)
                end_date = dt.strftime('%Y%m%d')
            except Exception:
                end_date = str(timestamp)
            record = {'end_date': end_date}
            for name, series in field_map.items():
                value = series.get(timestamp) if isinstance(series, dict) else None
                record[str(name).split('.')[-1]] = _json_num(value)
            if filter_core and record.get('revenue_inc') is None and record.get('tot_assets') is None:
                continue
            records.append(record)
        result[stock] = records
    return result


def _account_id(ContextInfo, params):
    account_id = str(params.get('account_id') or getattr(ContextInfo, 'accountID', '') or '')
    if not account_id:
        raise RuntimeError('未配置 ACCOUNT_ID，无法查询或操作交易账户')
    return account_id


def _trade_detail(ContextInfo, params, datatype):
    account_id = _account_id(ContextInfo, params)
    account = params.get('account', 'stock')
    return get_trade_detail_data(account_id, account, datatype) or []


def _execute(ContextInfo, action, params):
    if action == 'ping':
        import sys
        return {'python_version': sys.version}
    if action == 'stock_list':
        stocks = ContextInfo.get_stock_list_in_sector(params['sectorname'])
        return {'stocks': stocks or []}
    if action == 'industry_name':
        global _industry_cache
        if _industry_cache is None:
            _industry_cache = {}
            sectors = (
                'SW1交通运输', 'SW1传媒', 'SW1公用事业', 'SW1农林牧渔',
                'SW1医药生物', 'SW1商贸零售', 'SW1国防军工', 'SW1基础化工',
                'SW1家用电器', 'SW1建筑材料', 'SW1建筑装饰', 'SW1房地产',
                'SW1有色金属', 'SW1机械设备', 'SW1汽车', 'SW1煤炭',
                'SW1环保', 'SW1电力设备', 'SW1电子', 'SW1石油石化',
                'SW1社会服务', 'SW1纺织服饰', 'SW1综合', 'SW1美容护理',
                'SW1计算机', 'SW1轻工制造', 'SW1通信', 'SW1钢铁',
                'SW1银行', 'SW1非银金融', 'SW1食品饮料',
            )
            for sector in sectors:
                for code in ContextInfo.get_stock_list_in_sector(sector) or []:
                    _industry_cache[code] = sector
        return {'industry_name': _industry_cache.get(params['stockcode'], '')}
    if action == 'last_volume':
        return {'last_volume': ContextInfo.get_last_volume(params['stockcode'])}
    if action == 'full_tick':
        return ContextInfo.get_full_tick(params['stocks']) or {}
    if action == 'divid_factors':
        return {'factors': ContextInfo.get_divid_factors(params['stockcode']) or {}}
    if action == 'instrument_detail':
        return {'detail': ContextInfo.get_instrumentdetail(params['stockcode']) or {}}
    if action == 'market_data_ex':
        raw = ContextInfo.get_market_data_ex(
            params['fields'], params['stocks'], params.get('period', '1d'),
            params.get('start_time', ''), params.get('end_time', ''),
            int(params.get('count', -1)), params.get('dividend_type', 'none'))
        data = {}
        for stock, values in (raw or {}).items():
            data[stock] = values.to_dict() if hasattr(values, 'to_dict') else values
        return {'data': data}
    if action == 'market_data':
        raw = ContextInfo.get_market_data(
            params['fields'], params['stocks'], params.get('start_time', ''),
            params.get('end_time', ''), True, params.get('period', '1d'),
            params.get('dividend_type', 'none'), int(params.get('count', -1)))
        return {'data': raw.to_dict() if hasattr(raw, 'to_dict') else raw}
    if action == 'financial_data':
        raw = ContextInfo.get_raw_financial_data(
            params['fields'], params['stocks'], params['start_date'],
            params['end_date'], params.get('report_type', 'report_time'))
        data = _serialize_financial(raw)
        return {
            'data': data,
            'nonempty_count': sum(1 for rows in data.values() if rows),
        }
    if action in ('money_total', 'money_available'):
        rows = _trade_detail(ContextInfo, params, 'account')
        if not rows:
            raise RuntimeError('资金数据为空，请检查 ACCOUNT_ID 和账户类型')
        key = 'total_money' if action == 'money_total' else 'available_money'
        value = rows[0].m_dBalance if action == 'money_total' else rows[0].m_dAvailable
        return {key: round(value, 2)}
    if action == 'holding':
        result = {}
        for position in _trade_detail(ContextInfo, params, 'position'):
            stock = position.m_strInstrumentID + '.' + position.m_strExchangeID
            result[stock] = {
                'StockCode': stock,
                'StockName': position.m_strInstrumentName,
                'Volume': position.m_nVolume,
                'OpenPrice': position.m_dOpenPrice,
                'MarketValue': position.m_dMarketValue,
                'CanUseVolume': position.m_nCanUseVolume,
                'LastPrice': position.m_dLastPrice,
            }
        return result
    if action == 'order_status':
        return {'orders': [{
            'order_sys_id': order.m_strOrderSysID,
            'status': order.m_nOrderStatus,
            'volume_left': order.m_nVolumeTotal,
            'volume_traded': order.m_nVolumeTraded,
        } for order in _trade_detail(ContextInfo, params, 'order')]}
    if action == 'deal':
        fields = (
            'm_strTradeID', 'm_strInstrumentID', 'm_dPrice', 'm_nVolume',
            'm_dTradeAmount', 'm_strOrderSysID', 'm_strTradeTime',
        )
        return {'deals': [
            {name: getattr(deal, name, None) for name in fields}
            for deal in _trade_detail(ContextInfo, params, 'deal')
        ]}
    if action == 'cancel_all':
        account = params.get('account', 'stock')
        account_id = _account_id(ContextInfo, params)
        canceled = []
        for order in _trade_detail(ContextInfo, params, 'order'):
            if order.m_nOrderStatus in (48, 49, 50, 51, 52, 53):
                cancel(order.m_strOrderSysID, account_id, account, ContextInfo)
                canceled.append(order.m_strOrderSysID)
        return {'status': 'success', 'canceled_orders': canceled}
    if action in ('buy', 'sell'):
        account = params.get('account', 'stock')
        if action == 'buy':
            op_type = 33 if account == 'credit' else 23
        else:
            op_type = 34 if account == 'credit' else 24
        order_ref = passorder(
            op_type, 1101, _account_id(ContextInfo, params), params['stock'],
            int(params.get('prType', 11)), float(params['price']),
            int(params['volume']), 'qmt', 2, ContextInfo)
        return {
            'status': 'success', 'action': action, 'stock': params['stock'],
            'order_ref': str(order_ref) if order_ref else 'unknown',
        }
    if action == 'cancel_by_id':
        account = params.get('account', 'stock')
        account_id = _account_id(ContextInfo, params)
        order_id = str(params['orderId'])
        for order in _trade_detail(ContextInfo, params, 'order'):
            if order.m_strOrderSysID == order_id and order.m_nOrderStatus in (48, 49, 50, 51, 52, 53):
                cancel(order_id, account_id, account, ContextInfo)
                return {'status': 'success', 'order_sys_id': order_id}
        raise RuntimeError('未找到可撤的委托: ' + order_id)
    raise ValueError('不支持的数据请求: ' + str(action))


def _process(ContextInfo):
    global _last_command_sequence
    packet = _read_packet(_command, COMMAND_SIZE)
    if not packet:
        return
    sequence, request = packet
    if sequence == _last_command_sequence:
        return
    _last_command_sequence = sequence
    try:
        data = _execute(ContextInfo, request.get('action'), request.get('params') or {})
        _write_result({
            'request_id': request.get('request_id'),
            'status': 'ok',
            'data': data,
        })
    except Exception as exc:
        _write_result({
            'request_id': request.get('request_id'),
            'status': 'error',
            'error': str(exc),
        })


def init_loaded(ContextInfo):
    global _command, _result, _command_sequence, _result_sequence, _last_command_sequence
    if _command is None:
        _command = mmap.mmap(-1, COMMAND_SIZE, tagname='SentinelQmtCommandV1')
    if _result is None:
        _result = mmap.mmap(-1, RESULT_SIZE, tagname='SentinelQmtResultV1')
    _command_sequence = 0
    _result_sequence = 0
    _last_command_sequence = 0
    print('[共享内存数据服务] 已启动')


def _native_gateway_tick(ContextInfo):
    _process(ContextInfo)


def handlebar(ContextInfo):
    _process(ContextInfo)


def stop(ContextInfo):
    global _command, _result
    if _command is not None:
        _command.close()
        _command = None
    if _result is not None:
        _result.close()
        _result = None
    print('[共享内存数据服务] 已停止')
'''


class PacketWriter:
    def __init__(self, name, size):
        self.size = size
        self.memory = mmap.mmap(-1, size, tagname=name)
        self.sequence = self._published_sequence()

    def _published_sequence(self):
        try:
            magic, version, sequence, _, _ = struct.unpack(
                HEADER_FORMAT, self.memory[:HEADER_SIZE])
            if magic == b'SQL1' and version == 1 and sequence and sequence & 1:
                return sequence
        except (struct.error, ValueError):
            pass
        return 0

    def write(self, packet):
        body = json.dumps(packet, ensure_ascii=False, default=str,
                          separators=(',', ':')).encode('utf-8')
        if not 0 < len(body) <= self.size - HEADER_SIZE:
            raise ValueError('共享内存数据包过大')
        checksum = binascii.crc32(body) & 0xffffffff
        writing = self.sequence + 1 if self.sequence else 0
        published = writing + 1
        self.memory.seek(0)
        self.memory.write(struct.pack(
            HEADER_FORMAT, b'SQL1', 1, writing, len(body), checksum))
        self.memory.seek(HEADER_SIZE)
        self.memory.write(body)
        self.memory.flush()
        self.memory.seek(0)
        self.memory.write(struct.pack(
            HEADER_FORMAT, b'SQL1', 1, published, len(body), checksum))
        self.memory.flush()
        self.sequence = published

    def close(self):
        self.memory.close()


class PacketReader:
    def __init__(self, name, size):
        self.size = size
        self.memory = mmap.mmap(-1, size, tagname=name)

    def read(self):
        header = bytes(self.memory[:HEADER_SIZE])
        try:
            magic, version, sequence, length, checksum = struct.unpack(
                HEADER_FORMAT, header)
        except struct.error:
            return None
        if not sequence or not sequence & 1:
            return None
        if magic != b'SQL1' or version != 1:
            raise ValueError('共享内存协议不兼容')
        if not 0 < length <= self.size - HEADER_SIZE:
            raise ValueError('共享内存数据长度无效')
        body = bytes(self.memory[HEADER_SIZE:HEADER_SIZE + length])
        if header != bytes(self.memory[:HEADER_SIZE]):
            return None
        if binascii.crc32(body) & 0xffffffff != checksum:
            return None
        return json.loads(body.decode('utf-8'))

    def close(self):
        self.memory.close()


class SharedMemoryQMT:
    def __init__(self):
        self.strategy_writer = PacketWriter(STRATEGY_MEMORY_NAME, STRATEGY_MEMORY_SIZE)
        self.command_writer = PacketWriter(COMMAND_MEMORY_NAME, COMMAND_MEMORY_SIZE)
        self.result_reader = PacketReader(RESULT_MEMORY_NAME, RESULT_MEMORY_SIZE)
        self.request_number = 0
        self._publish_server()

    def _publish_server(self):
        previous_revision = 0
        try:
            magic, version, sequence, length, checksum = struct.unpack(
                HEADER_FORMAT, self.strategy_writer.memory[:HEADER_SIZE])
            if magic == b'SQL1' and version == 1 and sequence and sequence & 1:
                body = bytes(self.strategy_writer.memory[HEADER_SIZE:HEADER_SIZE + length])
                if binascii.crc32(body) & 0xffffffff == checksum:
                    previous_revision = json.loads(body.decode('utf-8')).get('revision', 0)
        except Exception:
            pass
        revision = max(int(time.time()), int(previous_revision) + 1)
        code = QMT_SERVER_SOURCE.encode('utf-8')
        self.strategy_writer.write({
            'abi': 1,
            'version': 'qmt-shared-server-1.0',
            'revision': revision,
            'code_length': len(code),
            'sha256': hashlib.sha256(code).hexdigest(),
            'code_b64': base64.b64encode(code).decode('ascii'),
        })
        self.revision = revision
        # 加载器每2秒轮询一次，避免首个请求被上一版数据服务处理。
        time.sleep(2.2)

    def call(self, action, params=None, timeout=120):
        self.request_number += 1
        request_id = '%d-%d-%d' % (os.getpid(), time.time_ns(), self.request_number)
        self.command_writer.write({
            'request_id': request_id,
            'action': action,
            'params': params or {},
        })
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            response = self.result_reader.read()
            if response and response.get('request_id') == request_id:
                if response.get('status') == 'error':
                    raise RuntimeError('QMT 数据请求失败: %s' % response.get('error'))
                return response.get('data')
            time.sleep(0.05)
        raise TimeoutError('等待 QMT 返回数据超时，请确认 qmt_gateway_loader 正在运行')

    def close(self):
        self.result_reader.close()
        self.command_writer.close()
        self.strategy_writer.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()


_CLIENT = None
_CLIENT_LOCK = threading.Lock()


def _client():
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = SharedMemoryQMT()
    return _CLIENT


def reset_client():
    global _CLIENT
    with _CLIENT_LOCK:
        if _CLIENT is not None:
            _CLIENT.close()
            _CLIENT = None


def shared_call(action, params=None, timeout=120):
    with _CLIENT_LOCK:
        return _client().call(action, params, timeout=timeout)


def _list(value):
    if isinstance(value, str):
        return [item.strip() for item in value.split(',') if item.strip()]
    return list(value or [])


def shared_http(path, payload=None, method='POST', timeout=120):
    """兼容现有 QMT HTTP 路径，调用方无需了解共享内存协议。"""
    data = dict(payload or {})
    if path == '/api/sys/python_version':
        return shared_call('ping', timeout=timeout)
    if path == '/api/data/stock_list_in_sector':
        return shared_call('stock_list', data, timeout=timeout)
    if path == '/api/check/get_industry_name_of_stock':
        return shared_call('industry_name', data, timeout=timeout)
    if path == '/api/data/full_tick':
        data['stocks'] = _list(data.get('stocks'))
        return shared_call('full_tick', data, timeout=timeout)
    if path == '/api/data/last_volume':
        return shared_call('last_volume', data, timeout=timeout)
    if path == '/api/data/divid_factors':
        return shared_call('divid_factors', data, timeout=timeout)
    if path == '/api/data/instrumentdetail':
        return shared_call('instrument_detail', data, timeout=timeout)
    if path in ('/api/data/market_data_ex', '/api/data/market_data'):
        data['stocks'] = _list(data.get('stock_code') or data.get('stocks'))
        data['fields'] = _list(data.get('fields'))
        action = 'market_data_ex' if path.endswith('_ex') else 'market_data'
        return shared_call(action, data, timeout=timeout)
    if path == '/api/data/financial_data':
        data['fields'] = _list(data.get('fieldList'))
        data['stocks'] = _list(data.get('stockList'))
        data['start_date'] = data.get('startDate', '')
        data['end_date'] = data.get('endDate', '')
        return shared_call('financial_data', data, timeout=timeout)

    trade_actions = {
        '/api/money/total': 'money_total',
        '/api/money/available': 'money_available',
        '/api/holding': 'holding',
        '/api/order/status': 'order_status',
        '/api/order/deal': 'deal',
        '/api/order/buy': 'buy',
        '/api/order/sell': 'sell',
        '/api/order/cancel_by_id': 'cancel_by_id',
        '/api/order/cancel_all': 'cancel_all',
    }
    action = trade_actions.get(path)
    if action:
        data.setdefault('account_id', os.environ.get('ACCOUNT_ID', ''))
        return shared_call(action, data, timeout=timeout)
    raise ValueError('共享内存后端暂未实现接口: %s' % path)


def connect_tip():
    """探活失败时的操作提示。按本文件所在目录推断加载器路径。"""
    parent = os.path.basename(os.path.dirname(os.path.abspath(__file__)))
    loader = f"{parent}/qmt_gateway_loader.py" if parent in ("data_sync", "lib") else "qmt_gateway_loader.py"
    return (
        "请先在已登录的大 QMT 里添加并运行本项目的 %s，"
        "等策略窗口出现「[共享内存数据服务] 已启动」后再回测或拉行情。不使用 15588 端口。"
    ) % loader


def ping(attempts=3, timeout=8):
    last_error = ''
    tries = max(1, int(attempts))
    for attempt in range(tries):
        try:
            body = shared_http('/api/sys/python_version', method='GET', timeout=timeout)
            return {
                'ok': True,
                'base': 'shared-memory://SentinelQmtLoaderV1',
                'python_version': str(body.get('python_version') or '')[:20],
            }
        except Exception as exc:
            last_error = str(exc)[:160]
            if attempt + 1 < tries:
                time.sleep(1)
    return {
        'ok': False,
        'base': 'shared-memory://SentinelQmtLoaderV1',
        'message': last_error,
    }
