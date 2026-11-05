#!/usr/bin/env python3
"""AIOps 知识库端到端检索质量评测脚本
职责：
1. 预置真实运维排障测试用例集 (涵盖 CPU、磁盘、内存、慢响应、503 及无关问题)
2. 调用 VectorStoreManager 执行混合召回 (Dense + Sparse + RRF + Rerank)
3. 统计召回命中率 (Hit Rate)、延迟 (Latency) 与阈值过滤准确率
"""

import sys
import time
from pathlib import Path

# 将项目根目录加入模块搜索路径
project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from loguru import logger
from app.services.vector_store_manager import vector_store_manager


TEST_CASES = [
    {
        "query": "HighCPUUsage 告警 CPU使用率超过80% 排查步骤",
        "expected_doc": "cpu_high_usage.md",
        "description": "CPU 飙高排障指南",
    },
    {
        "query": "磁盘空间不足 磁盘使用率超过85% 怎么清理和扩容？",
        "expected_doc": "disk_high_usage.md",
        "description": "磁盘告警清理排障",
    },
    {
        "query": "JVM 堆内存占用过高 内存泄露 OOM 排查步骤",
        "expected_doc": "memory_high_usage.md",
        "description": "内存泄露排障",
    },
    {
        "query": "接口响应慢 延迟高 慢SQL排查步骤",
        "expected_doc": "slow_response.md",
        "description": "服务慢响应排障",
    },
    {
        "query": "ServiceUnavailable 503 报错处理 服务不可用",
        "expected_doc": "service_unavailable.md",
        "description": "503 服务不可用排障",
    },
    {
        "query": "如何制作正宗的四川宫保鸡丁？",
        "expected_doc": None,  # 预期应被阈值过滤为空，防止大模型幻觉
        "description": "无关噪音防幻觉测试",
    },
]


def run_evaluation():
    logger.info("=" * 75)
    logger.info("🧪 开始执行 AIOps 混合检索质量与准确率端到端评测")
    logger.info(f"📋 测试用例数: {len(TEST_CASES)} 个")
    logger.info("=" * 75)

    passed_count = 0
    total_latency_ms = 0.0

    print(f"\n{'序号':<4} | {'测试用例描述':<18} | {'耗时(ms)':<8} | {'召回数':<6} | {'Top-1 命中来源':<24} | {'结果'}")
    print("-" * 78)

    for i, case in enumerate(TEST_CASES, 1):
        query = case["query"]
        expected = case["expected_doc"]
        desc = case["description"]

        start_time = time.time()
        docs = vector_store_manager.hybrid_search(query, top_k=3)
        latency = (time.time() - start_time) * 1000
        total_latency_ms += latency

        top_source = "None"
        if docs:
            top_source = docs[0].metadata.get("_file_name", "unknown")

        # 判定标准
        if expected is None:
            # 噪音问题：没有命中或者得分极低视为通过
            is_pass = (len(docs) == 0) or (docs[0].metadata.get("rerank_score", 0.0) < 0.45)
        else:
            is_pass = (len(docs) > 0) and (top_source == expected)

        if is_pass:
            passed_count += 1
            status_str = "✅ PASS"
        else:
            status_str = "❌ FAIL"

        print(f"{i:<4} | {desc:<18} | {latency:6.1f}ms | {len(docs):<6} | {top_source:<24} | {status_str}")

    avg_latency = total_latency_ms / len(TEST_CASES)
    hit_rate = (passed_count / len(TEST_CASES)) * 100

    print("-" * 78)
    logger.info("=" * 75)
    logger.info(f"📊 评测汇总报告:")
    logger.info(f"   • 测试通过率 (Hit Rate): {hit_rate:.1f}% ({passed_count}/{len(TEST_CASES)})")
    logger.info(f"   • 平均检索延迟: {avg_latency:.2f} 毫秒")
    logger.info(f"   • 结论: {'🎉 混合检索系统表现完美，达到生产上线标准！' if hit_rate == 100 else '⚠️ 存在部分未命中项，请检查分词与分片'}")
    logger.info("=" * 75)


if __name__ == "__main__":
    run_evaluation()

