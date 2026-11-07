"""通用日志工具包 (基于 Log Stream Catalog 与受控 LogQL 模板引擎)

供 LangGraph AIOps Agent 与 RAG Agent 调用的生产级日志排障工具：
1. search_log_streams: 检索可用日志流与动态存活性 (防幻觉第一道防线)
2. query_logs: 通用受控日志查询 (支持 ISO 时间对齐、级别过滤、多关键词 AND/OR 组合)
3. get_log_context: 错误日志上下文还原 (基于时间戳/指纹的应用层双向窗口检索)
4. aggregate_log_errors: 错误频次趋势度量 (基于 count_over_time 的指标化聚合)
"""

import json
from typing import Any, Dict, List, Optional
from langchain_core.tools import tool
from loguru import logger

from app.services.logging import logging_gateway


@tool
async def search_log_streams(
    keyword: str = "",
    resource_type: Optional[str] = "all",
) -> str:
    """根据关键词在日志流目录 (Log Stream Catalog) 中检索可用的日志流，并动态获取采集端活性状态。

    在执行具体日志查询前，必须先调用此工具确认系统支持的逻辑日志流 ID (如 container.postgres, system.syslog)。
    本工具会返回日志流的名称、业务描述、分类以及活性状态 (active=近期有日志, silent=近期静默无日志, unknown=无法探测)。

    Args:
        keyword: 检索关键词，支持中文或英文（例如："postgres", "系统", "syslog", "dockerd", "promtail", "loki"）。传空字符串返回所有。
        resource_type: 资源类型过滤，可选值："container" (容器日志), "system" (系统与底层日志), "app" (应用微服务), "all" (全部，默认)

    Returns:
        JSON 格式的匹配日志流列表，包含 stream_id、name、description、status (active/silent/unknown)、last_seen 与支持的过滤字段。
    """
    try:
        logger.info(f"工具调用: search_log_streams(keyword='{keyword}', resource_type='{resource_type}')")
        results = await logging_gateway.search_log_streams(keyword=keyword, resource_type=resource_type)
        output = {
            "total_matches": len(results),
            "results": results,
        }
        if not results:
            output["hint"] = "未检索到匹配的日志流，请尝试更换关键词（如 syslog, postgres, promtail, loki）或将 resource_type 设置为 all。"
        return json.dumps(output, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"search_log_streams 执行失败: {e}")
        return json.dumps({"status": "error", "message": f"检索日志流失败: {str(e)}"}, ensure_ascii=False)


@tool
async def query_logs(
    stream_id: str,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    level: Optional[str] = None,
    keywords: Optional[List[str]] = None,
    match_mode: str = "all",
    limit: int = 30,
) -> str:
    """通用受控日志查询工具（基于 Log Stream Catalog 自动生成 LogQL 并安全执行）。

    支持精准时间窗口对齐（将指标突刺时间与日志异常对齐）、日志级别过滤与多关键词逻辑组合。
    底层自动识别文本正则与 JSON Parser 结构化提取，严防构造非法 Label 查询。

    Args:
        stream_id: 必须是日志目录中定义的有效逻辑流 ID（例如："system.syslog", "container.postgres", "container.promtail", "container.loki"）。若不确定，请先调用 search_log_streams。
        start_time: 起始时间。强烈推荐传入 ISO 8601 格式（例如 "2026-11-06T19:00:00+08:00"）对齐指标异常时刻；也支持相对时间如 "15m", "1h"。若不传，默认向前回溯 15 分钟。
        end_time: 截止时间。支持 ISO 8601 格式（例如 "2026-11-06T19:15:00+08:00"）。若不传，默认当前时刻。
        level: 日志级别过滤（可选，例如："ERROR", "WARN", "INFO", "DEBUG"）。自动匹配正文或结构化字段。
        keywords: 关键词列表（可选，例如：["connection refused", "timeout", "dockerd"]）。
        match_mode: 关键词匹配逻辑：
            - "all" (默认)：必须同时满足所有关键词 (AND 逻辑)。
            - "any"：满足任意一个关键词即可 (OR 逻辑)。
        limit: 最大返回日志条数，默认 30 条（安全上限硬限制 100 条）。

    Returns:
        标准化 JSON 结果，包含排序好的日志列表 (含 timestamp, fingerprint, labels, line)，自动完成敏感信息脱敏与体积截断保护。
    """
    try:
        logger.info(
            f"工具调用: query_logs(stream_id='{stream_id}', start_time='{start_time}', "
            f"end_time='{end_time}', level='{level}', keywords={keywords}, match_mode='{match_mode}', limit={limit})"
        )
        res = await logging_gateway.query_logs(
            stream_id=stream_id,
            start_time=start_time,
            end_time=end_time,
            level=level,
            keywords=keywords,
            match_mode=match_mode,
            limit=limit,
        )
        return json.dumps(res, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"query_logs 执行失败: {e}")
        return json.dumps({"status": "error", "message": f"日志查询失败: {str(e)}"}, ensure_ascii=False)


