"""监控指标目录与 PromQL 查询模板定义

基于指标目录 (Metric Catalog) 与查询模板 (Query Templates) 的抽象设计：
1. Agent 面向逻辑指标 ID (如 node.cpu.usage, node.memory.used) 发起查询
2. 模板引擎负责把逻辑 ID 与业务标签 (filters) 安全渲染为经过验证的合法 PromQL
3. 消除不同 Exporter 指标命名不一致的差异，杜绝大模型臆造 PromQL 语法
"""

from enum import Enum
from typing import Dict, List, Optional
from pydantic import BaseModel, Field


class ResourceType(str, Enum):
    """监控资源类别"""
    NODE = "node"            # 主机/宿主机/物理机/虚拟机
    CONTAINER = "container"  # 容器 (Docker / cAdvisor)
    POD = "pod"              # K8s Pod / Workload
    APP = "app"              # 应用程序/微服务内部指标 (Go/JVM/Process)
    EXPORTER = "exporter"    # 监控采集端自身


class QueryMode(str, Enum):
    """指标查询模式"""
    INSTANT = "instant"      # 瞬时单值查询 (单点探活、当前阈值判定)
    RANGE = "range"          # 区间时序走势 (均值、峰值、拐点分析)


class MetricDefinition(BaseModel):
    """指标元数据定义模型"""
    id: str = Field(..., description="标准逻辑指标 ID，例如 node.cpu.usage")
    name: str = Field(..., description="指标人类可读中文名称")
    description: str = Field(..., description="指标业务含义与排障指导说明")
    resource_type: ResourceType = Field(..., description="适用的资源分类")
    unit: str = Field(..., description="指标度量单位，如 %, Bytes, Bytes/s, 个, 秒")
    exporter: str = Field(..., description="产生该指标的 Exporter，如 node_exporter, custom_exporter")
    metric_names: List[str] = Field(default_factory=list, description="涉及的原生 Prometheus 指标名")
    required_labels: List[str] = Field(default_factory=list, description="强制要求的过滤标签名")
    optional_labels: List[str] = Field(default_factory=list, description="支持的可选过滤标签名")
    
    # 核心 PromQL 模板 (支持 {{labels}} 动态占位符注入)
    template_instant: str = Field(..., description="瞬时查询 PromQL 模板")
    template_range: str = Field(..., description="区间查询 PromQL 模板")
    supported_range: bool = Field(default=True, description="是否支持区间时序分析")
    threshold_warning: Optional[float] = Field(None, description="参考预警阈值")
    threshold_critical: Optional[float] = Field(None, description="参考严重阈值")


# ============================================================
# 首批生产级内置指标目录 (Metric Catalog)
# ============================================================

