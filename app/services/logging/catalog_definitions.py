"""日志流目录 (Log Stream Catalog) 与元数据定义

基于日志流目录 (Log Stream Catalog) 与受控 LogQL 模板引擎的设计：
1. Agent 面向业务逻辑流 ID (如 container.postgres, system.syslog) 发起查询
2. Catalog 记录日志流的基础 Selector、文本/JSON格式、日志级别存储形式
3. 彻底避免 Agent 瞎猜不存在的 Label，并解决不同日志中 Level 存放位置不一的问题
"""

from enum import Enum
from typing import Dict, List, Optional
from pydantic import BaseModel, Field


class LogFormat(str, Enum):
    """日志正文格式"""
    RAW_TEXT = "text"      # 纯文本 / Syslog
    JSON = "json"          # 结构化 JSON 日志
    LOGFMT = "logfmt"      # key=value 键值对日志


class StreamStatus(str, Enum):
    """日志流动态活性状态"""
    ACTIVE = "active"      # 近期持续有日志上报
    SILENT = "silent"      # 近期静默无新日志 (低频或无请求，不代表链路故障)
    UNKNOWN = "unknown"    # 无法验证或采集端不可达


class LogStreamDefinition(BaseModel):
    """日志流元数据模型"""
    id: str = Field(..., description="标准逻辑流 ID，如 container.postgres")
    name: str = Field(..., description="日志流中文名称")
    description: str = Field(..., description="业务含义与排障场景说明")
    resource_type: str = Field(..., description="资源分类: container / system / app")
    selector: Dict[str, str] = Field(..., description="Loki 基础 Label 过滤集合，如 {'job': 'docker', 'container': '...'}")
    format: LogFormat = Field(default=LogFormat.RAW_TEXT, description="日志正文格式 (text / json / logfmt)")
    level_in_label: bool = Field(default=False, description="日志级别是否已被提升为独立 Label (如 {level='error'})")
    field_mapping: Dict[str, str] = Field(default_factory=dict, description="JSON/Logfmt 字段映射 (如 {'level': 'level', 'message': 'msg'})")
    capabilities: List[str] = Field(
        default_factory=lambda: ["start_time", "end_time", "level", "keywords", "limit"],
        description="该日志流支持的过滤维度"
    )


# ============================================================
# 预置标准日志流定义集合
# ============================================================

BUILTIN_LOG_STREAMS: List[LogStreamDefinition] = [
    LogStreamDefinition(
        id="system.syslog",
        name="系统核心运行日志 (Syslog)",
        description="宿主机 /var/log/messages 系统日志，记录内核事件、NetworkManager 状态、dockerd 守护进程等底层系统异常。",
        resource_type="system",
        selector={"job": "syslog", "server": "biz-server"},
        format=LogFormat.RAW_TEXT,
        level_in_label=False,
    ),
    LogStreamDefinition(
        id="container.postgres",
        name="PostgreSQL 数据库容器日志",
        description="业务知识库与持久化状态向量数据库容器运行日志，排查 SQL 执行超时、死锁与连接中断等问题。",
        resource_type="container",
        selector={"job": "docker", "container": "super-biz-postgres"},
        format=LogFormat.RAW_TEXT,
        level_in_label=False,
    ),
    LogStreamDefinition(
        id="container.promtail",
        name="Promtail 日志采集端自身日志",
        description="日志采集代理自身状态日志，排查目标文件监听断流、采集规则解析异常与远程推送失败等问题。",
        resource_type="container",
        selector={"job": "docker", "container": "promtail"},
        format=LogFormat.RAW_TEXT,
        level_in_label=False,
    ),
    LogStreamDefinition(
        id="container.loki",
        name="Loki 日志引擎核心日志",
        description="Loki 日志存储与查询引擎运行日志，排查查询超时、索引刷盘与限流拒绝等服务端状态。",
        resource_type="container",
        selector={"job": "docker", "container": "loki"},
        format=LogFormat.RAW_TEXT,
        level_in_label=False,
    ),
    LogStreamDefinition(
        id="container.node_exporter",
        name="Node Exporter 监控端日志",
        description="主机硬件指标采集端日志，排查采集端权限拒绝、挂载点无响应等监控断连问题。",
        resource_type="container",
        selector={"job": "docker", "container": "node_exporter"},
        format=LogFormat.RAW_TEXT,
        level_in_label=False,
    ),
    LogStreamDefinition(
        id="app.service.backend",
        name="业务核心后端微服务日志 (预留)",
        description="核心业务微服务标准运行日志，包含全链路请求轨迹、业务异常报错与数据库连接池状态。",
        resource_type="app",
        selector={"job": "docker", "container": "backend"},
        format=LogFormat.JSON,
        level_in_label=False,
        field_mapping={"level": "level", "message": "message", "timestamp": "timestamp"},
    ),
]

