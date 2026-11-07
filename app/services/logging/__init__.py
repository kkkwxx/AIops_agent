"""统一日志核心服务层 (Unified Logging Service)

包含四个核心子模块：
- catalog_definitions: 日志流元数据与模型定义
- client: Loki 异步 HTTP 客户端
- catalog: 日志流目录 (Log Stream Catalog) 与受控 LogQL 模板引擎
- formatter: 日志响应格式化与前后文提取
- gateway: 日志安全网关与治理调度器 (Logging Gateway)
"""

from .catalog_definitions import (
    BUILTIN_LOG_STREAMS,
    LogFormat,
    LogStreamDefinition,
    StreamStatus,
)
from .catalog import LogStreamCatalog, log_stream_catalog
from .client import (
    LokiClient,
    LokiConnectionError,
    LokiError,
    LokiQueryError,
    loki_client,
)
from .formatter import LogResultFormatter
from .gateway import LoggingGateway, logging_gateway

__all__ = [
    "BUILTIN_LOG_STREAMS",
    "LogFormat",
    "LogStreamDefinition",
    "StreamStatus",
    "LogStreamCatalog",
    "log_stream_catalog",
    "LokiClient",
    "LokiError",
    "LokiConnectionError",
    "LokiQueryError",
    "loki_client",
    "LogResultFormatter",
    "LoggingGateway",
    "logging_gateway",
]

