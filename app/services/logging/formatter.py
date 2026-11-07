"""日志结果格式化器与上下文提取器 (LogResultFormatter)

职责：
1. 解析 Loki 原生响应 (streams / matrix)
2. 计算每条日志的纳秒时间戳、本地 ISO 时间与唯一指纹 (Fingerprint)
3. 针对 get_log_context 精准定位目标日志，切片提取前后文关联日志 (before/target/after)
4. 针对 aggregate_log_errors 结构化指标统计与峰值计算
5. 控制返回体积，防止原始日志冗余撑爆 LLM Context
"""

from datetime import datetime
import hashlib
from typing import Any, Dict, List, Optional
from loguru import logger

from .catalog_definitions import LogStreamDefinition


class LogResultFormatter:
    """日志格式化器"""

    @classmethod
    def parse_stream_lines(
        cls,
        raw_data: Dict[str, Any],
        stream_id: str,
        logql: str,
        start_time_iso: Optional[str] = None,
        end_time_iso: Optional[str] = None,
        max_lines: int = 100,
        sort_chronological: bool = True,
    ) -> Dict[str, Any]:
        """解析 Loki streams 格式日志返回为标准结构化对象

        Args:
            raw_data: Loki HTTP API 返回的 data 字段
            stream_id: 日志流 ID
            logql: 实际执行的 LogQL
            start_time_iso: 查询起止时间
            end_time_iso: 查询截止时间
            max_lines: 最大日志条数截断限制
            sort_chronological: 是否按时间由远及近升序排列 (排障友好)
        """
        streams = raw_data.get("result", [])
        all_entries: List[Dict[str, Any]] = []

        for s in streams:
            stream_labels = s.get("stream", {})
            values = s.get("values", [])
            for item in values:
                if len(item) < 2:
                    continue
                ts_ns_str, line_text = item[0], item[1]
                try:
                    ts_ns = int(ts_ns_str)
                    dt = datetime.fromtimestamp(ts_ns / 1e9).astimezone()
                    ts_iso = dt.isoformat()
                except Exception:
                    ts_ns = 0
                    ts_iso = ""

                # 生成唯一指纹 (ts_ns + 截断正文)，确保上下文定位唯一性
                fp_src = f"{ts_ns}_{line_text[:120]}".encode("utf-8")
                fingerprint = hashlib.md5(fp_src).hexdigest()[:16]

                all_entries.append({
                    "timestamp": ts_iso,
                    "timestamp_ns": ts_ns,
                    "fingerprint": fingerprint,
                    "labels": stream_labels,
                    "line": line_text,
                })

        # 排序：默认按时间由旧到新升序排列 (时间流向自然)
        if sort_chronological:
            all_entries.sort(key=lambda x: x["timestamp_ns"])
        else:
            all_entries.sort(key=lambda x: x["timestamp_ns"], reverse=True)

        truncated = len(all_entries) > max_lines
        trimmed_entries = all_entries[:max_lines]

        return {
            "status": "success",
            "stream_id": stream_id,
            "logql": logql,
            "total_returned": len(trimmed_entries),
            "total_matched": len(all_entries),
            "truncated": truncated,
            "time_range": {
                "start": start_time_iso,
                "end": end_time_iso,
            },
            "logs": trimmed_entries,
        }

    @classmethod
    def extract_context_window(
        cls,
        raw_entries: List[Dict[str, Any]],
        target_timestamp_ns: int,
        target_fingerprint: Optional[str] = None,
        before_lines: int = 5,
        after_lines: int = 15,
    ) -> Dict[str, Any]:
        """在按时间升序排列的日志流中定位目标日志并提取上下文

        Args:
            raw_entries: 已按时间戳升序排序的日志条目列表
            target_timestamp_ns: 目标日志的纳秒时间戳
            target_fingerprint: 目标日志的指纹 (可选，精确匹配)
            before_lines: 向前提取行数
            after_lines: 向后提取行数
        """
        if not raw_entries:
            return {
                "status": "not_found",
                "message": "在指定的时间窗口内未找到任何相关日志",
                "context_logs": [],
            }

        # 1. 优先按指纹精准定位
        target_idx: Optional[int] = None
        if target_fingerprint:
            for i, entry in enumerate(raw_entries):
                if entry.get("fingerprint") == target_fingerprint:
                    target_idx = i
                    break

        # 2. 若指纹未命中，按时间戳最接近的条目定位
        if target_idx is None:
            closest_idx = 0
            min_diff = abs(raw_entries[0]["timestamp_ns"] - target_timestamp_ns)
            for i, entry in enumerate(raw_entries[1:], start=1):
                diff = abs(entry["timestamp_ns"] - target_timestamp_ns)
                if diff < min_diff:
                    min_diff = diff
                    closest_idx = i
            target_idx = closest_idx

        # 3. 切片提取前后上下文
        start_idx = max(0, target_idx - before_lines)
        end_idx = min(len(raw_entries), target_idx + after_lines + 1)

        context_logs = []
        for i in range(start_idx, end_idx):
            item = dict(raw_entries[i])
            if i < target_idx:
                item["position"] = "before"
                item["is_target"] = False
            elif i == target_idx:
                item["position"] = "target"
                item["is_target"] = True
            else:
                item["position"] = "after"
                item["is_target"] = False
            context_logs.append(item)

        target_log = raw_entries[target_idx]

        return {
            "status": "success",
            "target_log": target_log,
            "before_count": target_idx - start_idx,
            "after_count": end_idx - target_idx - 1,
            "total_context_lines": len(context_logs),
            "context_logs": context_logs,
        }

    @classmethod
    def format_error_aggregation(
        cls,
        raw_data: Dict[str, Any],
        stream_id: str,
        logql: str,
        step: str = "1m",
        start_time_iso: Optional[str] = None,
        end_time_iso: Optional[str] = None,
    ) -> Dict[str, Any]:
        """格式化错误统计聚合结果 (matrix 类型)

        Args:
            raw_data: Loki HTTP API 返回的 data 字段
            stream_id: 日志流 ID
            logql: 执行的聚合 LogQL
            step: 统计步长
            start_time_iso: 起始时间
            end_time_iso: 截止时间
        """
        matrix_results = raw_data.get("result", [])
        series_list = []
        total_errors_all = 0
        global_peak_errors = 0
        global_peak_time = None

        for item in matrix_results:
            metric_labels = item.get("metric", {})
            values = item.get("values", [])
            points = []
            series_total = 0
            series_peak = 0
            series_peak_time = None

            for val in values:
                if len(val) < 2:
                    continue
                sec, count_str = val[0], val[1]
                try:
                    count = int(float(count_str))
                except Exception:
                    count = 0

                dt_iso = datetime.fromtimestamp(sec).astimezone().isoformat()
                points.append({
                    "timestamp": dt_iso,
                    "count": count,
                })
                series_total += count
                if count > series_peak:
                    series_peak = count
                    series_peak_time = dt_iso

            total_errors_all += series_total
            if series_peak > global_peak_errors:
                global_peak_errors = series_peak
                global_peak_time = series_peak_time

            series_list.append({
                "labels": metric_labels,
                "total_errors": series_total,
                "peak_errors": series_peak,
                "peak_time": series_peak_time,
                "points": points,
            })

        return {
            "status": "success",
            "stream_id": stream_id,
            "logql": logql,
            "unit": f"errors_per_{step}",
            "time_range": {
                "start": start_time_iso,
                "end": end_time_iso,
                "step": step,
            },
            "summary": {
                "total_errors": total_errors_all,
                "peak_errors_in_window": global_peak_errors,
                "peak_time": global_peak_time,
                "series_count": len(series_list),
            },
            "series": series_list,
        }

