#!/usr/bin/env python3
"""数据库初始化脚本 (仅首次建表或更新 Schema 时执行)
读取 scripts/schema.sql 并在 PostgreSQL 中执行
"""

import asyncio
from pathlib import Path
from loguru import logger
import psycopg

from app.config import config


async def main():
    sql_path = Path(__file__).parent / "schema.sql"
    if not sql_path.exists():
        logger.error(f"❌ 找不到初始化 SQL 文件: {sql_path}")
        return

    logger.info(f"📖 正在读取 SQL 文件: {sql_path}")
    with open(sql_path, "r", encoding="utf-8") as f:
        schema_sql = f.read()

    conn_string = config.database.postgres.conn_string
    logger.info(f"🔌 正在连接数据库并执行 DDL: {config.database.postgres.host}:{config.database.postgres.port}/{config.database.postgres.db}")

    try:
        # 使用原生异步连接执行完整的 schema.sql
        async with await psycopg.AsyncConnection.connect(conn_string, autocommit=True) as conn:
            async with conn.cursor() as cur:
                await cur.execute(schema_sql)
        logger.info("✅ 数据库表结构、pgvector 扩展与索引初始化成功！")
    except Exception as e:
        logger.error(f"❌ 数据库初始化失败: {e}")
        raise


if __name__ == "__main__":
    asyncio.run(main())