BUILTIN_METRICS: List[MetricDefinition] = [
    # ------------------------------------------------------------
    # 1. 主机/节点指标 (Node Exporter)
    # ------------------------------------------------------------
    MetricDefinition(
        id="node.cpu.usage",
        name="主机 CPU 使用率",
        description="主机所有 CPU 核心的整体实时/区间使用率，0~100 百分比。常用于排查 CPU 飙高与突发负载。",
        resource_type=ResourceType.NODE,
        unit="%",
        exporter="node_exporter",
        metric_names=["node_cpu_seconds_total"],
        required_labels=[],
        optional_labels=["instance"],
        template_instant='100 - (avg by (instance) (rate(node_cpu_seconds_total{mode="idle", {labels}}[1m])) * 100)',
        template_range='100 - (avg by (instance) (rate(node_cpu_seconds_total{mode="idle", {labels}}[1m])) * 100)',
        threshold_warning=75.0,
        threshold_critical=90.0,
    ),
    MetricDefinition(
        id="node.cpu.load1",
        name="主机 1 分钟平均负载",
        description="系统过去 1 分钟内的平均运行队列长度/活跃任务数。通常超过 CPU 逻辑核心数即表明排队堆积。",
        resource_type=ResourceType.NODE,
        unit="数值",
        exporter="node_exporter",
        metric_names=["node_load1"],
        required_labels=[],
        optional_labels=["instance"],
        template_instant="node_load1{{labels}}",
        template_range="node_load1{{labels}}",
    ),
    MetricDefinition(
        id="node.cpu.load5",
        name="主机 5 分钟平均负载",
        description="系统过去 5 分钟内的平均负载，用于判断负载是短期突刺还是持续性压力。",
        resource_type=ResourceType.NODE,
        unit="数值",
        exporter="node_exporter",
        metric_names=["node_load5"],
        required_labels=[],
        optional_labels=["instance"],
        template_instant="node_load5{{labels}}",
        template_range="node_load5{{labels}}",
    ),
    MetricDefinition(
        id="node.cpu.load15",
        name="主机 15 分钟平均负载",
        description="系统过去 15 分钟内的长周期平均负载，用于宏观评估系统整体水位。",
        resource_type=ResourceType.NODE,
        unit="数值",
        exporter="node_exporter",
        metric_names=["node_load15"],
        required_labels=[],
        optional_labels=["instance"],
        template_instant="node_load15{{labels}}",
        template_range="node_load15{{labels}}",
    ),
    MetricDefinition(
        id="node.memory.usage",
        name="主机内存使用率",
        description="主机实际物理内存使用百分比 (排除可用缓存)，0~100 百分比。超过 85% 容易诱发内核 OOM Killer 杀进程。",
        resource_type=ResourceType.NODE,
        unit="%",
        exporter="node_exporter",
        metric_names=["node_memory_MemTotal_bytes", "node_memory_MemAvailable_bytes"],
        required_labels=[],
        optional_labels=["instance"],
        template_instant="(1 - (node_memory_MemAvailable_bytes{{labels}} / node_memory_MemTotal_bytes{{labels}})) * 100",
        template_range="(1 - (node_memory_MemAvailable_bytes{{labels}} / node_memory_MemTotal_bytes{{labels}})) * 100",
        threshold_warning=80.0,
        threshold_critical=90.0,
    ),
    MetricDefinition(
        id="node.memory.used",
        name="主机已用内存字节数",
        description="主机当前已消耗的物理内存大小，单位字节 (Bytes)。",
        resource_type=ResourceType.NODE,
        unit="Bytes",
        exporter="node_exporter",
        metric_names=["node_memory_MemTotal_bytes", "node_memory_MemAvailable_bytes"],
        required_labels=[],
        optional_labels=["instance"],
        template_instant="node_memory_MemTotal_bytes{{labels}} - node_memory_MemAvailable_bytes{{labels}}",
        template_range="node_memory_MemTotal_bytes{{labels}} - node_memory_MemAvailable_bytes{{labels}}",
    ),
    MetricDefinition(
        id="node.memory.total",
        name="主机物理内存总量",
        description="主机配置的物理内存总量，单位字节 (Bytes)。",
        resource_type=ResourceType.NODE,
        unit="Bytes",
        exporter="node_exporter",
        metric_names=["node_memory_MemTotal_bytes"],
        required_labels=[],
        optional_labels=["instance"],
        template_instant="node_memory_MemTotal_bytes{{labels}}",
        template_range="node_memory_MemTotal_bytes{{labels}}",
        supported_range=False,
    ),
    MetricDefinition(
        id="node.disk.usage",
        name="磁盘分区使用率",
        description="主机根分区或业务数据分区的已用空间占比，排除 tmpfs/overlay 等临时挂载点。超过 90% 需紧急清理日志。",
        resource_type=ResourceType.NODE,
        unit="%",
        exporter="node_exporter",
        metric_names=["node_filesystem_size_bytes", "node_filesystem_avail_bytes"],
        required_labels=[],
        optional_labels=["instance", "mountpoint"],
        template_instant='(1 - (node_filesystem_avail_bytes{fstype!~"tmpfs|overlay|iso9660", {labels}} / node_filesystem_size_bytes{fstype!~"tmpfs|overlay|iso9660", {labels}})) * 100',
        template_range='(1 - (node_filesystem_avail_bytes{fstype!~"tmpfs|overlay|iso9660", {labels}} / node_filesystem_size_bytes{fstype!~"tmpfs|overlay|iso9660", {labels}})) * 100',
        threshold_warning=85.0,
        threshold_critical=92.0,
    ),
    MetricDefinition(
        id="node.disk.io_read_rate",
        name="磁盘读取速率",
        description="主机各物理磁盘设备每秒读取的字节数 (Bytes/s)，排查是否有大文件全表扫描或大量文件读取。",
        resource_type=ResourceType.NODE,
        unit="Bytes/s",
        exporter="node_exporter",
        metric_names=["node_disk_read_bytes_total"],
        required_labels=[],
        optional_labels=["instance", "device"],
        template_instant="sum by (instance, device) (rate(node_disk_read_bytes_total{{labels}}[1m]))",
        template_range="sum by (instance, device) (rate(node_disk_read_bytes_total{{labels}}[1m]))",
    ),
    MetricDefinition(
        id="node.disk.io_write_rate",
        name="磁盘写入速率",
        description="主机各物理磁盘设备每秒写入的字节数 (Bytes/s)，排查日志狂刷、数据库刷脏页或大量写盘导致的 IO 瓶颈。",
        resource_type=ResourceType.NODE,
        unit="Bytes/s",
        exporter="node_exporter",
        metric_names=["node_disk_written_bytes_total"],
        required_labels=[],
        optional_labels=["instance", "device"],
        template_instant="sum by (instance, device) (rate(node_disk_written_bytes_total{{labels}}[1m]))",
        template_range="sum by (instance, device) (rate(node_disk_written_bytes_total{{labels}}[1m]))",
    ),
    MetricDefinition(
        id="node.net.receive_rate",
        name="网络入网速率",
        description="网卡每秒接收流量 (Bytes/s)，已自动排除回环 lo 和 docker 虚拟网桥。用于分析入向网络拥塞与突发请求。",
        resource_type=ResourceType.NODE,
        unit="Bytes/s",
        exporter="node_exporter",
        metric_names=["node_network_receive_bytes_total"],
        required_labels=[],
        optional_labels=["instance", "device"],
        template_instant='sum by (instance, device) (rate(node_network_receive_bytes_total{device!~"lo|docker.*|br.*|veth.*", {labels}}[1m]))',
        template_range='sum by (instance, device) (rate(node_network_receive_bytes_total{device!~"lo|docker.*|br.*|veth.*", {labels}}[1m]))',
    ),
    MetricDefinition(
        id="node.net.transmit_rate",
        name="网络出网速率",
        description="网卡每秒发送流量 (Bytes/s)，用于排查出向打满、大文件下载或响应数据激增导致的网卡丢包。",
        resource_type=ResourceType.NODE,
        unit="Bytes/s",
        exporter="node_exporter",
        metric_names=["node_network_transmit_bytes_total"],
        required_labels=[],
        optional_labels=["instance", "device"],
        template_instant='sum by (instance, device) (rate(node_network_transmit_bytes_total{device!~"lo|docker.*|br.*|veth.*", {labels}}[1m]))',
        template_range='sum by (instance, device) (rate(node_network_transmit_bytes_total{device!~"lo|docker.*|br.*|veth.*", {labels}}[1m]))',
    ),
    MetricDefinition(
        id="node.net.drop_errors",
        name="网络丢包与错误速率",
        description="网卡接收端每秒丢弃/错误的数据包数。正常应为 0，若持续大于 0 表明网卡缓冲区溢出或底层物理网络抖动。",
        resource_type=ResourceType.NODE,
        unit="包/秒",
        exporter="node_exporter",
        metric_names=["node_network_receive_errs_total", "node_network_receive_drop_total"],
        required_labels=[],
        optional_labels=["instance", "device"],
        template_instant='sum by (instance) (rate(node_network_receive_errs_total{device!~"lo", {labels}}[1m]) + rate(node_network_receive_drop_total{device!~"lo", {labels}}[1m]))',
        template_range='sum by (instance) (rate(node_network_receive_errs_total{device!~"lo", {labels}}[1m]) + rate(node_network_receive_drop_total{device!~"lo", {labels}}[1m]))',
        threshold_warning=1.0,
        threshold_critical=10.0,
    ),

    # ------------------------------------------------------------
    # 2. 应用程序 / 进程级指标 (Process & Runtime Exporter)
    # ------------------------------------------------------------
    MetricDefinition(
        id="app.process.cpu_usage",
        name="应用进程 CPU 使用率",
        description="指定进程所消耗的 CPU 核数百分比，排查单进程死循环或热点代码。",
        resource_type=ResourceType.APP,
        unit="%",
        exporter="custom_exporter",
        metric_names=["process_cpu_seconds_total"],
        required_labels=[],
        optional_labels=["instance", "job"],
        template_instant="rate(process_cpu_seconds_total{{labels}}[1m]) * 100",
        template_range="rate(process_cpu_seconds_total{{labels}}[1m]) * 100",
        threshold_warning=80.0,
        threshold_critical=95.0,
    ),
    MetricDefinition(
        id="app.process.memory_rss",
        name="应用进程常驻物理内存",
        description="进程实际占用的物理内存 RSS 大小 (Bytes)，排查应用程序是否存在内存缓慢爬升泄漏。",
        resource_type=ResourceType.APP,
        unit="Bytes",
        exporter="custom_exporter",
        metric_names=["process_resident_memory_bytes"],
        required_labels=[],
        optional_labels=["instance", "job"],
        template_instant="process_resident_memory_bytes{{labels}}",
        template_range="process_resident_memory_bytes{{labels}}",
    ),
    MetricDefinition(
        id="app.process.open_fds",
        name="应用进程打开句柄数",
        description="当前进程打开的文件描述符 (FD) 数量，排查未关闭 Socket、连接池泄漏导致的 'Too many open files' 崩溃。",
        resource_type=ResourceType.APP,
        unit="个",
        exporter="custom_exporter",
        metric_names=["process_open_fds"],
        required_labels=[],
        optional_labels=["instance", "job"],
        template_instant="process_open_fds{{labels}}",
        template_range="process_open_fds{{labels}}",
        threshold_warning=1024.0,
        threshold_critical=4096.0,
    ),
    MetricDefinition(
        id="app.runtime.goroutines",
        name="Go 运行时 Goroutine 协程数",
        description="当前 Go 语言微服务存活的协程总量。协程持续不减反增是典型的 Goroutine 泄漏前兆。",
        resource_type=ResourceType.APP,
        unit="个",
        exporter="custom_exporter",
        metric_names=["go_goroutines"],
        required_labels=[],
        optional_labels=["instance", "job"],
        template_instant="go_goroutines{{labels}}",
        template_range="go_goroutines{{labels}}",
        threshold_warning=2000.0,
        threshold_critical=10000.0,
    ),
    MetricDefinition(
        id="app.runtime.gc_duration_p99",
        name="Go 运行时 GC 耗时 P99",
        description="Go 垃圾回收暂停的 99 分位耗时 (秒)。若 GC 超过 100ms 容易诱发业务 RPC 偶发超时 (如 5003 错误)。",
        resource_type=ResourceType.APP,
        unit="秒",
        exporter="custom_exporter",
        metric_names=["go_gc_duration_seconds"],
        required_labels=[],
        optional_labels=["instance", "job"],
        template_instant='go_gc_duration_seconds{quantile="0.99", {labels}}',
        template_range='go_gc_duration_seconds{quantile="0.99", {labels}}',
        threshold_warning=0.05,
        threshold_critical=0.2,
    ),

    # ------------------------------------------------------------
    # 3. 监控基础设施与采集探活指标 (Prometheus Target Status)
    # ------------------------------------------------------------
    MetricDefinition(
        id="exporter.target.up",
        name="监控采集端探活状态",
        description="Prometheus 自身采集各个 Exporter 的健康存活性：1 表示采集健康 (UP)，0 表示目标无法连接 (DOWN)。",
        resource_type=ResourceType.EXPORTER,
        unit="布尔 (1/0)",
        exporter="prometheus",
        metric_names=["up"],
        required_labels=[],
        optional_labels=["instance", "job"],
        template_instant="up{{labels}}",
        template_range="up{{labels}}",
        threshold_critical=1.0,
    ),
]
