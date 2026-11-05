#!/usr/bin/env python3
"""AIOps 运维知识库文档切分与向量持久化独立入库脚本
职责：
1. 扫描知识库目录 (默认 aiops-docs/*.md)
2. 调用 document_splitter_service 执行 Markdown 两阶段层级切分
3. 原子清除各文档历史旧切片 (防数据残留)
4. 批量生成 1024 维 Dense 向量与 jieba 倒排分词词流
5. 事务级写入 PostgreSQL rag.knowledge_chunks 表并构建 HNSW 与 GIN 索引
"""

import argparse
import os
import sys
import time
from pathlib import Path

# 将项目根目录加入模块搜索路径
project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from loguru import logger
from langchain_core.documents import Document

from app.config import config
from app.services.document_splitter_service import document_splitter_service
from app.services.vector_store_manager import vector_store_manager
from app.services.vector_embedding_service import vector_embedding_service


def ingest_documents(docs_dir: str, mock_vector: bool = False, dry_run: bool = False):
    """执行知识库切分与入库流程

    Args:
        docs_dir: 文档目录路径
        mock_vector: 是否使用模拟向量 (用于离线无 API Key 环境测试存储与检索管线)
        dry_run: 演练模式 (仅切分统计，不写入数据库)
    """
    target_dir = Path(docs_dir).resolve()
    if not target_dir.exists() or not target_dir.is_dir():
        logger.error(f"❌ 目录不存在: {target_dir}")
        sys.exit(1)

    # 扫描支持的文件 (.md, .txt)
    files = sorted(list(target_dir.glob("*.md")) + list(target_dir.glob("*.txt")))
    if not files:
        logger.warning(f"⚠️ 在 {target_dir} 中未找到任何 .md 或 .txt 文档")
        return

    logger.info("=" * 60)
    logger.info(f"🚀 开始执行 AIOps 知识库入库流程")
    logger.info(f"📁 目标目录: {target_dir}")
    logger.info(f"📄 待处理文档总数: {len(files)}")
    logger.info(f"⚙️ 模式: {'演练模式(Dry-run)' if dry_run else '生产写入模式'}, Mock 向量: {mock_vector}")
    logger.info("=" * 60)

    total_chunks = 0
    total_files_success = 0
    start_total_time = time.time()

    for idx, file_path in enumerate(files, 1):
        file_name = file_path.name
        logger.info(f"[{idx}/{len(files)}] 正在处理: {file_name}...")

        try:
            content = file_path.read_text(encoding="utf-8")
            if not content.strip():
                logger.warning(f"  ⚠️ 文件内容为空，跳过: {file_name}")
                continue

            # 1. 执行智能文档分块
            normalized_path = file_path.as_posix()
            chunks = document_splitter_service.split_document(content, normalized_path)
            logger.info(f"  ✂️ 切分完成: 得到 {len(chunks)} 个高质量分块")

            if not chunks:
                continue

            # 确保元数据完整
            for c_idx, chunk in enumerate(chunks):
                chunk.metadata["_file_name"] = file_name
                chunk.metadata["_source"] = normalized_path
                chunk.metadata["chunk_index"] = c_idx

            if dry_run:
                total_chunks += len(chunks)
                total_files_success += 1
                logger.info(f"  [Dry-run] 演练跳过数据库写入")
                continue

            # 2. 原子清理旧切片
            deleted_count = vector_store_manager.delete_by_source(file_name)
            if deleted_count > 0:
                logger.debug(f"  🧹 原子清理历史旧切片: {deleted_count} 条")

            # 3. 写入 PostgreSQL
            if mock_vector:
                import numpy as np
                fake_embeddings = [np.random.rand(1024).tolist() for _ in chunks]
                from unittest.mock import patch
                with patch.object(vector_embedding_service, 'embed_documents', return_value=fake_embeddings):
                    inserted_ids = vector_store_manager.add_documents(chunks)
            else:
                inserted_ids = vector_store_manager.add_documents(chunks)

            logger.info(f"  ✅ 成功持久化入库: {len(inserted_ids)} 个切片")
            total_chunks += len(chunks)
            total_files_success += 1

        except Exception as e:
            logger.error(f"  ❌ 处理文件 {file_name} 失败: {e}")
            if not mock_vector and "DASHSCOPE_API_KEY" in str(e):
                logger.error(
                    "\n💡 提示: 您尚未在 .env 中配置有效的 DASHSCOPE_API_KEY。\n"
                    "   如需在无 Key 状态下测试完整数据库入库与分词流水线，请追加参数: --mock-vector\n"
                    "   命令示例: .venv/bin/python scripts/ingest_docs.py --mock-vector\n"
                )
                sys.exit(1)

    elapsed = time.time() - start_total_time
    logger.info("=" * 60)
    logger.info(f"🎉 知识库入库任务完成!")
    logger.info(f"📊 成功文件: {total_files_success}/{len(files)}")
    logger.info(f"📦 累积分片: {total_chunks} 个")
    logger.info(f"⏱️ 总耗时: {elapsed:.2f} 秒")
    logger.info("=" * 60)


def main():
    parser = argparse.ArgumentParser(description="AIOps 运维知识库文档切分与向量入库脚本")
    parser.add_argument(
        "--dir",
        type=str,
        default="aiops-docs",
        help="待入库的文档目录路径 (默认: aiops-docs)",
    )
    parser.add_argument(
        "--mock-vector",
        action="store_true",
        help="使用随机 1024 维向量代替 DashScope API (用于离线/无 Key 测试)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="仅演练切分逻辑并打印统计，不写入数据库",
    )
    args = parser.parse_args()

    ingest_documents(
        docs_dir=args.dir,
        mock_vector=args.mock_vector,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()

