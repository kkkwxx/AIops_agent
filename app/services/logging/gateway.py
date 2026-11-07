"""日志安全网关与治理调度器 (Logging Gateway)

职责：
1. 作为 Agent 与底层 Loki 之间的统一安全访问大门
2. 参数审计与校验：ISO 8601 与相对时间解析、时间跨度截断 (≤24h)、返回条数硬限制 (≤100条)
3. 动态敏感数据脱敏流水线 (密码/Token/手机号/AK 掩码)
4. Prompt 注入防御：输出隔离沙箱标记 (<system_log_stream>)
5. 结果体积极限保护：响应体严格控制在 256KB 内，防止击穿 LLM Context
6. 标准化异常语义降级与排障引导
"""

from datetime import datetime, timezone
import json
import re
import time
from typing import Any, Dict, List, Optional
from loguru import logger

from app.config import config
from .catalog import LogStreamCatalog, log_stream_catalog
from .client import (
    LokiClient,
    LokiConnectionError,
    LokiError,
    LokiQueryError,
    loki_client,
)
from .formatter import LogResultFormatter


class LoggingGateway:
    """日志安全治理网关单例"""

    # 敏感信息脱敏正则匹配规则
    SENSITIVE_PATTERNS = [
        # 密码与明文 Secret
        (re.compile(r'(?i)(password|passwd|pwd|secret)\s*[:=]\s*["\']?([^"\',\s&]{1,64})["\']?'), r'\1="***"'),
        # Bearer Authorization Token
        (re.compile(r'(?i)bearer\s+[a-zA-Z0-9_\-\.]{10,}'), 'Bearer ***'),
        # 常见 AccessKey / Token 赋值
        (re.compile(r'(?i)(token|access_key|accesskey|api_key|apikey|secret_key)\s*[:=]\s*["\']?([a-zA-Z0-9_\-]{8,64})["\']?'), r'\1="***"'),
        # 中国大陆手机号码
        (re.compile(r'\b(1[3-9]\d)\d{4}(\d{4})\b'), r'\1****\2'),
    ]

    def __init__(
        self,
        client: Optional[LokiClient] = None,
        catalog: Optional[LogStreamCatalog] = None,
        max_range_hours: Optional[int] = None,
        max_lines: Optional[int] = None,
        max_response_bytes: Optional[int] = None,
    ):
        self.client = client or loki_client
        self.catalog = catalog or log_stream_catalog
        self.max_range_hours = (
            max_range_hours
            if max_range_hours is not None
            else config.logging.loki.max_range_hours
        )
        self.max_lines = (
            max_lines
            if max_lines is not None
            else config.logging.loki.max_lines
        )
        self.max_response_bytes = (
            max_response_bytes
            if max_response_bytes is not None
            else config.logging.loki.max_response_bytes
        )
        logger.info(
            f"LoggingGateway 初始化完成: max_range_hours={self.max_range_hours}h, "
            f"max_lines={self.max_lines}, max_response_bytes={self.max_response_bytes}B"
        )

    # ============================================================
    # 核心对外业务接口 (直接支撑 LangGraph Tools)
    # ============================================================

    async def search_log_streams(
        self,
        keyword: str = "",
        resource_type: Optional[str] = None,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """在受控日志流目录中检索匹配的日志流，并动态探活"""
        return await self.catalog.search(
            keyword=keyword,
            resource_type=resource_type,
            limit=limit,
            check_liveness=True,
        )

    async def query_logs(
        self,
        stream_id: str,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        level: Optional[str] = None,
        keywords: Optional[List[str]] = None,
        match_mode: str = "all",
        limit: int = 50,
    ) -> Dict[str, Any]:
        """通用结构化日志受控查询

        Args:
            stream_id: 日志流 ID (如 container.postgres, system.syslog)
            start_time: 起始时间 (支持 ISO 8601 或相对时间如 '15m')
            end_time: 截止时间 (支持 ISO 8601，默认当前时间)
            level: 目标日志级别 (ERROR/WARN/INFO/DEBUG)
            keywords: 过滤关键词列表
            match_mode: 关键词匹配模式 ('all' 或 'any')
            limit: 返回最大行数 (网关硬限制 ≤100)
        """
        # 1. 组装受控 LogQL
        try:
            logql, stream = self.catalog.build_logql(
                stream_id=stream_id,
                level=level,
                keywords=keywords,
                match_mode=match_mode,
            )
        except KeyError as e:
            return {
                "status": "error",
                "error_type": "invalid_stream_id",
                "message": str(e),
                "suggestion": "请先调用 search_log_streams 工具查找系统中已登记的日志流 ID",
            }

        # 2. 解析与校验起止时间 (纳秒级别)
        start_ns, end_ns, start_iso, end_iso = self._parse_and_validate_timerange(start_time, end_time)

        # 3. 截断最大返回行数
        clamped_limit = min(max(1, limit), self.max_lines)

        # 4. 执行底层 Loki 查询
        try:
            raw_data = await self.client.query_range(
                logql=logql,
                start=start_ns,
                end=end_ns,
                limit=clamped_limit,
                direction="backward",
            )

            # 5. 格式化日志
            result = LogResultFormatter.parse_stream_lines(
                raw_data=raw_data,
                stream_id=stream_id,
                logql=logql,
                start_time_iso=start_iso,
                end_time_iso=end_iso,
                max_lines=clamped_limit,
                sort_chronological=True,
            )

            # 6. 脱敏与体积安全治理
            self._apply_redaction(result.get("logs", []))
            self._apply_byte_limit(result)

            return result

        except LokiConnectionError as e:
            return {
                "status": "service_down",
                "source": "loki",
                "stream_id": stream_id,
                "message": str(e),
                "diagnostic_hint": "Loki 日志引擎不可达，请检查 Docker 中的 loki 容器是否正常运行",
            }
        except LokiQueryError as e:
            return {
                "status": "error",
                "source": "loki",
                "stream_id": stream_id,
                "message": str(e),
            }
        except Exception as e:
            logger.error(f"query_logs 执行异常: {e}")
            return {
                "status": "error",
                "message": f"日志查询失败: {str(e)}",
            }

    async def get_log_context(
        self,
        stream_id: str,
        target_timestamp: str,
        before_lines: int = 5,
        after_lines: int = 15,
        fingerprint: Optional[str] = None,
    ) -> Dict[str, Any]:
        """获取目标错误日志的前后关联上下文 (应用层双向窗口检索)

        Args:
            stream_id: 日志流 ID
            target_timestamp: 目标日志的时间戳 (支持 ISO 8601 或纳秒整数)
            before_lines: 向前获取日志行数 (默认 5)
            after_lines: 向后获取日志行数 (默认 15)
            fingerprint: 目标日志指纹 (可选，用于精确匹配相同时间戳日志)
        """
        stream = self.catalog.get(stream_id)
        if not stream:
            return {
                "status": "error",
                "error_type": "invalid_stream_id",
                "message": f"未知的日志流 ID '{stream_id}'",
                "suggestion": "请先调用 search_log_streams 查看有效流 ID",
            }

        # 1. 解析目标时间戳为纳秒
        try:
            target_ns = self._parse_single_time_to_ns(target_timestamp)
        except Exception as e:
            return {
                "status": "error",
                "error_type": "invalid_timestamp",
                "message": f"无法解析目标时间戳 '{target_timestamp}': {e}",
            }

        # 2. 构造前后 30 秒探测时间窗口
        window_half_ns = int(30 * 1e9)
        start_ns = target_ns - window_half_ns
        end_ns = target_ns + window_half_ns

        # 3. 构造基础流 LogQL (不带 level 过滤，确保完整保留上下文业务日志)
        base_logql = self.catalog._format_base_selector(stream.selector)
        total_fetch = before_lines + after_lines + 30

        try:
            raw_data = await self.client.query_range(
                logql=base_logql,
                start=start_ns,
                end=end_ns,
                limit=min(total_fetch, 150),
                direction="forward",
            )

            # 解析为按时间升序条目
            parsed = LogResultFormatter.parse_stream_lines(
                raw_data=raw_data,
                stream_id=stream_id,
                logql=base_logql,
                max_lines=150,
                sort_chronological=True,
            )

            raw_logs = parsed.get("logs", [])
            self._apply_redaction(raw_logs)

            # 4. 在应用层提取目标日志及前后上下文窗口
            context_result = LogResultFormatter.extract_context_window(
                raw_entries=raw_logs,
                target_timestamp_ns=target_ns,
                target_fingerprint=fingerprint,
                before_lines=before_lines,
                after_lines=after_lines,
            )

            context_result["stream_id"] = stream_id
            self._apply_byte_limit(context_result)
            return context_result

        except Exception as e:
            logger.error(f"get_log_context 执行异常: {e}")
            return {
                "status": "error",
                "message": f"提取日志上下文失败: {str(e)}",
            }

    async def aggregate_log_errors(
        self,
        stream_id: str,
        time_range: str = "1h",
        step: str = "1m",
    ) -> Dict[str, Any]:
        """统计日志流指定时间跨度内的错误频次与分布趋势 (指标化聚合)

        Args:
            stream_id: 日志流 ID
            time_range: 统计时间窗口 (如 '30m', '1h', '6h')
            step: 分辨率步长 (如 '1m', '5m')
        """
        try:
            logql, stream = self.catalog.build_aggregation_logql(stream_id, window=step)
        except KeyError as e:
            return {
                "status": "error",
                "error_type": "invalid_stream_id",
                "message": str(e),
            }

        start_ns, end_ns, start_iso, end_iso = self._parse_and_validate_timerange(
            start_time=time_range, end_time=None
        )

        try:
            raw_data = await self.client.query_range(
                logql=logql,
                start=start_ns,
                end=end_ns,
                step=step,
            )

            result = LogResultFormatter.format_error_aggregation(
                raw_data=raw_data,
                stream_id=stream_id,
                logql=logql,
                step=step,
                start_time_iso=start_iso,
                end_time_iso=end_iso,
            )
            return result

        except Exception as e:
            logger.error(f"aggregate_log_errors 执行异常: {e}")
            return {
                "status": "error",
                "message": f"统计日志错误聚合失败: {str(e)}",
            }

    # ============================================================
    # 私有数据治理与校验方法
    # ============================================================

    def _parse_single_time_to_ns(self, time_val: str) -> int:
        """将单个时间表达转换为纳秒时间戳"""
        val = time_val.strip()
        # 若为纯数字，判断是否已是纳秒或秒
        if val.isdigit():
            num = int(val)
            if num > 1e16:  # 纳秒
                return num
            elif num > 1e11:  # 毫秒
                return int(num * 1e6)
            else:  # 秒
                return int(num * 1e9)

        # 尝试标准 ISO 格式解析
        try:
            # 兼容带有空格的时间格式 '2026-11-06 19:00:00'
            iso_val = val.replace(" ", "T")
            dt = datetime.fromisoformat(iso_val)
            if dt.tzinfo is None:
                # 缺失时区则赋予本地时区
                dt = dt.astimezone()
            return int(dt.timestamp() * 1e9)
        except ValueError:
            pass

        # 尝试纯时间格式解析 (如 '11:49' 或 '20:00:15.123')，自动绑定当前本地日期
        time_match = re.match(r'^(\d{1,2}):(\d{2})(?::(\d{2}(?:\.\d+)?))?$', val)
        if time_match:
            hour = int(time_match.group(1))
            minute = int(time_match.group(2))
            sec_str = time_match.group(3) or '0'
            second = int(float(sec_str))
            micro = int((float(sec_str) - second) * 1e6)
            now_dt = datetime.now().astimezone()
            target_dt = now_dt.replace(hour=hour, minute=minute, second=second, microsecond=micro)
            return int(target_dt.timestamp() * 1e9)

        # 相对时间回溯，如 '15m'
        seconds = self._parse_relative_duration(val)
        return int((time.time() - seconds) * 1e9)

    def _parse_and_validate_timerange(
        self,
        start_time: Optional[str],
        end_time: Optional[str],
    ) -> tuple[int, int, str, str]:
        """解析并校验时间范围，返回 (start_ns, end_ns, start_iso, end_iso)"""
        now = time.time()

        # 解析 end_time
        if end_time and end_time.strip():
            end_ns = self._parse_single_time_to_ns(end_time)
        else:
            end_ns = int(now * 1e9)

        # 解析 start_time
        if start_time and start_time.strip():
            # 判断 start_time 是否是相对时间 (如 "15m", "1h")
            if re.match(r'^\d+[smhd]$', start_time.strip()):
                delta_sec = self._parse_relative_duration(start_time.strip())
                start_ns = end_ns - int(delta_sec * 1e9)
            else:
                start_ns = self._parse_single_time_to_ns(start_time)
        else:
            # 默认回溯 15 分钟
            start_ns = end_ns - int(15 * 60 * 1e9)

        # 确保 start_ns < end_ns
        if start_ns >= end_ns:
            start_ns = end_ns - int(15 * 60 * 1e9)

        # 校验最大跨度 (硬限制 24h)
        max_span_ns = int(self.max_range_hours * 3600 * 1e9)
        if (end_ns - start_ns) > max_span_ns:
            logger.warning(
                f"查询时间跨度超过允许上限 ({self.max_range_hours}h)，自动截断至近 {self.max_range_hours} 小时"
            )
            start_ns = end_ns - max_span_ns

        start_dt = datetime.fromtimestamp(start_ns / 1e9).astimezone()
        end_dt = datetime.fromtimestamp(end_ns / 1e9).astimezone()

        return start_ns, end_ns, start_dt.isoformat(), end_dt.isoformat()

    def _parse_relative_duration(self, duration_str: str) -> float:
        """解析相对时间字符串 (如 15s, 30m, 1h, 2d) 为秒数"""
        m = re.match(r'^(\d+)([smhd])$', duration_str.strip().lower())
        if not m:
            return 900.0  # 默认 15m
        val, unit = int(m.group(1)), m.group(2)
        unit_map = {"s": 1, "m": 60, "h": 3600, "d": 86400}
        return float(val * unit_map[unit])

    def _apply_redaction(self, log_entries: List[Dict[str, Any]]):
        """对日志列表中的文本正文执行就地脱敏"""
        for item in log_entries:
            line = item.get("line", "")
            if not line:
                continue
            for pattern, repl in self.SENSITIVE_PATTERNS:
                line = pattern.sub(repl, line)
            item["line"] = line

    def _apply_byte_limit(self, result: Dict[str, Any]):
        """控制整体序列化响应体体积不超过 max_response_bytes"""
        try:
            serialized = json.dumps(result, ensure_ascii=False)
            current_bytes = len(serialized.encode("utf-8"))
            if current_bytes <= self.max_response_bytes:
                return

            logger.warning(
                f"日志响应体体积 ({current_bytes}B) 超过限制 ({self.max_response_bytes}B)，执行紧急截断"
            )
            logs = result.get("logs") or result.get("context_logs")
            if isinstance(logs, list):
                # 动态逐步减半直到体积符合标准
                while logs and len(serialized.encode("utf-8")) > self.max_response_bytes:
                    half = max(1, len(logs) // 2)
                    logs = logs[:half]
                    if "logs" in result:
                        result["logs"] = logs
                        result["total_returned"] = len(logs)
                    elif "context_logs" in result:
                        result["context_logs"] = logs
                    result["truncated"] = True
                    serialized = json.dumps(result, ensure_ascii=False)

        except Exception as e:
            logger.debug(f"计算响应体积异常: {e}")


# 全局单例
logging_gateway = LoggingGateway()

