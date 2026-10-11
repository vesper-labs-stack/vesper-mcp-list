"""Configuration, conservative SQL policy and MySQL 5.6 read-only execution."""
from __future__ import annotations

import base64
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
import json
import os
from pathlib import Path
import re
from typing import Mapping

from dotenv import dotenv_values
import pymysql
from sqlglot import exp, parse
from sqlglot.errors import SqlglotError
from mysql_ssh import mysql_socket


DEFAULT_ENV = Path(__file__).resolve().with_name('.env')


@dataclass(frozen=True)
class Profile:
    host: str
    user: str
    password: str = field(repr=False)
    database: str
    port: int = 3306
    max_rows: int = 500
    connect_timeout: int = 5
    read_timeout: int = 15
    max_result_bytes: int = 1048576
    ssl_ca: str | None = None
    ssh_host: str | None = None
    ssh_port: int = 22
    ssh_user: str | None = None
    ssh_password: str | None = field(default=None, repr=False)
    ssh_known_hosts: str | None = None


def _integer(values, key, default, low, high):
    try:
        value = int(values.get(key, str(default)))
    except (ValueError, TypeError):
        raise ValueError(f'{key} 必须是整数。') from None
    if not low <= value <= high:
        raise ValueError(f'{key} 必须在 {low} 到 {high} 之间。')
    return value


def load_profiles(path=DEFAULT_ENV, environ: Mapping[str, str] | None = None) -> dict[str, Profile]:
    # Do not interpolate credentials or mutate the host process environment.
    values = dict(dotenv_values(path, interpolate=False))
    values.update(os.environ if environ is None else environ)
    names = [n.strip().lower() for n in (values.get('MYSQL_CONNECTIONS') or '').split(',')]
    if not names or any(not re.fullmatch(r'[a-z][a-z0-9_]*', n) for n in names):
        raise ValueError('请在 .env 中配置 MYSQL_CONNECTIONS，例如 main,report。')
    if len(set(names)) != len(names):
        raise ValueError('MYSQL_CONNECTIONS 中的连接名称不能重复。')
    settings = dict(
        max_rows=_integer(values, 'MYSQL_MAX_ROWS', 500, 1, 10000),
        connect_timeout=_integer(values, 'MYSQL_CONNECT_TIMEOUT', 5, 1, 120),
        read_timeout=_integer(values, 'MYSQL_READ_TIMEOUT', 15, 1, 300),
        max_result_bytes=_integer(values, 'MYSQL_MAX_RESULT_BYTES', 1048576, 1024, 16777216),
    )
    result = {}
    for name in names:
        prefix = f'MYSQL_{name.upper()}_'
        for suffix in ('HOST', 'USER', 'DATABASE'):
            if not values.get(prefix + suffix):
                raise ValueError(f'缺少配置 {prefix + suffix}。')
        if values.get(prefix + 'PASSWORD') is None:
            raise ValueError(f'缺少配置 {prefix}PASSWORD。')
        ssh_host = values.get(prefix + 'SSH_HOST') or None
        ssh_fields = ('SSH_PORT', 'SSH_USER', 'SSH_PASSWORD', 'SSH_KNOWN_HOSTS')
        if not ssh_host and any(values.get(prefix + key) is not None for key in ssh_fields):
            raise ValueError(f'配置了 SSH 参数但缺少 {prefix}SSH_HOST。')
        if ssh_host:
            for key in ('SSH_USER', 'SSH_PASSWORD'):
                if not values.get(prefix + key):
                    raise ValueError(f'缺少配置 {prefix + key}。')
        result[name] = Profile(
            host=values[prefix + 'HOST'], user=values[prefix + 'USER'],
            password=values[prefix + 'PASSWORD'], database=values[prefix + 'DATABASE'],
            port=_integer(values, prefix + 'PORT', 3306, 1, 65535),
            ssl_ca=values.get(prefix + 'SSL_CA') or None, **settings,
            ssh_host=ssh_host,
            ssh_port=_integer(values, prefix + 'SSH_PORT', 22, 1, 65535),
            ssh_user=values.get(prefix + 'SSH_USER'),
            ssh_password=values.get(prefix + 'SSH_PASSWORD'),
            ssh_known_hosts=values.get(prefix + 'SSH_KNOWN_HOSTS') or None,
        )
    return result


