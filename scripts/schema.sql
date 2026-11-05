-- ============================================================
-- SuperBizAgent PostgreSQL 初始化 DDL 脚本
-- 职责：创建 pgvector 扩展、划分 Schema 命名空间、建立知识库表与索引
-- ============================================================

-- 1. 启用向量扩展
CREATE EXTENSION IF NOT EXISTS vector;

-- 2. 创建业务逻辑隔离的 Schema
CREATE SCHEMA IF NOT EXISTS rag;
CREATE SCHEMA IF NOT EXISTS langgraph;

-- 3. 知识库切片表
CREATE TABLE IF NOT EXISTS rag.knowledge_chunks (
    id VARCHAR(64) PRIMARY KEY,                         -- 切片唯一 ID (UUID 或 doc_id + chunk_idx)
    document_id VARCHAR(128) NOT NULL,                  -- 原始文件标识 (如 cpu_high_usage.md)
    content TEXT NOT NULL,                              -- 切片正文
    content_hash VARCHAR(64) NOT NULL,                  -- 内容 SHA256 哈希 (用于增量幂等更新)
    embedding vector(1024) NOT NULL,                   -- 1024 维 Dense 语义向量 (text-embedding-v4)
    search_tokens TEXT NOT NULL,                        -- jieba 中文分词 + 精确错误码/IP 提取结果
    tsv tsvector GENERATED ALWAYS AS (to_tsvector('simple', search_tokens)) STORED, -- 全文检索倒排索引字段
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,        -- 扩展元数据 (h1, h2, 文件路径等)
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- 4. 建立 HNSW 向量近似最近邻索引 (余弦距离)
CREATE INDEX IF NOT EXISTS idx_chunks_embedding 
ON rag.knowledge_chunks USING hnsw (embedding vector_cosine_ops);

-- 5. 建立全文检索 GIN 倒排索引 (加速关键词匹配)
CREATE INDEX IF NOT EXISTS idx_chunks_tsv 
ON rag.knowledge_chunks USING gin (tsv);

-- 6. 建立文档 ID 索引 (用于更新文档时原子删除旧切片)
CREATE INDEX IF NOT EXISTS idx_chunks_doc_id 
ON rag.knowledge_chunks (document_id);

-- 7. 建立哈希索引 (用于快速判断切片是否变动)
CREATE INDEX IF NOT EXISTS idx_chunks_hash 
ON rag.knowledge_chunks (content_hash);

