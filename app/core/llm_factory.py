"""LLM 工厂类

使用 LangChain ChatOpenAI 通过标准 OpenAI 兼容协议接入大语言模型。
支持所有提供 base_url + api_key 模式的国内外大模型，包括：
- 阿里云百炼 (通义千问): https://dashscope.aliyuncs.com/compatible-mode/v1
- DeepSeek: https://api.deepseek.com/v1 (例如 deepseek-chat, deepseek-reasoner)
- 月之暗面 Moonshot (Kimi): https://api.moonshot.cn/v1
- 智谱清言 (GLM-4): https://open.bigmodel.cn/api/paas/v4
- 硅基流动 SiliconFlow: https://api.siliconflow.cn/v1
- 本地部署 Ollama: http://localhost:11434/v1
- 本地部署 vLLM / SGLang / LocalAI: http://localhost:8000/v1
- 官方 OpenAI / Azure OpenAI
"""

from typing import Optional, Any
from langchain_openai import ChatOpenAI
from app.config import config
from loguru import logger


class LLMFactory:
    """通用 LLM 实例工厂 - 基于 OpenAI 兼容协议"""

    @staticmethod
    def create_chat_model(
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        streaming: bool = True,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        max_tokens: Optional[int] = None,
        **kwargs: Any,
    ) -> ChatOpenAI:
        """创建兼容 OpenAI 协议的 ChatOpenAI 模型实例

        参数生效优先级：
        显式传入参数 > config.models.llm > config.models.dashscope (回退百炼)
        """
        target_model = (
            model
            or config.models.llm.get_effective_model(fallback_model=config.rag.model)
        )
        target_base_url = (
            base_url
            or config.models.llm.get_effective_base_url(fallback_base=config.models.dashscope.api_base)
        )
        target_api_key = (
            api_key
            or config.models.llm.get_effective_api_key(fallback_key=config.models.dashscope.api_key)
        )
        target_temperature = (
            temperature
            if temperature is not None
            else config.models.llm.temperature
        )
        target_max_tokens = (
            max_tokens
            if max_tokens is not None
            else config.models.llm.max_tokens
        )

        if not target_api_key or target_api_key in ("your-api-key-here", "your_api_key"):
            logger.warning("⚠️ LLM API Key 未配置或为默认占位符，请在 .env 中设置 LLM_API_KEY 或 DASHSCOPE_API_KEY")
            # 填充占位符，避免 ChatOpenAI 在客户端初始化阶段抛出缺少 api_key 异常
            target_api_key = target_api_key or "sk-placeholder"

        logger.info(
            f"🤖 初始化通用 Chat 模型: model={target_model}, "
            f"base_url={target_base_url}, streaming={streaming}, "
            f"temperature={target_temperature}"
        )

        chat_args: dict[str, Any] = {
            "model": target_model,
            "base_url": target_base_url,
            "api_key": target_api_key,
            "temperature": target_temperature,
            "streaming": streaming,
            **kwargs,
        }

        if target_max_tokens:
            chat_args["max_tokens"] = target_max_tokens

        return ChatOpenAI(**chat_args)


# 全局 LLM 工厂实例
llm_factory = LLMFactory()
