"""基于 PostgreSQL (pgvector + GIN) 的企业级混合检索向量存储管理器
职责：
1. 双路混合召回：Dense 向量 (HNSW 余弦距离) + Sparse 全文 (GIN 倒排索引 + jieba 分词)
2. 倒数排名融合 (RRF: Reciprocal Rank Fusion)
3. 交叉编码语义重排 (DashScope gte-rerank) 与置信度阈值过滤
4. 幂等增量写入与原子文件旧切片清理
5. 完美适配 LangChain BaseRetriever 接口
"""

import hashlib
import json
import re
import uuid
import asyncio
from typing import Any, Dict, List, Optional
import jieba
from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever
from loguru import logger

from app.config import config
from app.core.postgres_client import postgres_manager
from app.services.rerank_service import rerank_service
from app.services.vector_embedding_service import vector_embedding_service


# 标点符号与特殊字符过滤正则
_PUNCTUATION_PATTERN = re.compile(r'[,，。！？、：；“”"\'()（）\[\]【】{}<>《》\n\r\t]+')

# 常见无意义停用词集合 (防止在 OR 全文检索中因泛词导致不相干分块虚假召回)
_STOP_WORDS = {
    "的", "了", "和", "是", "在", "有", "我", "你", "他", "她", "它", "们",
    "这", "那", "就", "也", "都", "而", "及", "与", "着", "或", "一个",
    "之", "等", "其", "于", "被", "把", "让", "给", "但", "但是", "如果",
    "因为", "所以", "并且", "而且", "可以", "能", "会", "对", "向", "从",
    "如何", "怎么", "怎样", "什么", "哪", "哪个", "哪些", "制作",
}


def extract_search_tokens(text: str) -> str:
    """提取用于 PostgreSQL 全文检索的词流 (结巴中文分词 + 精确保留英文代码/错误码/数字)

    Args:
        text: 待切词的原始文本

    Returns:
        str: 以空格分隔的分词结果字符串，供 to_tsvector('simple', ...) 处理
    """
    if not text or not text.strip():
        return ""
    # 使用 jieba 搜索引擎模式切词，粒度更细，召回率更高
    raw_words = jieba.cut_for_search(text)
    tokens = [
        w.strip()
        for w in raw_words
        if w.strip()
        and not _PUNCTUATION_PATTERN.fullmatch(w.strip())
        and w.strip().lower() not in _STOP_WORDS
    ]
    return " ".join(tokens)


