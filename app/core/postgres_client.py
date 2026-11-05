"""PostgreSQL 核心客户端管理模块
职责：纯粹的连接池生命周期管理、向量类型自动注册与健康探测（零 DDL 字符串污染）
"""

from typing import Optional
from psycopg_pool import AsyncConnectionPool, ConnectionPool
from psycopg.rows import dict_row
from pgvector.psycopg import register_vector, register_vector_async
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from loguru import logger

from app.config import config


async def _configure_async_connection(conn):
    """异步连接初始化钩子：自动注册 pgvector 类型编解码器"""
    await register_vector_async(conn)


def _configure_sync_connection(conn):
    """同步连接初始化钩子：自动注册 pgvector 类型编解码器"""
    register_vector(conn)


class PostgresManager:
    """PostgreSQL 连接池与生命周期管理器 (单例模式)"""

    def __init__(self):
        self._async_pool: Optional[AsyncConnectionPool] = None
        self._sync_pool: Optional[ConnectionPool] = None
        self._checkpointer_pool: Optional[AsyncConnectionPool] = None
        self._checkpointer: Optional[AsyncPostgresSaver] = None
        self._conn_string = config.database.postgres.conn_string

    def get_sync_pool(self) -> ConnectionPool:
        """获取同步连接池"""
        if self._sync_pool is None or self._sync_pool.closed:
            self._sync_pool = ConnectionPool(
                conninfo=self._conn_string,
                min_size=config.database.postgres.pool_min_size,
                max_size=config.database.postgres.pool_max_size,
                configure=_configure_sync_connection,
                open=True,
            )
        return self._sync_pool

    async def get_async_pool(self) -> AsyncConnectionPool:
        """获取异步连接池（供 LangGraph Checkpointer 与 FastAPI 异步链路使用）"""
        if self._async_pool is None or self._async_pool.closed:
            self._async_pool = AsyncConnectionPool(
                conninfo=self._conn_string,
                min_size=config.database.postgres.pool_min_size,
                max_size=config.database.postgres.pool_max_size,
                configure=_configure_async_connection,
                open=False,
            )
            await self._async_pool.open()
        return self._async_pool

    async def get_checkpointer(self) -> AsyncPostgresSaver:
        """获取基于专用异步连接池的 LangGraph AsyncPostgresSaver 实例 (单例模式)"""
        if self._checkpointer is None:
            if self._checkpointer_pool is None or self._checkpointer_pool.closed:
                self._checkpointer_pool = AsyncConnectionPool(
                    conninfo=self._conn_string,
                    min_size=config.database.postgres.pool_min_size,
                    max_size=config.database.postgres.pool_max_size,
                    kwargs={
                        "autocommit": True,
                        "prepare_threshold": 0,
                        "row_factory": dict_row,
                    },
                    open=False,
                )
                await self._checkpointer_pool.open()
            self._checkpointer = AsyncPostgresSaver(conn=self._checkpointer_pool)
            await self._checkpointer.setup()
            logger.info("✅ PostgreSQL AsyncPostgresSaver 已就绪并完成 setup")
        return self._checkpointer

    async def health_check(self) -> bool:
        """数据库连通性健康探测 (执行极轻量的 SELECT 1)"""
        try:
            pool = await self.get_async_pool()
            async with pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("SELECT 1;")
                    res = await cur.fetchone()
                    return res is not None and res[0] == 1
        except Exception as e:
            logger.warning(f"PostgreSQL 健康探测失败: {e}")
            return False

    async def close(self):
        """关闭所有连接池 (FastAPI 优雅退出时调用)"""
        if self._checkpointer_pool and not self._checkpointer_pool.closed:
            await self._checkpointer_pool.close()
            logger.info("🔌 PostgreSQL Checkpointer 异步连接池已安全关闭")
        if self._async_pool and not self._async_pool.closed:
            await self._async_pool.close()
            logger.info("🔌 PostgreSQL 异步连接池已安全关闭")
        if self._sync_pool and not self._sync_pool.closed:
            self._sync_pool.close()
            logger.info("🔌 PostgreSQL 同步连接池已安全关闭")


# 全局单例
postgres_manager = PostgresManager()