@tool
async def get_log_context(
    stream_id: str,
    target_timestamp: str,
    before_lines: int = 5,
    after_lines: int = 15,
    fingerprint: Optional[str] = None,
) -> str:
    """提取目标错误日志的前后关联上下文日志（应用层双向时间窗口检索）。

    当通过 query_logs 定位到某条 ERROR/FATAL 异常日志后，调用本工具还原报错发生前后的业务调用链、SQL 请求或上下文环境。

    Args:
        stream_id: 目标日志所在的日志流 ID（如 "system.syslog", "container.postgres"）。
        target_timestamp: 目标日志的时间戳（必须直接传入 query_logs 返回结果中的 timestamp 字段，如 "2026-11-06T19:31:19.686763+08:00" 或纳秒数值）。
        before_lines: 目标日志之前提取的行数，默认 5 行。
        after_lines: 目标日志之后提取的行数，默认 15 行（通常异常堆栈在报错后方展开）。
        fingerprint: 目标日志的唯一指纹（可选，直接传入 query_logs 返回的 fingerprint，用于相同时间戳下的精确去歧义匹配）。

    Returns:
        JSON 格式的上下文日志列表，明确标注每条记录的相对位置（before / target / after），方便还原完整事故案发现场。
    """
    try:
        logger.info(
            f"工具调用: get_log_context(stream_id='{stream_id}', target_timestamp='{target_timestamp}', "
            f"before_lines={before_lines}, after_lines={after_lines}, fingerprint='{fingerprint}')"
        )
        res = await logging_gateway.get_log_context(
            stream_id=stream_id,
            target_timestamp=target_timestamp,
            before_lines=before_lines,
            after_lines=after_lines,
            fingerprint=fingerprint,
        )
        return json.dumps(res, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"get_log_context 执行失败: {e}")
        return json.dumps({"status": "error", "message": f"提取日志上下文失败: {str(e)}"}, ensure_ascii=False)


@tool
async def aggregate_log_errors(
    stream_id: str,
    time_range: str = "1h",
    step: str = "1m",
) -> str:
    """统计日志流指定时间跨度内的错误频次与变化趋势（基于 count_over_time 指标化度量）。

    用于评估故障波及范围、判断错误是否正在突刺暴增或收敛，补充结构化指标不足的异常事件趋势。

    Args:
        stream_id: 目标日志流 ID（例如 "system.syslog", "container.postgres"）。
        time_range: 统计时间跨度，默认 "1h"。支持 "15m", "30m", "1h", "2h", "6h", "24h"。
        step: 采样统计步长，默认 "1m"。支持 "1m", "5m"。

    Returns:
        JSON 格式的时序统计结果，包含单位 (errors_per_1m)、总错误数 (total_errors)、峰值时间 (peak_time) 与分段时序点。
    """
    try:
        logger.info(
            f"工具调用: aggregate_log_errors(stream_id='{stream_id}', time_range='{time_range}', step='{step}')"
        )
        res = await logging_gateway.aggregate_log_errors(
            stream_id=stream_id,
            time_range=time_range,
            step=step,
        )
        return json.dumps(res, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"aggregate_log_errors 执行失败: {e}")
        return json.dumps({"status": "error", "message": f"统计错误频次失败: {str(e)}"}, ensure_ascii=False)

