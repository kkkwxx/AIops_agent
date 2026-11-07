"""工具模块 - 供 Agent 调用的各种工具"""

from app.tools.knowledge_tool import retrieve_knowledge
from app.tools.logging_tools import (
    aggregate_log_errors,
    get_log_context,
    query_logs,
    search_log_streams,
)
from app.tools.monitoring_tools import (
    advanced_promql_query,
    get_target_health,
    query_metrics,
    search_metrics,
)
from app.tools.time_tool import get_current_time

# 本地基础工具全集列表 (包含知识库检索、时间工具、受控监控工具包与受控日志工具包)
LOCAL_TOOLS = [
    get_current_time,
    retrieve_knowledge,
    # 监控工具集
    search_metrics,
    query_metrics,
    get_target_health,
    advanced_promql_query,
    # 日志工具集
    search_log_streams,
    query_logs,
    get_log_context,
    aggregate_log_errors,
]

__all__ = [
    "retrieve_knowledge",
    "get_current_time",
    "search_metrics",
    "query_metrics",
    "get_target_health",
    "advanced_promql_query",
    "search_log_streams",
    "query_logs",
    "get_log_context",
    "aggregate_log_errors",
    "LOCAL_TOOLS",
]
