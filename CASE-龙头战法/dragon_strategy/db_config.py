# -*- coding: utf-8 -*-
# Author：@跟陈博士学AI
# 21-CASE-A: 数据库配置
"""
数据库配置 -- 从 .env 读取 MySQL 连接

环境变量约定:
    WUCAI_SQL_HOST       MySQL 主机
    WUCAI_SQL_PORT       端口, 默认 3306
    WUCAI_SQL_USERNAME   用户名
    WUCAI_SQL_PASSWORD   密码
    WUCAI_SQL_DB         数据库名, 默认 wucai_trade
"""
import os
from pathlib import Path
import pymysql
from dotenv import dotenv_values

# 优先读 CASE 根目录 .env（dragon_strategy/db_config.py -> CASE-龙头战法/.env），
# 退回到本目录 .env 以兼容旧用法
_root_env = Path(__file__).resolve().parent.parent / '.env'
_local_env = Path(__file__).parent / '.env'
_env_path = _root_env if _root_env.exists() else _local_env
_env = dotenv_values(_env_path)


# 数据库配置
DB_CONFIG = {
    'host':     _env.get('WUCAI_SQL_HOST', 'localhost'),
    'user':     _env.get('WUCAI_SQL_USERNAME', 'root'),
    'password': os.environ.get('WUCAI_SQL_PASSWORD', ''),
    'database': _env.get('WUCAI_SQL_DB', 'wucai_trade'),
    'port':     int(_env.get('WUCAI_SQL_PORT', '3306')),
    'charset':  'utf8mb4',
}


def get_connection():
    """获取一个 MySQL 连接 (调用方负责 close)"""
    return pymysql.connect(**DB_CONFIG)


def execute_query(sql, params=None):
    """执行 SELECT, 返回 List[Dict]"""
    conn = get_connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute(sql, params or ())
        result = cursor.fetchall()
        cursor.close()
        return result
    finally:
        conn.close()


def execute_update(sql, params=None):
    """执行 INSERT / UPDATE / DELETE / DDL, 返回受影响行数"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        n = cursor.execute(sql, params or ())
        conn.commit()
        cursor.close()
        return n
    finally:
        conn.close()


def execute_many(sql, rows):
    """批量 INSERT / UPDATE, 自动分批避免单次 packet 过大"""
    if not rows:
        return 0
    conn = get_connection()
    try:
        cursor = conn.cursor()
        # 每批 1000 行
        batch_size = 1000
        total = 0
        for i in range(0, len(rows), batch_size):
            batch = rows[i:i + batch_size]
            n = cursor.executemany(sql, batch)
            total += n
        conn.commit()
        cursor.close()
        return total
    finally:
        conn.close()
