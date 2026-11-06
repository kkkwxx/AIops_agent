"""监控安全网关与治理调度器 (Monitoring Gateway)

职责：
1. 作为 Agent 与底层 Prometheus 之间的数据治理大门
2. 时间跨度校验 (默认 ≤24h)、采样步长校验 (≥15s) 与硬超时调度
3. PromQL 安全语法审查与写指令/危险函数拦截 (保护生产 TSDB 不被恶意刷挂)
4. 数据规模截断 (限制最多返回 50 条序列，保护 LLM Context)
5. 结果标准化适配与统一错误语义映射
"""

import re
import time
from typing import Any, Dict, List, Optional
from loguru import logger

from app.config import config
from .catalog import metric_catalog
from .catalog_definitions import MetricDefinition, QueryMode, ResourceType
from .client import (
    PrometheusClient,
    PrometheusConnectionError,
    PrometheusError,
    PrometheusQueryError,
    prometheus_client,
)
from .formatter import ResultFormatter


class MonitoringGateway:
    """监控安全网关单例"""

    # 严格拦截的危险关键字/管理接口模式
    DANGEROUS_PATTERNS = [
        r'\badmin\b',
        r'\bdelete\b',
        r'\bdrop\b',
        r'\bclean_tombstones\b',
        r'\bsnapshot\b',
        r'\bquit\b',
        r'__name__\s*=\s*".*delete.*"',
    ]

    def __init__(
        self,
        client: Optional[PrometheusClient] = None,
        max_range_hours: Optional[int] = None,
    ):
        self.client = client or prometheus_client
        self.max_range_hours = (
            max_range_hours
            if max_range_hours is not None
            else config.monitoring.prometheus.max_range_hours
        )
        logger.info(f"MonitoringGateway 初始化: max_range_hours={self.max_range_hours}h")

    # ============================================================
    # 核心对外业务接口 (直接支撑 LangGraph Tools)
    # ============================================================

    def search_metrics(
        self,
        keyword: str,
        resource_type: Optional[str] = None,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """按关键词与资源类型在指标目录中检索匹配的指标"""
        matched = metric_catalog.search(keyword=keyword, resource_type=resource_type, limit=limit)
        return [
            {
                "metric_id": m.id,
                "name": m.name,
                "description": m.description,
                "resource_type": m.resource_type.value,
                "unit": m.unit,
                "required_labels": m.required_labels,
                "optional_labels": m.optional_labels,
                "supported_range": m.supported_range,
                "exporter": m.exporter,
            }
            for m in matched
        ]

    async def query_metrics(
        self,
        metric_id: str,
        resource: str = "node",
        filters: Optional[Dict[str, str]] = None,
        time_range: str = "30m",
        query_mode: str = "range",
        step: str = "1m",
    ) -> Dict[str, Any]:
        """通用受控指标查询 (通过 Metric Catalog 模板生成 PromQL 并执行)"""
        mode = QueryMode.RANGE if query_mode.lower() == "range" else QueryMode.INSTANT

        # 1. 查找指标并注入参数组装 PromQL
        try:
            promql, metric = metric_catalog.build_promql(
                metric_id=metric_id,
                filters=filters,
                query_mode=mode,
            )
        except KeyError as e:
            return {
                "status": "error",
                "error_type": "invalid_metric_id",
                "message": str(e),
                "suggestion": "请先调用 search_metrics 工具查找系统中支持的指标 ID",
            }
        except ValueError as e:
            return {
                "status": "error",
                "error_type": "missing_required_labels",
                "message": str(e),
            }

        # 2. 计算时间范围
        range_seconds = self._parse_time_range(time_range)
        end_time = time.time()
        start_time = end_time - range_seconds
        step_normalized = self._normalize_step(step)

        # 3. 执行 Prometheus 查询
        try:
            if mode == QueryMode.RANGE:
                raw_data = await self.client.query_range(
                    promql=promql,
                    start=start_time,
                    end=end_time,
                    step=step_normalized,
                )
            else:
                raw_data = await self.client.query_instant(
                    promql=promql,
                    time=end_time,
                )

            # 4. 格式化并计算摘要
            return ResultFormatter.format_metrics_result(
                raw_data=raw_data,
                metric=metric,
                query_mode=mode,
                promql=promql,
                start_time=start_time if mode == QueryMode.RANGE else None,
                end_time=end_time,
                step=step_normalized if mode == QueryMode.RANGE else None,
            )

        except PrometheusConnectionError as e:
            return {
                "status": "target_down",
                "source": "prometheus",
                "metric_id": metric_id,
                "reason": "prometheus_service_unreachable",
                "message": str(e),
                "diagnostic_hint": "Prometheus TSDB 实例未启动或网络不通，请检查 Docker 中的 prometheus 容器",
            }
        except PrometheusQueryError as e:
            return {
                "status": "error",
                "source": "prometheus",
                "metric_id": metric_id,
                "reason": "query_execution_failed",
                "message": str(e),
                "promql_executed": promql,
            }
        except Exception as e:
            logger.error(f"query_metrics 发生未预期错误: {e}")
            return {
                "status": "error",
                "message": f"查询系统异常: {str(e)}",
            }

    async def get_target_health(
        self,
        job: Optional[str] = None,
        instance: Optional[str] = None,
    ) -> Dict[str, Any]:
        """探测 Prometheus 采集目标 (Exporter) 存活健康度"""
        try:
            raw_targets = await self.client.get_targets(state="active")
            formatted = ResultFormatter.format_targets_result(raw_targets, filter_job=job)

            # 如果指定了 instance 进一步过滤
            if instance and "targets" in formatted:
                filtered_targets = [
                    t for t in formatted["targets"]
                    if instance in t["instance"] or instance in t["scrape_url"]
                ]
                formatted["targets"] = filtered_targets
                formatted["total_targets"] = len(filtered_targets)

            return formatted

        except PrometheusConnectionError as e:
            return {
                "status": "target_down",
                "source": "prometheus",
                "reason": "prometheus_service_unreachable",
                "message": str(e),
                "has_failure": True,
            }
        except Exception as e:
            logger.error(f"get_target_health 执行失败: {e}")
            return {
                "status": "error",
                "message": f"采集探活异常: {str(e)}",
            }

    async def advanced_promql_query(
        self,
        promql: str,
        time_range: str = "30m",
        step: str = "1m",
    ) -> Dict[str, Any]:
        """受控的高级 PromQL 原生查询 (带严格只读安全校验与时间/数据量治理)"""
        # 1. 安全校验：只读与危险指令防范
        clean_promql = promql.strip()
        for pat in self.DANGEROUS_PATTERNS:
            if re.search(pat, clean_promql, re.IGNORECASE):
                logger.warning(f"拦截非法/危险 PromQL 查询尝试: '{promql}' (匹配模式: {pat})")
                return {
                    "status": "error",
                    "reason": "security_policy_violation",
                    "message": "查询被安全策略拦截：仅支持只读 PromQL 表达式，严禁包含 admin/delete/drop 等敏感写操作",
                }

        # 2. 限制时间跨度
        range_seconds = self._parse_time_range(time_range)
        end_time = time.time()
        start_time = end_time - range_seconds
        step_normalized = self._normalize_step(step)

        # 3. 执行查询
        try:
            raw_data = await self.client.query_range(
                promql=clean_promql,
                start=start_time,
                end=end_time,
                step=step_normalized,
            )

            result_list = raw_data.get("result", [])
            max_allowed_series = config.monitoring.prometheus.max_series

            # 数据量控制：截断过多序列
            truncated = False
            if len(result_list) > max_allowed_series:
                raw_data["result"] = result_list[:max_allowed_series]
                truncated = True

            # 4. 封装返回
            fake_metric = MetricDefinition(
                id="custom.promql",
                name="高级 PromQL 查询",
                description=f"原生 PromQL: {clean_promql}",
                resource_type=ResourceType.APP,
                unit="自定义",
                exporter="custom",
                template_instant=clean_promql,
                template_range=clean_promql,
            )

            result = ResultFormatter.format_metrics_result(
                raw_data=raw_data,
                metric=fake_metric,
                query_mode=QueryMode.RANGE,
                promql=clean_promql,
                start_time=start_time,
                end_time=end_time,
                step=step_normalized,
            )

            if truncated:
                result["warning"] = f"匹配时序数量达到 {len(result_list)} 条，为防止溢出已安全截断至前 {max_allowed_series} 条"

            return result

        except Exception as e:
            return {
                "status": "error",
                "message": f"PromQL 执行失败: {str(e)}",
                "promql": clean_promql,
            }

    # ============================================================
    # 辅助与参数校验方法
    # ============================================================

    def _parse_time_range(self, time_range_str: str) -> int:
        """将 5m, 30m, 1h, 24h 等字符串解析为秒数，并强制限制上限"""
        m = re.match(r'^(\d+)([smhd])$', time_range_str.strip().lower())
        if not m:
            logger.warning(f"无法识别的时间格式 '{time_range_str}'，默认回退为 30 分钟")
            return 1800  # 30m

        val = int(m.group(1))
        unit = m.group(2)

        multipliers = {'s': 1, 'm': 60, 'h': 3600, 'd': 86400}
        total_seconds = val * multipliers[unit]

        max_seconds = self.max_range_hours * 3600
        if total_seconds > max_seconds:
            logger.warning(
                f"请求时间跨度 {time_range_str} ({total_seconds}s) 超过最大限制 {self.max_range_hours}h，自动截断"
            )
            return max_seconds

        return max(total_seconds, 60)  # 至少 1 分钟

    def _normalize_step(self, step_str: str) -> str:
        """校验并规整化 step 步长，防止采样过密打崩 Prometheus"""
        clean_step = step_str.strip().lower()
        m = re.match(r'^(\d+)([smh])$', clean_step)
        if not m:
            return "1m"

        val = int(m.group(1))
        unit = m.group(2)
        sec = val * {'s': 1, 'm': 60, 'h': 3600}[unit]

        # 步长不得低于 15 秒
        if sec < 15:
            return "15s"
        return clean_step


# 全局单例
monitoring_gateway = MonitoringGateway()

