"""日志流目录 (Log Stream Catalog) 管理器

职责：
1. 维护受控的日志流元数据字典 (Log Stream Catalog)
2. 提供面向关键词、逻辑流 ID、资源类型的高效检索与动态存活性探活 (active / silent / unknown)
3. 受控 LogQL 模板引擎：将业务意图 (流 ID、级别、关键字数组、匹配模式) 转换为语法严谨的 LogQL
4. 区分纯文本正则提取与 JSON Parser 结构化提取，避免无效 Label 查询
"""

import asyncio
from datetime import datetime, timezone
import re
import time
from typing import Any, Dict, List, Optional, Tuple
from loguru import logger

from .catalog_definitions import BUILTIN_LOG_STREAMS, LogFormat, LogStreamDefinition, StreamStatus
from .client import LokiClient, loki_client


class LogStreamCatalog:
    """日志流目录管理器单例"""

    def __init__(
        self,
        streams: Optional[List[LogStreamDefinition]] = None,
        client: Optional[LokiClient] = None,
    ):
        self._streams_list: List[LogStreamDefinition] = streams or BUILTIN_LOG_STREAMS
        self._streams_map: Dict[str, LogStreamDefinition] = {s.id: s for s in self._streams_list}
        self.client = client or loki_client
        logger.info(f"LogStreamCatalog 初始化完成: 成功加载 {len(self._streams_list)} 个预置日志流定义")

    def get(self, stream_id: str) -> Optional[LogStreamDefinition]:
        """根据唯一流 ID 获取日志流元数据"""
        return self._streams_map.get(stream_id.strip())

    def list_all(self, resource_type: Optional[str] = None) -> List[LogStreamDefinition]:
        """列出所有预置日志流，支持按资源类别过滤"""
        if not resource_type or resource_type == "all":
            return list(self._streams_list)
        return [s for s in self._streams_list if s.resource_type == resource_type]

    async def check_stream_liveness(
        self,
        stream: LogStreamDefinition,
        lookback_seconds: int = 900,  # 默认检测最近 15 分钟
    ) -> Tuple[StreamStatus, Optional[str]]:
        """动态检测指定日志流的最近活跃状态与最新时间戳

        Args:
            stream: 日志流元数据
            lookback_seconds: 回溯探测时间窗口 (秒)

        Returns:
            Tuple[StreamStatus, Optional[str]]: (状态: active/silent/unknown, 最新日志 ISO 时间字符串)
        """
        now = time.time()
        start_ns = int((now - lookback_seconds) * 1e9)
        end_ns = int(now * 1e9)
        base_logql = self._format_base_selector(stream.selector)

        try:
            data = await self.client.query_range(
                logql=base_logql,
                start=start_ns,
                end=end_ns,
                limit=1,
                direction="backward",
            )
            result = data.get("result", [])
            if result and len(result) > 0:
                values = result[0].get("values", [])
                if values and len(values) > 0:
                    latest_ns_str = values[0][0]
                    # 将纳秒时间戳转为本地时区 ISO 8601
                    ts_float = float(latest_ns_str) / 1e9
                    dt = datetime.fromtimestamp(ts_float).astimezone()
                    return StreamStatus.ACTIVE, dt.isoformat()
            
            # 15 分钟内无日志，处于静默期
            return StreamStatus.SILENT, None

        except Exception as e:
            logger.debug(f"探测日志流 {stream.id} 存活性发生异常: {e}")
            return StreamStatus.UNKNOWN, None

    async def search(
        self,
        keyword: str = "",
        resource_type: Optional[str] = None,
        limit: int = 10,
        check_liveness: bool = True,
    ) -> List[Dict[str, Any]]:
        """按关键词与资源类型在日志流目录中检索匹配的日志流，并动态探活

        Args:
            keyword: 检索关键词 (如 "postgres", "系统", "dockerd", "loki", "syslog")
            resource_type: 资源类型 ("container", "system", "app", "all")
            limit: 最大返回条数
            check_liveness: 是否并发查询 Loki 校验近 15 分钟存活性
        """
        kw = (keyword or "").strip().lower()
        candidates: List[Tuple[int, LogStreamDefinition]] = []

        for stream in self._streams_list:
            if resource_type and resource_type != "all" and stream.resource_type != resource_type:
                continue

            if not kw:
                candidates.append((10, stream))
                continue

            score = 0
            s_id = stream.id.lower()
            s_name = stream.name.lower()
            s_desc = stream.description.lower()
            selector_values = [str(v).lower() for v in stream.selector.values()]

            # 评分优先级机制
            if kw == s_id:
                score += 100
            elif kw in s_id:
                score += 60

            if kw in s_name:
                score += 50

            if any(kw == val for val in selector_values):
                score += 45
            elif any(kw in val for val in selector_values):
                score += 30

            if kw in s_desc:
                score += 20

            if score > 0:
                candidates.append((score, stream))

        # 按得分从高到低排序截取
        candidates.sort(key=lambda x: x[0], reverse=True)
        top_streams = [s for _, s in candidates[:limit]]

        # 并发进行存活性检测
        if check_liveness and top_streams:
            liveness_results = await asyncio.gather(
                *[self.check_stream_liveness(s) for s in top_streams],
                return_exceptions=True
            )
        else:
            liveness_results = [(StreamStatus.UNKNOWN, None) for _ in top_streams]

        results = []
        for s, liveness in zip(top_streams, liveness_results):
            if isinstance(liveness, tuple):
                status, last_seen = liveness
            else:
                status, last_seen = StreamStatus.UNKNOWN, None

            results.append({
                "stream_id": s.id,
                "name": s.name,
                "description": s.description,
                "resource_type": s.resource_type,
                "selector": s.selector,
                "format": s.format.value,
                "status": status.value,
                "last_seen": last_seen,
                "supported_fields": s.capabilities,
            })

        logger.debug(f"LogStreamCatalog 检索: keyword='{keyword}', 命中 {len(results)} 个日志流")
        return results

    def build_logql(
        self,
        stream_id: str,
        level: Optional[str] = None,
        keywords: Optional[List[str]] = None,
        match_mode: str = "all",
    ) -> Tuple[str, LogStreamDefinition]:
        """受控 LogQL 模板引擎：将业务查询参数装配为标准的 LogQL 语句

        Args:
            stream_id: 日志流 ID (如 container.postgres)
            level: 日志级别 (如 ERROR, WARN, INFO)
            keywords: 关键词列表
            match_mode: 关键词匹配模式: "all" (AND关系) 或 "any" (OR关系)

        Returns:
            Tuple[str, LogStreamDefinition]: 生成的合法 LogQL 及日志流元数据
        """
        stream = self.get(stream_id)
        if not stream:
            raise KeyError(
                f"未知的日志流 ID '{stream_id}'。请先调用 search_log_streams 查看系统已注册的有效日志流。"
            )

        # 1. 组装基础 Label 选择器
        selector_dict = dict(stream.selector)

        # 如果日志系统原生支持 level 作为 Label (如 {job="backend", level="error"})
        if stream.level_in_label and level:
            selector_dict["level"] = f'(?i)^{level.strip()}$'

        logql = self._format_base_selector(selector_dict)

        # 2. 正文格式与日志级别匹配处理 (区分 RAW_TEXT 与 JSON)
        if level and not stream.level_in_label:
            level_clean = level.strip().upper()
            if stream.format == LogFormat.JSON:
                # JSON 解析器提取
                lvl_field = stream.field_mapping.get("level", "level")
                logql += f' | json | {lvl_field} =~ "(?i)^{level_clean}$"'
            else:
                # 纯文本正文级别正则匹配 (Loki 行过滤正则不支持 \b 单词边界，使用无序 alternation)
                if level_clean in ("ERROR", "ERR"):
                    pattern = r"(?i)(error|err|fatal|panic|critical|exception)"
                elif level_clean in ("WARN", "WARNING"):
                    pattern = r"(?i)(warn|warning)"
                elif level_clean in ("INFO", "INFORMATION"):
                    pattern = r"(?i)(info|information)"
                elif level_clean in ("DEBUG", "DBG"):
                    pattern = r"(?i)(debug|dbg|trace)"
                else:
                    pattern = rf"(?i){re.escape(level_clean)}"
                logql += f' |~ "{pattern}"'

        # 3. 关键词过滤流水线 (支持 AND 与 OR)
        if keywords:
            cleaned_kws = [k.strip() for k in keywords if k and k.strip()]
            if cleaned_kws:
                if match_mode.lower() == "any":
                    # OR 关系：使用单个正则表达式管道
                    escaped_kws = [re.escape(k) for k in cleaned_kws]
                    pattern = "(?i)(" + "|".join(escaped_kws) + ")"
                    logql += f' |~ "{pattern}"'
                else:
                    # ALL 关系：使用 LogQL 行级过滤流水线 (管道串联，性能更优)
                    for kw in cleaned_kws:
                        safe_kw = kw.replace('"', '\\"')
                        logql += f' |= "{safe_kw}"'

        return logql, stream

    def build_aggregation_logql(
        self,
        stream_id: str,
        window: str = "1m",
    ) -> Tuple[str, LogStreamDefinition]:
        """构建错误频次聚合指标 LogQL (采用 count_over_time 严格度量错误事件数量)

        Args:
            stream_id: 日志流 ID
            window: 聚合滑动窗口大小 (如 1m, 5m)
        """
        stream = self.get(stream_id)
        if not stream:
            raise KeyError(
                f"未知的日志流 ID '{stream_id}'。请先调用 search_log_streams 查看系统已注册的有效日志流。"
            )

        selector_str = self._format_base_selector(stream.selector)

        # 针对错误关键字的聚合过滤
        if stream.level_in_label:
            filter_part = '{job="' + stream.selector.get("job", "") + '", level=~"(?i)(error|err|fatal|panic)"}'
        elif stream.format == LogFormat.JSON:
            lvl_field = stream.field_mapping.get("level", "level")
            filter_part = f'{selector_str} | json | {lvl_field} =~ "(?i)(error|err|fatal|panic)"'
        else:
            filter_part = f'{selector_str} |~ "(?i)(error|err|fatal|panic|exception)"'

        # 聚合分组 Label
        group_labels = ["job"]
        if "container" in stream.selector:
            group_labels.append("container")

        by_str = ", ".join(group_labels)
        logql = f"sum by ({by_str}) (count_over_time({filter_part} [{window}]))"
        return logql, stream

    @staticmethod
    def _format_base_selector(selector: Dict[str, str]) -> str:
        """格式化基础 Label 选择器字符串，如 {job="syslog", server="biz-server"}"""
        pairs = []
        for k, v in sorted(selector.items()):
            if v.startswith("(?i)") or "=~" in v:
                pairs.append(f'{k}=~"{v}"')
            else:
                pairs.append(f'{k}="{v}"')
        return "{" + ", ".join(pairs) + "}"


# 全局单例
log_stream_catalog = LogStreamCatalog()
