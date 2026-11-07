"""
Planner 节点：制定执行计划
基于 LangGraph 官方教程实现
"""

from textwrap import dedent
from typing import Dict, Any, List
from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field
from loguru import logger

from app.config import config
from app.core.llm_factory import llm_factory
from app.tools import LOCAL_TOOLS, retrieve_knowledge
from app.agent.mcp_client import get_mcp_client_with_retry
from .state import PlanExecuteState
from .utils import format_tools_description


class Plan(BaseModel):
    """计划的输出格式"""
    steps: List[str] = Field(
        description="完成任务所需的不同步骤。这些步骤应该按顺序执行，每一步都建立在前一步的基础上。"
    )


# Planner 提示词
planner_prompt = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            dedent("""
                作为一个专家级别的规划者，你需要将复杂的任务分解为可执行的步骤。

                可用工具列表（用于制定计划时参考）：

                {tools_description}

                注意：你的职责是制定计划，实际的工具调用由 Executor 负责执行。

                {experience_context}

                对于给定的任务，请创建一个严谨的、逐步的排障计划。制定计划时请遵循以下最佳实践：
                1. **指标排查**：需要查询 CPU、内存、负载等性能指标时，如果指标 ID 尚未确定，应先使用 `search_metrics` 查找可用指标目录，再使用 `query_metrics` 查询时序走势（排查突刺使用 range 模式）。
                2. **日志排查**：需要排查服务或容器日志时，应先使用 `search_log_streams` 查找确认有效日志流 ID 及其活性状态，再使用 `query_logs` 对齐时间窗口过滤错误日志。
                3. **现场还原与影响面**：定位到具体错误日志后，使用 `get_log_context` 提取前后文还原现场 SQL 或调用链；评估故障波及范围时使用 `aggregate_log_errors` 统计错误频次与走势。
                4. **应急处置与方案**：涉及应急止血措施、配置优化或排障流程时，优先使用 `retrieve_knowledge` 检索内部知识库 SOP 文档。
                5. 每个步骤应明确使用哪个工具及关键参数（如 metric_id, stream_id, 时间窗口等）。步骤之间保持清晰因果推进。

                示例输入："分析当前系统的性能问题"
                示例输出：
                步骤1: 使用 search_metrics 工具查找 CPU 和内存相关指标
                步骤2: 使用 query_metrics 工具查询最近 30 分钟 CPU 和内存使用率走势
                步骤3: 使用 search_log_streams 工具查找系统核心日志流
                步骤4: 使用 query_logs 工具检查对应时段的错误日志
                步骤5: 综合以上证据分析根因并生成排障报告
            """).strip(),
        ),
        ("placeholder", "{messages}"),
    ]
)


async def planner(state: PlanExecuteState) -> Dict[str, Any]:
    """
    规划节点：根据用户输入生成执行计划

    流程：
    1. 先查询内部文档，获取相关经验和最佳实践
    2. 基于经验文档和可用工具制定执行计划
    """
    logger.info("=== Planner：制定执行计划 ===")

    input_text = state.get("input", "")
    logger.info(f"用户输入: {input_text}")

    try:
        # 步骤1: 查询内部文档获取相关经验
        logger.info("查询内部文档，寻找相关经验...")
        experience_docs = ""
        try:
            # retrieve_knowledge 使用 response_format="content_and_artifact"
            # ainvoke() 只返回 content（字符串），不是元组
            context_str = await retrieve_knowledge.ainvoke({"query": input_text})
            if context_str and context_str.strip():
                experience_docs = context_str
                logger.info(f"找到相关经验文档，长度: {len(experience_docs)}")
            else:
                logger.info("未找到相关经验文档")
        except Exception as e:
            logger.warning(f"查询内部文档失败: {e}")

        # 步骤2: 获取可用工具列表
        # 获取本地工具
        local_tools = list(LOCAL_TOOLS)

        # 获取 MCP 工具
        mcp_client = await get_mcp_client_with_retry()
        mcp_tools = await mcp_client.get_tools()

        # 合并所有工具
        all_tools = local_tools + mcp_tools
        logger.info(f"可用工具数量: 本地 {len(local_tools)} + MCP {len(mcp_tools)}")

        # 格式化工具描述
        tools_description = format_tools_description(all_tools)

        # 步骤3: 格式化经验文档上下文
        if experience_docs:
            experience_context = dedent(f"""
                ## 相关经验文档

                以下是从知识库中检索到的相关经验和最佳实践，请参考这些经验制定执行计划：

                {experience_docs}

                ---
            """).strip()
        else:
            experience_context = ""

        # 步骤4: 创建 LLM 并生成计划 (使用通用 LLM 工厂)
        llm = llm_factory.create_chat_model(temperature=0)

        planner_chain = planner_prompt | llm.with_structured_output(Plan)

        # 调用 LLM 生成计划
        plan_result = await planner_chain.ainvoke({
            "messages": [("user", input_text)],
            "tools_description": tools_description,
            "experience_context": experience_context
        })

        # 提取步骤列表
        if isinstance(plan_result, Plan):
            plan_steps = plan_result.steps
        else:
            # 如果返回的是字典，提取 steps 字段
            plan_steps = plan_result.get("steps", [])  # type: ignore

        logger.info(f"计划已生成，共 {len(plan_steps)} 个步骤")
        for i, step in enumerate(plan_steps, 1):
            logger.info(f"  步骤{i}: {step}")

        return {"plan": plan_steps}

    except Exception as e:
        logger.error(f"生成计划失败: {e}", exc_info=True)
        # 返回一个默认计划
        return {
            "plan": [
                "收集相关信息",
                "分析数据",
                "生成报告"
            ]
        }
