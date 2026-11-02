# -*- coding: utf-8 -*-
# Author：@跟陈博士学AI
# 数据库配置 -- wucai_trade.*，读取 CASE-AI 项目根目录 .env（与其它模块同一路径）
"""
环境变量: WUCAI_SQL_HOST / WUCAI_SQL_PORT / WUCAI_SQL_USERNAME / WUCAI_SQL_PASSWORD / WUCAI_SQL_DB
"""
import os
from pathlib import Path

import pymysql
from dotenv import dotenv_values

_ENV_FILE = Path(__file__).resolve().parent.parent.parent / ".env"
_env = dotenv_values(_ENV_FILE)

DB_CONFIG = {
    "host":     _env.get("WUCAI_SQL_HOST", "localhost"),
    "user":     _env.get("WUCAI_SQL_USERNAME", "root"),
    "password": os.environ.get('WUCAI_SQL_PASSWORD', ''),
    "database": _env.get("WUCAI_SQL_DB", "wucai_trade"),
    "port":     int(_env.get("WUCAI_SQL_PORT", "3306")),
    "charset":  "utf8mb4",
}


def get_connection():
    return pymysql.connect(**DB_CONFIG)


def execute_query(sql, params=None):
    conn = get_connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute(sql, params or ())
        return cursor.fetchall()
    finally:
        conn.close()