def compute_content_hash(text: str) -> str:
    """计算文本的 SHA256 哈希值用于增量幂等更新"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class VectorStoreManager:
    """PostgreSQL pgvector 混合检索与向量存储管理器"""

    def __init__(self):
        self.dense_top_k = config.rag.retrieval.dense_top_k
        self.sparse_top_k = config.rag.retrieval.sparse_top_k
        self.rrf_top_k = config.rag.retrieval.rrf_top_k
        self.final_top_k = config.rag.retrieval.final_top_k
        logger.info(
            f"VectorStoreManager (PostgreSQL 混合检索) 初始化完成: "
            f"dense_top_k={self.dense_top_k}, sparse_top_k={self.sparse_top_k}, "
            f"rrf_top_k={self.rrf_top_k}, final_top_k={self.final_top_k}"
        )

    def add_documents(self, documents: List[Document]) -> List[str]:
        """批量添加文档切片到 PostgreSQL (支持幂等判重与批量插入)

        Args:
            documents: LangChain Document 切片列表

        Returns:
            List[str]: 成功写入的切片 ID 列表
        """
        if not documents:
            return []

        logger.info(f"开始批量写入 {len(documents)} 个分片到 PostgreSQL...")
        
        # 1. 准备文本与元数据
        texts = [doc.page_content for doc in documents]
        doc_ids = []
        rows_to_insert = []

        # 2. 批量生成密集语义向量
        try:
            embeddings = vector_embedding_service.embed_documents(texts)
        except Exception as e:
            logger.error(f"批量生成 Embedding 失败: {e}")
            raise RuntimeError(f"Embedding 服务异常: {e}") from e

        # 3. 组装待入库数据
        for i, doc in enumerate(documents):
            chunk_id = doc.metadata.get("id") or str(uuid.uuid4())
            document_id = (
                doc.metadata.get("_file_name")
                or doc.metadata.get("_source")
                or "unknown_document"
            )
            content_hash = compute_content_hash(doc.page_content)
            search_tokens = extract_search_tokens(doc.page_content)
            embedding = embeddings[i]
            metadata_json = json.dumps(doc.metadata, ensure_ascii=False)

            rows_to_insert.append((
                chunk_id,
                document_id,
                doc.page_content,
                content_hash,
                embedding,
                search_tokens,
                metadata_json,
            ))
            doc_ids.append(chunk_id)

        # 4. 执行 PostgreSQL 事务批量 Upsert
        pool = postgres_manager.get_sync_pool()
        upsert_sql = """
            INSERT INTO rag.knowledge_chunks 
            (id, document_id, content, content_hash, embedding, search_tokens, metadata)
            VALUES (%s, %s, %s, %s, %s::vector, %s, %s::jsonb)
            ON CONFLICT (id) DO UPDATE SET
                document_id = EXCLUDED.document_id,
                content = EXCLUDED.content,
                content_hash = EXCLUDED.content_hash,
                embedding = EXCLUDED.embedding,
                search_tokens = EXCLUDED.search_tokens,
                metadata = EXCLUDED.metadata,
                created_at = CURRENT_TIMESTAMP;
        """

        try:
            with pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.executemany(upsert_sql, rows_to_insert)
                conn.commit()
            logger.info(f"✅ 成功将 {len(rows_to_insert)} 个分片持久化至 PostgreSQL (rag.knowledge_chunks)")
            return doc_ids
        except Exception as e:
            logger.error(f"PostgreSQL 批量写入失败: {e}")
            raise RuntimeError(f"写入知识库失败: {e}") from e

    def delete_by_source(self, file_path: str) -> int:
        """根据源文件路径或文件名原子删除历史旧切片

        Args:
            file_path: 文件全路径或文件名

        Returns:
            int: 删除的记录数
        """
        pool = postgres_manager.get_sync_pool()
        delete_sql = """
            DELETE FROM rag.knowledge_chunks 
            WHERE document_id = %s 
               OR metadata->>'_source' = %s 
               OR metadata->>'_file_name' = %s;
        """
        try:
            with pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(delete_sql, (file_path, file_path, file_path))
                    deleted_count = cur.rowcount
                conn.commit()
            logger.info(f"原子删除旧切片完成: file={file_path}, 影响行数={deleted_count}")
            return deleted_count
        except Exception as e:
            logger.warning(f"删除旧切片失败 (首次索引或无历史记录): {e}")
            return 0

    def hybrid_search(self, query: str, top_k: Optional[int] = None) -> List[Document]:
        """执行全流程混合检索：Dense + Sparse -> RRF 排名融合 -> gte-rerank 重排过滤

        Args:
            query: 用户提问
            top_k: 最终返回的最大文档数（默认读取 final_top_k）

        Returns:
            List[Document]: 经过交叉编码精排与阈值过滤的高质量文档列表
        """
        if not query or not query.strip():
            return []

        final_limit = top_k if top_k is not None else self.final_top_k
        pool = postgres_manager.get_sync_pool()

        dense_candidates: List[Dict[str, Any]] = []
        sparse_candidates: List[Dict[str, Any]] = []

        # ========================================================
        # 1. 第一路：Dense 语义向量检索 (HNSW 索引 + 余弦距离)
        # ========================================================
        try:
            query_vector = vector_embedding_service.embed_query(query)
            dense_sql = """
                SELECT id, document_id, content, metadata,
                       1 - (embedding <=> %s::vector) AS dense_similarity
                FROM rag.knowledge_chunks
                ORDER BY embedding <=> %s::vector
                LIMIT %s;
            """
            with pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(dense_sql, (query_vector, query_vector, self.dense_top_k))
                    rows = cur.fetchall()
                    for rank, row in enumerate(rows, start=1):
                        dense_candidates.append({
                            "id": row[0],
                            "document_id": row[1],
                            "content": row[2],
                            "metadata": row[3] if isinstance(row[3], dict) else json.loads(row[3]),
                            "dense_similarity": float(row[4]),
                            "dense_rank": rank,
                        })
            logger.debug(f"Dense 向量检索完成: 召回 {len(dense_candidates)} 条候选")
        except Exception as e:
            logger.warning(f"Dense 向量检索降级 (可能未配置 API Key 或网络抖动): {e}")

        # ========================================================
        # 2. 第二路：Sparse 关键词全文检索 (GIN 索引 + jieba 分词)
        # ========================================================
        search_query_tokens = extract_search_tokens(query)
        if search_query_tokens.strip():
            try:
                # 提取有效检索词并构造 OR 析取查询表达式 (term1 | term2 | ...)
                # 使用 | 连接词项能保证即使只命中部分关键词也能召回候选，由 ts_rank_cd 依据词频与覆盖度打分
                raw_terms = search_query_tokens.split()
                clean_terms = [
                    re.sub(r"[^a-zA-Z0-9_\u4e00-\u9fa5]", "", t)
                    for t in raw_terms
                ]
                valid_terms = [t for t in clean_terms if t]

                if valid_terms:
                    ts_query_str = " | ".join(valid_terms)
                    sparse_sql = """
                        SELECT id, document_id, content, metadata,
                               ts_rank_cd(tsv, to_tsquery('simple', %s)) AS sparse_rank_score
                        FROM rag.knowledge_chunks
                        WHERE tsv @@ to_tsquery('simple', %s)
                        ORDER BY sparse_rank_score DESC
                        LIMIT %s;
                    """
                    with pool.connection() as conn:
                        with conn.cursor() as cur:
                            cur.execute(sparse_sql, (ts_query_str, ts_query_str, self.sparse_top_k))
                            rows = cur.fetchall()
                            for rank, row in enumerate(rows, start=1):
                                sparse_candidates.append({
                                    "id": row[0],
                                    "document_id": row[1],
                                    "content": row[2],
                                    "metadata": row[3] if isinstance(row[3], dict) else json.loads(row[3]),
                                    "sparse_score": float(row[4]),
                                    "sparse_rank": rank,
                                })
                    logger.debug(f"Sparse 全文检索完成: 召回 {len(sparse_candidates)} 条候选")
            except Exception as e:
                logger.warning(f"Sparse 全文检索执行异常: {e}")

        # ========================================================
        # 3. 倒数排名融合 (RRF: Reciprocal Rank Fusion)
        # 融合公式: RRF_score(d) = sum( 1 / (60 + rank_i) )
        # ========================================================
        k_rrf = 60
        fused_pool: Dict[str, Dict[str, Any]] = {}

        # 累加 Dense 分数
        for item in dense_candidates:
            cid = item["id"]
            if cid not in fused_pool:
                fused_pool[cid] = {
                    "id": cid,
                    "content": item["content"],
                    "metadata": dict(item["metadata"]),
                    "rrf_score": 0.0,
                }
            fused_pool[cid]["rrf_score"] += 1.0 / (k_rrf + item["dense_rank"])
            fused_pool[cid]["metadata"]["dense_rank"] = item["dense_rank"]
            fused_pool[cid]["metadata"]["dense_similarity"] = round(item["dense_similarity"], 4)

        # 累加 Sparse 分数
        for item in sparse_candidates:
            cid = item["id"]
            if cid not in fused_pool:
                fused_pool[cid] = {
                    "id": cid,
                    "content": item["content"],
                    "metadata": dict(item["metadata"]),
                    "rrf_score": 0.0,
                }
            fused_pool[cid]["rrf_score"] += 1.0 / (k_rrf + item["sparse_rank"])
            fused_pool[cid]["metadata"]["sparse_rank"] = item["sparse_rank"]
            fused_pool[cid]["metadata"]["sparse_score"] = round(item["sparse_score"], 4)

        if not fused_pool:
            logger.warning(f"双路检索均未命中任何候选记录: query='{query}'")
            return []

        # 转换为 Document 列表并按 RRF 得分降序排序
        rrf_sorted = sorted(fused_pool.values(), key=lambda x: x["rrf_score"], reverse=True)
        rrf_candidates: List[Document] = []
        for item in rrf_sorted[:self.rrf_top_k]:
            meta = item["metadata"]
            meta["id"] = item["id"]
            meta["rrf_score"] = round(item["rrf_score"], 6)
            rrf_candidates.append(Document(page_content=item["content"], metadata=meta))

        logger.info(
            f"RRF 双路融合完成: Dense={len(dense_candidates)}, Sparse={len(sparse_candidates)}, "
            f"融合去重候选={len(fused_pool)}, 截取待精排={len(rrf_candidates)}"
        )

        # ========================================================
        # 4. 交叉编码语义重排序 (DashScope gte-rerank) 与阈值截断
        # ========================================================
        final_documents = rerank_service.rerank(
            query=query,
            documents=rrf_candidates,
            top_n=final_limit,
        )
        return final_documents

    async def async_hybrid_search(self, query: str, top_k: Optional[int] = None) -> List[Document]:
        """异步混合检索包装器 (在独立工作线程中执行以防阻塞事件循环)"""
        return await asyncio.to_thread(self.hybrid_search, query, top_k)

    def similarity_search(self, query: str, k: int = 3) -> List[Document]:
        """兼容原有 LangChain VectorStore 的相似度搜索接口"""
        return self.hybrid_search(query=query, top_k=k)

    def get_vector_store(self) -> "VectorStoreManager":
        """获取向量存储实例（用于向后兼容原有调用链路）"""
        return self

    def as_retriever(self, search_kwargs: Optional[dict] = None) -> BaseRetriever:
        """获取适配 LangChain 的标准 Retriever 检索器对象"""
        kwargs = search_kwargs or {}
        k = kwargs.get("k", self.final_top_k)
        return PgVectorHybridRetriever(top_k=k)


class PgVectorHybridRetriever(BaseRetriever):
    """适配 LangChain 接口的标准混合检索器实现"""

    top_k: int = 3

    def _get_relevant_documents(self, query: str, *, run_manager=None) -> List[Document]:
        """同步检索接口实现"""
        return vector_store_manager.hybrid_search(query=query, top_k=self.top_k)

    async def _aget_relevant_documents(self, query: str, *, run_manager=None) -> List[Document]:
        """异步检索接口实现"""
        return await vector_store_manager.async_hybrid_search(query=query, top_k=self.top_k)


# 全局单例
vector_store_manager = VectorStoreManager()