# SQLGlot normalizes some MySQL functions to portable AST function names.
SAFE_FUNCTIONS = set('''COUNT SUM AVG MIN MAX GROUP_CONCAT ABS CEIL FLOOR ROUND
    MOD POW POWER SQRT SIGN EXP LN LOG LOG2 LOG10 CONCAT CONCAT_WS LENGTH
    CHAR_LENGTH CHARACTER_LENGTH LOWER UPPER TRIM LTRIM RTRIM SUBSTRING LEFT RIGHT
    REPLACE REPEAT REVERSE LPAD RPAD LOCATE INSTR STR_POSITION COALESCE IF IFNULL
    NULLIF CASE CAST TRY_CAST EXTRACT YEAR MONTH DAY DAY_OF_MONTH DAY_OF_WEEK
    DAY_OF_YEAR HOUR MINUTE SECOND WEEK WEEK_OF_YEAR DATE TIME DATE_ADD DATE_SUB
    DATE_DIFF DATEDIFF TIMESTAMPDIFF TIMESTAMP_DIFF TIME_TO_STR STR_TO_TIME
    STR_TO_DATE DATE_FORMAT CURRENT_DATE CURRENT_TIME CURRENT_TIMESTAMP
    NOW CURDATE CURTIME UNIX_TIMESTAMP FROM_UNIXTIME UNIX_TO_TIME VERSION
    DATABASE SCHEMA USER CURRENT_USER SESSION_USER CONNECTION_ID
    GREATEST LEAST ASCII HEX UNHEX BIT_LENGTH OCTET_LENGTH'''.split())


def validate_sql(sql: str) -> str:
    if not isinstance(sql, str) or not sql.strip() or len(sql) > 100000:
        raise ValueError('只允许单条 SELECT 查询，SQL 不能为空或超过 100000 字符。')
    # Conservative rejection also inside literals: executable comments and variables
    # have subtle dialect behavior and are unnecessary for this interface.
    if any(token in sql for token in ('/*!', '/*M!', '/*m!', '@', '\x00')):
        raise ValueError('SELECT 查询不允许变量或可执行注释。')
    try:
        statements = parse(sql, read='mysql')
    except SqlglotError:
        raise ValueError('SQL 无法安全解析；只允许单条 SELECT 查询。') from None
    if len(statements) != 1 or not isinstance(statements[0], (exp.Select, exp.Union)):
        raise ValueError('只允许单条 SELECT 查询（可包含 UNION）。')
    tree = statements[0]
    _validate_tree(tree)
    try:
        normalized = tree.sql(dialect='mysql', comments=False, unsupported_level='RAISE')
        generated = parse(normalized, read='mysql')
        if len(generated) != 1:
            raise ValueError('SELECT 转换后包含多条语句。')
        _validate_tree(generated[0])
        return normalized
    except SqlglotError:
        raise ValueError('SELECT 包含不支持的 MySQL 语法。') from None


def _validate_tree(tree):
    if not isinstance(tree, (exp.Select, exp.Union)):
        raise ValueError('只允许单条 SELECT 查询。')
    forbidden = (exp.Into, exp.Lock, exp.With, exp.Command, exp.DDL, exp.DML,
                 exp.Parameter, exp.Placeholder, exp.SessionParameter, exp.Window)
    for node in tree.walk():
        if isinstance(node, forbidden):
            raise ValueError('SELECT 不允许写入、锁定、WITH、变量或窗口函数。')
        if isinstance(node, exp.Dot) and isinstance(node.expression, exp.Func):
            raise ValueError('SELECT 不允许调用带数据库前缀的函数。')
        if isinstance(node, exp.Func):
            name = node.name.upper() if isinstance(node, exp.Anonymous) else node.sql_name()
            if name not in SAFE_FUNCTIONS:
                raise ValueError(f'SELECT 函数 {name} 不在只读白名单中。')


def _json_value(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, timedelta):
        return str(value)
    if isinstance(value, bytes):
        return {'base64': base64.b64encode(value).decode('ascii')}
    return value


