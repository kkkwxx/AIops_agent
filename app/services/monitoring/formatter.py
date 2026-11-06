"""监控结果标准化格式化器

职责：
1. 将 Prometheus 返回的原生矩阵 (matrix) 或向量 (vector) 解析为业务友好的数据结构
2. 计算统计分析摘要 (最新值、均值 avg、峰值 max、低谷 min、95 分位 p95)
3. 严格遵循数据治理协议：区分 success / no_data / target_down / error，绝不以空充好
4. 控制数据规模，防止向大模型下发数万条点导致上下文溢出
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from loguru import logger

from .catalog_definitions import MetricDefinition, QueryMode


class ResultFormatter:
    """监控查询结果格式化工具"""

    @staticmethod
    def format_metrics_result(
        raw_data: Dict[str, Any],
        metric: MetricDefinition,
        query_mode: QueryMode,
        promql: str,
        start_time: Optional[float] = None,
        end_time: Optional[float] = None,
        step: Optional[str] = None,
        max_points_preview: int = 30,
    ) -> Dict[str, Any]:
        """格式化指标查询结果为统一标准契约"""
        result_type = raw_data.get("resultType", "unknown")
        result_list = raw_data.get("result", [])

        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        # ------------------------------------------------------------
        # 1. 结果为空：区分无数据场景 (no_data)
        # ------------------------------------------------------------
        if not result_list:
            return {
                "status": "no_data",
                "source": "prometheus",
                "metric_id": metric.id,
                "metric_name": metric.name,
                "resource_type": metric.resource_type.value,
                "unit": metric.unit,
                "query_mode": query_mode.value,
                "promql_executed": promql,
                "sample_time": now_iso,
                "reason": "no_matching_series",
                "diagnostic_hint": (
                    f"在 Prometheus 中未找到匹配的 '{metric.id}' 时序记录。"
                    f"可能原因：1. 过滤标签 (filters) 不匹配；"
                    f"2. 采集端 Exporter ({metric.exporter}) 离线（建议调用 get_target_health 检查）；"
                    f"3. 目标机器在当前查询窗口内尚未上报数据。"
                ),
                "data": [],
                "series_count": 0,
            }

        # ------------------------------------------------------------
        # 2. 结果非空：解析时序或瞬时数据
        # ------------------------------------------------------------
        series_summaries = []
        all_numeric_values: List[float] = []

        if result_type == "matrix":
            # 区间时序查询 (Range Query)
            for item in result_list:
                metric_labels = item.get("metric", {})
                values_tuples = item.get("values", [])

                series_points = []
                series_vals: List[float] = []

                for ts, val_str in values_tuples:
                    try:
                        v = float(val_str)
                        series_vals.append(v)
                        all_numeric_values.append(v)
                        # 转换时间戳为 HH:MM 格式，更直观
                        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
                        series_points.append({
                            "timestamp": dt.strftime("%H:%M:%S"),
                            "value": round(v, 2),
                        })
                    except (ValueError, TypeError):
                        continue

                # 抽样预览，避免数据点过多撑爆 Prompt
                preview_points = series_points
                if len(series_points) > max_points_preview:
                    stride = len(series_points) // max_points_preview
                    preview_points = series_points[::stride][:max_points_preview]

                series_summaries.append({
                    "labels": metric_labels,
                    "point_count": len(series_points),
                    "summary": ResultFormatter._calc_stats(series_vals),
                    "points_preview": preview_points,
                })

        elif result_type == "vector":
            # 瞬时单点查询 (Instant Query)
            for item in result_list:
                metric_labels = item.get("metric", {})
                ts, val_str = item.get("value", [0, "0"])
                try:
                    v = float(val_str)
                    all_numeric_values.append(v)
                    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
                    point = {"timestamp": dt.strftime("%H:%M:%S"), "value": round(v, 2)}
                except (ValueError, TypeError):
                    point = {"timestamp": now_iso, "value": val_str}

                series_summaries.append({
                    "labels": metric_labels,
                    "point_count": 1,
                    "latest_value": point["value"],
                    "timestamp": point["timestamp"],
                })

        # 全局统计摘要
        overall_stats = ResultFormatter._calc_stats(all_numeric_values)

        # 检查是否触发参考阈值预警
        alert_info = ResultFormatter._check_threshold(metric, overall_stats.get("latest"))

        return {
            "status": "success",
            "source": "prometheus",
            "metric_id": metric.id,
            "metric_name": metric.name,
            "resource_type": metric.resource_type.value,
            "unit": metric.unit,
            "query_mode": query_mode.value,
            "promql_executed": promql,
            "query_window": {
                "start": datetime.fromtimestamp(start_time, tz=timezone.utc).isoformat() if start_time else now_iso,
                "end": datetime.fromtimestamp(end_time, tz=timezone.utc).isoformat() if end_time else now_iso,
                "step": step or "1m",
            },
            "sample_time": now_iso,
            "series_count": len(series_summaries),
            "overall_summary": overall_stats,
            "alert_assessment": alert_info,
            "series": series_summaries,
            "message": f"成功查询指标 '{metric.name}'，共包含 {len(series_summaries)} 条时间序列",
        }

    @staticmethod
    def _calc_stats(values: List[float]) -> Dict[str, Any]:
        """计算数值序列的统计学特征"""
        if not values:
            return {}

        sorted_vals = sorted(values)
        count = len(values)
        p95_idx = min(int(count * 0.95), count - 1)

        return {
            "latest": round(values[-1], 2),
            "avg": round(sum(values) / count, 2),
            "max": round(max(values), 2),
            "min": round(min(values), 2),
            "p95": round(sorted_vals[p95_idx], 2),
        }

    @staticmethod
    def _check_threshold(metric: MetricDefinition, latest_val: Optional[float]) -> Dict[str, Any]:
        """与内置阈值比对，提供辅助排障判断"""
        if latest_val is None:
            return {"triggered": False, "level": "normal", "description": "无有效指标值可评估"}

        if metric.threshold_critical is not None and latest_val >= metric.threshold_critical:
            return {
                "triggered": True,
                "level": "critical",
                "description": f"指标当前值 {latest_val} {metric.unit} 已超过严重阈值 ({metric.threshold_critical} {metric.unit})，需高度关注！"
            }

        if metric.threshold_warning is not None and latest_val >= metric.threshold_warning:
            return {
                "triggered": True,
                "level": "warning",
                "description": f"指标当前值 {latest_val} {metric.unit} 达到警告阈值 ({metric.threshold_warning} {metric.unit})。"
            }

        return {
            "triggered": False,
            "level": "normal",
            "description": f"指标当前值 {latest_val} {metric.unit} 处于正常安全区间。"
        }

    @staticmethod
    def format_targets_result(raw_targets: Dict[str, Any], filter_job: Optional[str] = None) -> Dict[str, Any]:
        """格式化 Prometheus 采集目标探活结果"""
        active_targets = raw_targets.get("activeTargets", [])
        
        target_list = []
        up_count = 0
        down_count = 0

        for t in active_targets:
            job = t.get("labels", {}).get("job", "unknown")
            if filter_job and filter_job != job:
                continue

            health = t.get("health", "unknown").lower()
            if health == "up":
                up_count += 1
            else:
                down_count += 1

            target_list.append({
                "job": job,
                "instance": t.get("labels", {}).get("instance", "unknown"),
                "health": health.upper(),
                "scrape_url": t.get("scrapeUrl", ""),
                "last_scrape": t.get("lastScrape", ""),
                "last_error": t.get("lastError", ""),
                "scrape_duration_ms": round(float(t.get("lastScrapeDuration", 0)) * 1000, 2),
            })

        return {
            "status": "success",
            "source": "prometheus",
            "total_targets": len(target_list),
            "up_count": up_count,
            "down_count": down_count,
            "targets": target_list,
            "has_failure": down_count > 0,
            "message": f"共探测到 {len(target_list)} 个监控目标: {up_count} UP, {down_count} DOWN",
        }

