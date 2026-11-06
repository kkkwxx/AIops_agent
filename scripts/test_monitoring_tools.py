#!/usr/bin/env python3
"""通用受控监控工具包端到端验证测试脚本

测试内容：
1. Metric Catalog 目录语义检索 (search_metrics: "cpu", "内存", 无关词)
2. 通用指标时序范围查询 (query_metrics: range 模式，统计值与采样点)
3. 瞬时指标查询与阈值评估 (query_metrics: instant 模式)
4. 空数据优雅降级与排查建议 (query_metrics: no_data 异常标签过滤)
5. Exporter 探活与根因诊断 (get_target_health: UP 与 DOWN 检测)
6. 原生 PromQL 安全防线 (advanced_promql_query: 只读放行与危险关键字拦截)
"""

import asyncio
import json
import sys
from pathlib import Path

# 项目根路径加入模块搜索
project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from loguru import logger
from app.tools.monitoring_tools import (
    advanced_promql_query,
    get_target_health,
    query_metrics,
    search_metrics,
)


def print_divider(title: str):
    print("\n" + "=" * 60)
    print(f"  {title}")
    print("=" * 60)


async def test_search_metrics():
    print_divider("测试 1: search_metrics 指标目录语义检索")

    # 1.1 检索 CPU
    logger.info("--> 检索 'cpu' 相关指标")
    res_cpu = json.loads(search_metrics.invoke({"keyword": "cpu"}))
    assert res_cpu.get("total_matches", 0) > 0, "应找到 CPU 相关指标"
    cpu_ids = [m["metric_id"] for m in res_cpu.get("results", [])]
    logger.info(f"匹配到 {res_cpu['total_matches']} 个 CPU 指标: {cpu_ids[:3]}")
    assert "node.cpu.usage" in cpu_ids

    # 1.2 检索 内存 (中文语义匹配)
    logger.info("--> 检索 '内存' 相关指标 (中文支持)")
    res_mem = json.loads(search_metrics.invoke({"keyword": "内存"}))
    assert res_mem.get("total_matches", 0) > 0, "应找到内存相关指标"
    mem_ids = [m["metric_id"] for m in res_mem.get("results", [])]
    logger.info(f"匹配到 {res_mem['total_matches']} 个内存指标: {mem_ids[:3]}")
    assert "node.memory.usage" in mem_ids

    # 1.3 检索不存在的指标
    logger.info("--> 检索不存在的关键字 'quantum_flux'")
    res_none = json.loads(search_metrics.invoke({"keyword": "quantum_flux"}))
    assert res_none.get("total_matches") == 0
    logger.info("未匹配到指标，符合预期 (无幻觉)")

    print("✅ 测试 1 通过: 指标目录检索与防幻觉机制正常")


async def test_query_metrics_range():
    print_divider("测试 2: query_metrics 范围查询 (Range Query)")

    logger.info("--> 查询 node.cpu.usage (最近 30m)")
    raw = await query_metrics.ainvoke({
        "metric_id": "node.cpu.usage",
        "time_range": "30m",
        "query_mode": "range",
        "step": "1m",
    })
    res = json.loads(raw)
    assert res.get("status") == "success", f"查询应成功: {res}"
    series = res.get("series", [])
    assert len(series) > 0, "应当返回至少一条时间序列"

    item = series[0]
    stats = item.get("summary", {})
    logger.info(f"序列标签: {item.get('labels')}")
    logger.info(f"统计指标: latest={stats.get('latest')}{res['unit']}, avg={stats.get('avg')}{res['unit']}, "
                f"max={stats.get('max')}{res['unit']}, min={stats.get('min')}{res['unit']}, p95={stats.get('p95')}{res['unit']}")
    logger.info(f"采样点数: {len(item.get('points_preview', []))} (已防爆截断)")

    assert stats.get("latest") is not None
    assert stats.get("avg") is not None
    print("✅ 测试 2 通过: 范围查询与五数统计分析正常")


