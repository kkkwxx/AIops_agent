"""通用监控工具包 (基于 Metric Catalog 与 Query Templates 的受控监控工具)

供 LangGraph AIOps Agent 与 RAG Agent 调用的标准生产级监控工具：
1. search_metrics: 检索可用监控指标与业务语义定义 (防幻觉第一道防线)
2. query_metrics: 受控通用指标查询 (支持 instant / range，自动生成合法 PromQL 并计算统计分析)
3. get_target_health: 监控采集端 (Exporter / Target) 探活与元数据检测 (诊断无数据真因)
4. advanced_promql_query: 受控高级 PromQL 只读查询 (带写拦截、时间限制与序列截断)
"""

import json
from typing import Any, Dict, Optional
from langchain_core.tools import tool
from loguru import logger

from app.services.monitoring import monitoring_gateway


@tool
def search_metrics(
    keyword: str,
    resource_type: Optional[str] = "all",
) -> str:
    """根据关键词在监控指标目录 (Metric Catalog) 中查找可用的监控指标。

    在准备查询 CPU、内存、磁盘、网络、负载或进程相关指标前，必须先调用此工具确定系统中存在的标准指标 ID。

    Args:
        keyword: 检索关键词，支持中文或英文（例如："cpu", "内存", "负载", "disk", "网络", "goroutine"）
        resource_type: 资源分类过滤，可选值："node" (主机/物理机/虚拟机), "app" (应用程序/微服务内部), "exporter" (采集端), "all" (全部，默认)

    Returns:
        JSON 格式的匹配指标列表，包含 metric_id、中文名、详细含义、度量单位与可选过滤标签。
    """
    try:
        logger.info(f"工具调用: search_metrics(keyword='{keyword}', resource_type='{resource_type}')")
        results = monitoring_gateway.search_metrics(keyword=keyword, resource_type=resource_type)
        output = {
            "total_matches": len(results),
            "results": results,
        }
        if not results:
            output["hint"] = "未在指标目录中检索到匹配指标，请尝试更换关键词（如 cpu, 内存, 磁盘, 网络, 负载）或使用 advanced_promql_query 进行高级查询。"
        return json.dumps(output, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"search_metrics 执行失败: {e}")
        return json.dumps({"status": "error", "message": f"指标目录检索失败: {str(e)}"}, ensure_ascii=False)


@tool
async def query_metrics(
    metric_id: str,
    resource: str = "node",
    filters: Optional[Dict[str, str]] = None,
    time_range: str = "30m",
    query_mode: str = "range",
    step: str = "1m",
) -> str:
    """通用受控监控指标查询工具（基于预置 PromQL 模板安全生成与执行）。

    推荐在排查 CPU 飙高、内存泄漏、磁盘打满、网络丢包或服务健康度时优先使用。
    本工具会自动组装合法的 PromQL，并返回包含最新值、均值(avg)、峰值(max)、低谷(min)与 95 分位(p95)的统计分析。

    Args:
        metric_id: 必须是指标目录中定义的标准逻辑 ID（例如："node.cpu.usage", "node.memory.usage", "node.cpu.load1", "node.disk.usage", "node.net.receive_rate"）。若不确定，请先调用 search_metrics。
        resource: 监控资源类别，默认 "node"。
        filters: 标签键值对过滤条件（可选，例如：{"instance": "rocky-linux-local"}）。如果不确定实例名，可传空字典。
        time_range: 时间跨度，默认 "30m"。支持 "5m", "15m", "30m", "1h", "2h", "6h", "24h"。
        query_mode: 查询模式：
            - "range" (默认)：查询一段时间内的走势与时序点，计算 avg/max/p95 统计特征，适合排查突刺和变化趋势。
            - "instant"：查询当前时刻的最新瞬时值，适合快速探活与当前阈值比对。
        step: 采样分辨率步长，默认 "1m"。支持 "15s", "1m", "5m"。

    Returns:
        标准化 JSON 结果，包含指标值、统计学特征 (latest/avg/max/min/p95)、时序抽样点预览或无数据诊断建议 (no_data)。
    """
    try:
        logger.info(
            f"工具调用: query_metrics(metric_id='{metric_id}', resource='{resource}', "
            f"filters={filters}, time_range='{time_range}', query_mode='{query_mode}')"
        )
        res = await monitoring_gateway.query_metrics(
            metric_id=metric_id,
            resource=resource,
            filters=filters,
            time_range=time_range,
            query_mode=query_mode,
            step=step,
        )
        return json.dumps(res, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"query_metrics 执行失败: {e}")
        return json.dumps({"status": "error", "message": f"指标查询执行失败: {str(e)}"}, ensure_ascii=False)


@tool
async def get_target_health(
    job: Optional[str] = None,
    instance: Optional[str] = None,
) -> str:
    """探测 Prometheus 监控采集端 (Exporter / Target) 的健康状态与存活性。

    当调用 query_metrics 返回 no_data（无时序数据）时，必须调用此工具判断是业务本身正常无请求，还是底层的 node_exporter 或 custom_exporter 宕机/端口无法连接。

    Args:
        job: 采集任务名过滤（可选，例如："node_exporter", "custom_exporter"）。如果不传则返回所有采集端状态。
        instance: 实例地址模糊匹配（可选，例如："rocky-linux-local", "127.0.0.1:9100"）。

    Returns:
        JSON 格式的目标状态报告，包含每个 Target 的状态 (UP 或 DOWN)、抓取 URL、最后抓取耗时与最后错误原因 (last_error)。
    """
    try:
        logger.info(f"工具调用: get_target_health(job='{job}', instance='{instance}')")
        res = await monitoring_gateway.get_target_health(job=job, instance=instance)
        return json.dumps(res, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"get_target_health 执行失败: {e}")
        return json.dumps({"status": "error", "message": f"采集端探活失败: {str(e)}"}, ensure_ascii=False)


@tool
async def advanced_promql_query(
    promql: str,
    time_range: str = "30m",
    step: str = "1m",
) -> str:
    """执行受控的原生 PromQL 只读查询（供多指标关联排查使用的高级能力）。

    仅当 query_metrics 中预置的指标模板无法满足多指标联合计算（如 CPU 与 Throttling 关联、自定义比值计算）时使用。
    本工具受到严格的安全策略监管：严禁执行任何写入/删除/管理指令，时间跨度强制限制 ≤24 小时。

    Args:
        promql: 标准只读 PromQL 查询表达式（例如："rate(node_cpu_seconds_total[5m])"）。
        time_range: 查询时间跨度，默认 "30m"。最大允许 24h。
        step: 采样分辨率，默认 "1m"。

    Returns:
        JSON 格式的查询时序结果与统计摘要。
    """
    try:
        logger.info(f"工具调用: advanced_promql_query(promql='{promql}', time_range='{time_range}')")
        res = await monitoring_gateway.advanced_promql_query(
            promql=promql,
            time_range=time_range,
            step=step,
        )
        return json.dumps(res, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"advanced_promql_query 执行失败: {e}")
        return json.dumps({"status": "error", "message": f"PromQL 执行异常: {str(e)}"}, ensure_ascii=False)
