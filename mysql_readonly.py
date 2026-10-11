"""MySQL 5.6 read-only stdio MCP. Credentials are loaded from adjacent .env."""
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from mysql_service import QueryService


mcp = FastMCP('mysql_readonly', instructions=(
    '先调用 list_connections 选择连接，再用 list_tables 和 describe_table 查看结构。'
    'query_sql 只接受单条 SELECT/UNION，兼容 MySQL 5.6；请使用过滤条件和 LIMIT。'
    '不支持写入、DDL、CALL、SHOW、WITH、锁定或非白名单函数。truncated 表示返回被截断。'
))
service = QueryService()
read_only = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True)


@mcp.tool(annotations=read_only)
def list_connections() -> dict:
    """列出 .env 中配置的连接名称；不返回地址、用户名或密码。"""
    return {'connections': list(service.profiles)}


@mcp.tool(annotations=read_only)
def query_sql(connection: str, sql: str, max_rows: int | None = None) -> dict:
    """在指定连接执行单条只读 SELECT；返回列名、行、行数和截断标记。"""
    return service.query(connection, sql, max_rows)


@mcp.tool(annotations=read_only)
def list_tables(connection: str) -> dict:
    """查看指定连接默认数据库的表、视图及注释。"""
    return service.list_tables(connection)


@mcp.tool(annotations=read_only)
def describe_table(connection: str, table: str) -> dict:
    """查看默认数据库中指定表的字段、类型、可空性、默认值、键和注释。"""
    return service.describe_table(connection, table)


if __name__ == '__main__':
    mcp.run(transport='stdio')