class QueryService:
    def __init__(self, env_path=DEFAULT_ENV, *, environ=None, connect=None):
        self.env_path = env_path
        self.environ = environ
        self.connect = connect or pymysql.connect
        self._profiles = None

    @property
    def profiles(self):
        if self._profiles is None:
            self._profiles = load_profiles(self.env_path, self.environ)
        return self._profiles

    def _profile(self, name):
        name = name.lower()
        if name not in self.profiles:
            raise ValueError('未知连接名称，请先调用 list_connections。')
        return self.profiles[name]

    def query(self, name: str, sql: str, max_rows: int | None = None):
        normalized = validate_sql(sql)  # Reject unsafe SQL even before loading credentials.
        profile = self._profile(name)
        count = profile.max_rows if max_rows is None else max_rows
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= profile.max_rows:
            raise ValueError(f'max_rows 必须在 1 到 {profile.max_rows} 之间。')
        tree = parse(normalized, read='mysql')[0]
        limit = tree.args.get('limit')
        existing = limit.expression if limit else None
        if existing is not None:
            if not isinstance(existing, exp.Literal) or not existing.is_int:
                raise ValueError('LIMIT 必须是非负整数。')
            wanted = int(existing.this)
            if wanted < 0:
                raise ValueError('LIMIT 必须是非负整数。')
            fetch_limit = min(wanted, count + 1)
        else:
            fetch_limit = count + 1
        bounded = validate_sql(tree.limit(fetch_limit).sql(dialect='mysql', comments=False))
        return self._execute(profile, bounded, None, count)

    def _execute(self, profile, sql, params, count):
        connection = None
        tunnel = ExitStack()
        try:
            forwarded = tunnel.enter_context(mysql_socket(profile)) if profile.ssh_host else None
            tls = {'ssl_ca': profile.ssl_ca, 'ssl_verify_cert': True,
                   'ssl_verify_identity': True} if profile.ssl_ca else {}
            connection = self.connect(
                host=profile.host, port=profile.port, user=profile.user,
                password=profile.password, database=profile.database,
                charset='utf8mb4', autocommit=False, local_infile=False,
                connect_timeout=profile.connect_timeout,
                read_timeout=profile.read_timeout, write_timeout=profile.read_timeout,
                # Normalize quoting semantics to match the MySQL SQL generator.
                init_command="SET SESSION sql_mode = 'STRICT_TRANS_TABLES,NO_ENGINE_SUBSTITUTION'",
                defer_connect=bool(profile.ssh_host),
                **tls,
            )
            if forwarded is not None:
                connection.connect(sock=forwarded)
            with connection.cursor() as cursor:
                cursor.execute('START TRANSACTION READ ONLY')
                cursor.execute(f'SET SESSION SQL_SELECT_LIMIT = {count + 1}')
                cursor.execute(sql, params)
                columns = [column[0] for column in cursor.description]
                raw_rows = cursor.fetchmany(count + 1)
                result = {'columns': columns, 'rows': [], 'row_count': 0,
                          'truncated': len(raw_rows) > count}
                used = len(json.dumps(result, ensure_ascii=False).encode('utf-8'))
                if used > profile.max_result_bytes:
                    raise ValueError('列信息超过返回字节限制，请选择更少的列。')
                for row in raw_rows[:count]:
                    converted = [_json_value(value) for value in row]
                    size = len(json.dumps(converted, ensure_ascii=False).encode('utf-8')) + 2
                    if used + size + 20 > profile.max_result_bytes:
                        result['truncated'] = True
                        break
                    result['rows'].append(converted)
                    used += size
                result['row_count'] = len(result['rows'])
                return result
        except pymysql.MySQLError as error:
            code = error.args[0] if error.args and isinstance(error.args[0], int) else 'unknown'
            raise ValueError(f'MySQL 查询失败（错误码 {code}），请检查连接、权限或 SQL。') from None
        except (OSError, UnicodeError):
            raise ValueError('数据库连接、TLS 或字符编码失败，请检查本地配置。') from None
        finally:
            try:
                if connection is not None:
                    try:
                        connection.rollback()
                    except pymysql.MySQLError:
                        pass
                    finally:
                        connection.close()
            finally:
                tunnel.close()

    def list_tables(self, name):
        profile = self._profile(name)
        return self._execute(profile,
            'SELECT TABLE_NAME, TABLE_TYPE, TABLE_COMMENT FROM information_schema.TABLES '
            'WHERE TABLE_SCHEMA = %s ORDER BY TABLE_NAME LIMIT %s',
            (profile.database, profile.max_rows + 1), profile.max_rows)

    def describe_table(self, name, table):
        if not table or len(table) > 64:
            raise ValueError('table 必须是 1 到 64 字符的表名。')
        profile = self._profile(name)
        return self._execute(profile,
            'SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_DEFAULT, COLUMN_KEY, '
            'EXTRA, COLUMN_COMMENT FROM information_schema.COLUMNS '
            'WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s ORDER BY ORDINAL_POSITION LIMIT %s',
            (profile.database, table, profile.max_rows + 1), profile.max_rows)
