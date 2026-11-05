"""RAG 重排序服务模块 (DashScope text-rerank REST API)
职责：对多路召回候选集进行高精度语义交叉重排序，并提供阈值过滤与网络异常本地降级能力。

直接调用 REST 接口（而非 dashscope SDK 的 legacy TextReRank），以同时兼容
公网端点 (https://dashscope.aliyuncs.com) 与百炼工作空间 MaaS 专属端点
(https://ws-xxx.cn-beijing.maas.aliyuncs.com)。
"""

from typing import List, Optional

import httpx
from langchain_core.documents import Document
from loguru import logger

from app.config import config

# text-rerank 原生 API 路径（公网与 MaaS 工作空间端点一致）
RERANK_API_PATH = "/api/v1/services/rerank/text-rerank/text-rerank"


class RerankService:
    """基于 DashScope text-rerank REST API 的重排序服务"""

    def __init__(self):
        self.enabled = config.rag.rerank.enabled
        self.model = config.rag.rerank.model
        self.base_url = (config.rag.rerank.base_url or "https://dashscope.aliyuncs.com").rstrip("/")
        self.default_threshold = config.rag.rerank.threshold
        self.default_top_n = config.rag.retrieval.final_top_k
        self.api_key = config.models.dashscope.api_key

    def _is_api_key_valid(self) -> bool:
        """检查 API Key 是否有效配置"""
        return bool(self.api_key and self.api_key != "your-api-key-here" and len(self.api_key) > 5)

    def rerank(
        self,
        query: str,
        documents: List[Document],
        top_n: Optional[int] = None,
        threshold: Optional[float] = None,
    ) -> List[Document]:
        """对候选文档列表执行交叉编码重排序

        Args:
            query: 用户提问
            documents: 混合召回得到的候选文档列表
            top_n: 最终返回的最大文档数（默认使用配置中的 final_top_k）
            threshold: 最小相关性得分阈值（0.0 ~ 1.0，低于此分数的文档被丢弃）

        Returns:
            List[Document]: 重排序并经过阈值过滤后的文档列表（按得分由高到低）
        """
        if not documents:
            return []

        target_top_n = top_n if top_n is not None else self.default_top_n
        target_threshold = threshold if threshold is not None else self.default_threshold

        # 1. 如果配置未启用重排序，直接根据既有顺序截断
        if not self.enabled:
            logger.debug("Rerank 服务已在配置中禁用，使用候选集原始顺序截断")
            return documents[:target_top_n]

        # 2. 如果未配置有效 API Key，优雅降级为本地顺序截断（不抛异常打断流程）
        if not self._is_api_key_valid():
            logger.warning("DashScope API Key 未配置或为默认占位符，rerank 自动降级为 RRF 既有顺序")
            return documents[:target_top_n]

        # 3. 提取候选文本列表
        doc_texts = [doc.page_content for doc in documents]

        try:
            logger.debug(
                f"调用 text-rerank: model={self.model}, base_url={self.base_url}, "
                f"query='{query}', 候选文档数={len(documents)}"
            )
            response = httpx.post(
                f"{self.base_url}{RERANK_API_PATH}",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.model,
                    "input": {"query": query, "documents": doc_texts},
                    "parameters": {
                        "top_n": len(documents),  # 全部打分，以便后续按 threshold 过滤
                        "return_documents": False,
                    },
                },
                timeout=30.0,
            )

            if response.status_code == 200:
                results = response.json().get("output", {}).get("results", [])
                ranked_docs: List[Document] = []

                for item in results:
                    score = float(item["relevance_score"])
                    idx = int(item["index"])
                    doc = documents[idx]
                    # 将 rerank 得分写入文档元数据，便于调试与可解释性
                    doc.metadata["rerank_score"] = round(score, 4)

                    if score >= target_threshold:
                        ranked_docs.append(doc)
                    else:
                        logger.debug(
                            f"文档被相似度阈值过滤: score={score:.4f} < threshold={target_threshold}, "
                            f"source={doc.metadata.get('_source', doc.metadata.get('document_id', 'unknown'))}"
                        )

                # 按 rerank 得分降序排序并截取 top_n
                ranked_docs.sort(key=lambda d: d.metadata.get("rerank_score", 0.0), reverse=True)
                final_results = ranked_docs[:target_top_n]
                logger.info(
                    f"rerank 完成: 原始候选 {len(documents)} 篇 -> 阈值过滤保留 {len(ranked_docs)} 篇 -> "
                    f"最终返回 {len(final_results)} 篇"
                )
                return final_results
            else:
                try:
                    detail = response.json()
                except Exception:
                    detail = response.text[:200]
                logger.warning(
                    f"rerank 接口返回非 200: status={response.status_code}, detail={detail}，"
                    f"自动降级为候选集原始排序"
                )
                return documents[:target_top_n]

        except Exception as e:
            logger.warning(f"rerank 服务调用异常: {e}，自动降级为候选集原始排序")
            return documents[:target_top_n]


# 全局单例
rerank_service = RerankService()
