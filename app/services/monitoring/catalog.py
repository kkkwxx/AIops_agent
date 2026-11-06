"""监控指标目录 (Metric Catalog) 管理器

职责：
1. 维护系统中经过验证的指标知识字典 (Metric Definition)
2. 提供 Agent 面向关键词、自然语言与资源类型的精准检索 (search_metrics)
3. 负责将业务参数和过滤条件 (filters) 安全组装为标准的 PromQL 查询语句
"""

import re
from typing import Dict, List, Optional, Tuple
from loguru import logger

from .catalog_definitions import BUILTIN_METRICS, MetricDefinition, QueryMode, ResourceType


class MetricCatalog:
    """指标目录单例管理器"""

    def __init__(self, metrics: Optional[List[MetricDefinition]] = None):
        self._metrics_list: List[MetricDefinition] = metrics or BUILTIN_METRICS
        self._metrics_map: Dict[str, MetricDefinition] = {m.id: m for m in self._metrics_list}
        logger.info(f"MetricCatalog 初始化完成: 成功加载 {len(self._metrics_list)} 个预置标准监控指标")

    def get(self, metric_id: str) -> Optional[MetricDefinition]:
        """根据指标唯一 ID 获取指标元数据"""
        return self._metrics_map.get(metric_id.strip())

    def list_all(self, resource_type: Optional[str] = None) -> List[MetricDefinition]:
        """获取所有指标列表，可按资源类型过滤"""
        if not resource_type or resource_type == "all":
            return list(self._metrics_list)
        return [m for m in self._metrics_list if m.resource_type.value == resource_type]

    def search(
        self,
        keyword: str,
        resource_type: Optional[str] = None,
        limit: int = 10,
    ) -> List[MetricDefinition]:
        """根据关键词检索指标 (支持中文名称、逻辑ID、描述与原生指标名)

        Args:
            keyword: 检索关键词 (如 "cpu", "内存", "负载", "disk", "goroutine")
            resource_type: 资源类别 ("node", "app", "exporter", "all")
            limit: 最大返回数量

        Returns:
            List[MetricDefinition]: 匹配的指标列表，按相关度排序
        """
        kw = keyword.strip().lower()
        if not kw:
            return self.list_all(resource_type)[:limit]

        matched: List[Tuple[int, MetricDefinition]] = []

        for m in self._metrics_list:
            # 资源类型过滤
            if resource_type and resource_type != "all" and m.resource_type.value != resource_type:
                continue

            score = 0
            m_id = m.id.lower()
            m_name = m.name.lower()
            m_desc = m.description.lower()
            m_prom_names = [n.lower() for n in m.metric_names]

            # 优先级打分机制
            if kw == m_id:
                score += 100
            elif kw in m_id:
                score += 60
            
            if kw in m_name:
                score += 50
            
            if any(kw == n for n in m_prom_names):
                score += 40
            elif any(kw in n for n in m_prom_names):
                score += 20

            if kw in m_desc:
                score += 10

            if score > 0:
                matched.append((score, m))

        # 按打分降序排列
        matched.sort(key=lambda item: item[0], reverse=True)
        results = [m for _, m in matched[:limit]]
        logger.debug(f"MetricCatalog 检索: keyword='{keyword}', 命中 {len(results)} 个指标")
        return results

    def build_promql(
        self,
        metric_id: str,
        filters: Optional[Dict[str, str]] = None,
        query_mode: QueryMode = QueryMode.RANGE,
    ) -> Tuple[str, MetricDefinition]:
        """将标准指标 ID 与业务过滤标签渲染为经过验证的合法 PromQL

        Args:
            metric_id: 标准指标逻辑 ID (如 "node.cpu.usage")
            filters: 标签字典 (如 {"instance": "rocky-linux-local"})
            query_mode: 查询模式 (INSTANT 或 RANGE)

        Returns:
            Tuple[str, MetricDefinition]: (渲染后的 PromQL 字符串, 指标元数据定义)

        Raises:
            KeyError: 当 metric_id 不存在时
            ValueError: 缺少必要必填标签时
        """
        metric = self.get(metric_id)
        if not metric:
            raise KeyError(f"未知指标 ID: '{metric_id}'，请先调用 search_metrics 查询可用指标目录")

        raw_filters = filters or {}

        # 检查必填标签
        for req_label in metric.required_labels:
            if req_label not in raw_filters:
                raise ValueError(
                    f"指标 '{metric_id}' 缺少必填过滤标签: '{req_label}'。可选标签: {metric.optional_labels}"
                )

        # 选取对应模式的模板
        template = metric.template_range if query_mode == QueryMode.RANGE else metric.template_instant

        # 格式化 labels 过滤器
        promql = self._inject_labels(template, raw_filters)
        return promql, metric

    @staticmethod
    def _inject_labels(template: str, filters: Dict[str, str]) -> str:
        """安全格式化并注入 PromQL 标签条件"""
        pairs = []
        for k, v in sorted(filters.items()):
            safe_k = re.sub(r'[^a-zA-Z0-9_]', '', k)
            safe_v = v.replace('"', '\\"')
            pairs.append(f'{safe_k}="{safe_v}"')
        label_str = ', '.join(pairs)

        if label_str:
            def repl_with(m):
                prefix = m.group(1).rstrip(', ')
                return '{' + ((prefix + ', ' + label_str) if prefix else label_str) + '}'
            return re.sub(r'\{([^}]*?)\{labels\}\}', repl_with, template)
        else:
            def repl_empty(m):
                prefix = m.group(1).rstrip(', ')
                return ('{' + prefix + '}') if prefix else ''
            return re.sub(r'\{([^}]*?)\{labels\}\}', repl_empty, template)


# 全局单例
metric_catalog = MetricCatalog()