async def test_query_metrics_instant():
    print_divider("测试 3: query_metrics 瞬时查询 (Instant Query)")

    logger.info("--> 瞬时查询 node.memory.usage")
    raw = await query_metrics.ainvoke({
        "metric_id": "node.memory.usage",
        "query_mode": "instant",
    })
    res = json.loads(raw)
    assert res.get("status") == "success", f"瞬时查询应成功: {res}"
    series = res.get("series", [])
    assert len(series) > 0

    item = series[0]
    val = item.get("latest_value")
    alert = res.get("alert_assessment", {})
    logger.info(f"当前内存使用率: {val}{res['unit']}")
    logger.info(f"自动阈值健康评估: level={alert.get('level')}, desc={alert.get('description')}")
    assert val is not None
    assert alert.get("level") in ["normal", "warning", "critical"]

    print("✅ 测试 3 通过: 瞬时单点值查询与阈值自动研判正常")


async def test_query_metrics_no_data():
    print_divider("测试 4: query_metrics 空数据优雅降级与诊断建议")

    logger.info("--> 传入不存在的 instance 标签过滤")
    raw = await query_metrics.ainvoke({
        "metric_id": "node.cpu.usage",
        "filters": {"instance": "non_existent_host_12345"},
        "time_range": "15m",
    })
    res = json.loads(raw)
    assert res.get("status") == "no_data", f"应当返回 no_data 状态: {res}"
    assert "diagnostic_hint" in res
    logger.info(f"诊断提示内容: {res['diagnostic_hint']}")
    logger.info(f"原因分类: {res.get('reason')}")

    print("✅ 测试 4 通过: 空数据不误报异常，提供可解释的诊断排查建议")


async def test_get_target_health():
    print_divider("测试 5: get_target_health 采集端探活诊断")

    logger.info("--> 探活所有 Target 状态")
    raw = await get_target_health.ainvoke({})
    res = json.loads(raw)
    assert res.get("status") == "success", f"探活应成功: {res}"

    total = res.get("total_targets", 0)
    up_cnt = res.get("up_count", 0)
    down_cnt = res.get("down_count", 0)
    logger.info(f"采集端总数: {total}, 正常(UP): {up_cnt}, 异常(DOWN): {down_cnt}")

    targets = res.get("targets", [])
    up_targets = [t["instance"] for t in targets if t["health"] == "UP"]
    down_targets = [t for t in targets if t["health"] == "DOWN"]

    logger.info(f"正常运行的目标: {up_targets}")
    assert len(up_targets) > 0, "本地 rocky-linux-local 节点采集端应为 UP"

    if down_targets:
        bad = down_targets[0]
        logger.info(f"发现异常目标: {bad['job']} ({bad['instance']}), 报错原因: {bad['last_error']}")

    print("✅ 测试 5 通过: 采集端探活状态与宕机根因提取正常")


async def test_advanced_promql():
    print_divider("测试 6: advanced_promql_query 安全只读防线")

    # 6.1 合法只读 PromQL
    logger.info("--> 执行合法 PromQL: sum(node_load1)")
    raw_safe = await advanced_promql_query.ainvoke({
        "promql": "sum(node_load1)",
        "time_range": "10m",
    })
    res_safe = json.loads(raw_safe)
    assert res_safe.get("status") == "success", f"合法查询应成功: {res_safe}"
    logger.info("合法 PromQL 执行成功")

    # 6.2 危险/非法操作拦截
    logger.info("--> 执行危险指令: 'drop series' / 'delete'")
    raw_danger = await advanced_promql_query.ainvoke({
        "promql": "sum(node_load1); drop series",
    })
    res_danger = json.loads(raw_danger)
    assert res_danger.get("status") == "error", "应被安全网关拦截"
    logger.info(f"拦截安全提示: {res_danger.get('message')}")
    assert "安全策略拦截" in res_danger.get("message", "")

    print("✅ 测试 6 通过: 高级 PromQL 只读查询与恶意指令拦截机制正常")


async def main():
    print("=" * 60)
    print("   🚀 开始通用监控工具套件全量端到端验证测试")
    print("=" * 60)

    try:
        await test_search_metrics()
        await test_query_metrics_range()
        await test_query_metrics_instant()
        await test_query_metrics_no_data()
        await test_get_target_health()
        await test_advanced_promql()

        print("\n" + "=" * 60)
        print("   🎉 全部 6 项监控工具验证测试顺利通过！")
        print("=" * 60)
    except Exception as e:
        logger.exception(f"测试失败: {e}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
