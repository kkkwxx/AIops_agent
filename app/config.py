"""配置管理模块 - 纯粹显式层级化架构 (基于 YAML 与 Pydantic)"""

import os
import re
import yaml
from pathlib import Path
from typing import Dict, Any
from pydantic import BaseModel, Field
from dotenv import load_dotenv

# 加载 .env 环境变量
load_dotenv()


def _expand_env_vars(content: str) -> str:
    """支持 ${VAR:default} 与 ${VAR} 语法的环境变量插值"""
    pattern = re.compile(r'\$\{([^}:]+)(?::([^}]*))?\}')

    def replace(match):
        env_var = match.group(1)
        default_val = match.group(2) if match.group(2) is not None else ""
        return os.getenv(env_var, default_val)

    return pattern.sub(replace, content)


# ============================================================
# 1. 严格按树状结构定义的配置模型
# ============================================================

class ServerConfig(BaseModel):
    app_name: str = "SuperBizAgent"
    app_version: str = "1.2.1"
    debug: bool = True
    host: str = "0.0.0.0"
    port: int = 9900


class PostgresConfig(BaseModel):
    host: str = "localhost"
    port: int = 5432
    db: str = "super_biz_agent"
    user: str = "postgres"
    password: str = "super_biz_password"
    pool_min_size: int = 2
    pool_max_size: int = 10

    @property
    def conn_string(self) -> str:
        """PostgreSQL 原生连接字符串"""
        return f"postgresql://{self.user}:{self.password}@{self.host}:{self.port}/{self.db}"


class DatabaseConfig(BaseModel):
    postgres: PostgresConfig = Field(default_factory=PostgresConfig)


class LLMConfig(BaseModel):
    """通用 LLM 配置 (兼容任意支持 OpenAI API 标准的提供商: base_url + api_key)"""
    provider: str = "openai"
    base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    api_key: str | None = ""
    model: str = "qwen-max"
    temperature: float = 0.7
    max_tokens: int | None = 4096

    def get_effective_api_key(self, fallback_key: str = "") -> str:
        """获取生效的 API Key，若未配置 LLM_API_KEY 则自动回退到百炼 key"""
        if self.api_key and self.api_key.strip():
            return self.api_key.strip()
        return fallback_key.strip() if fallback_key else ""

    def get_effective_base_url(self, fallback_base: str = "") -> str:
        """获取生效的 Base URL"""
        if self.base_url and self.base_url.strip():
            return self.base_url.strip()
        return fallback_base.strip() if fallback_base else "https://dashscope.aliyuncs.com/compatible-mode/v1"

    def get_effective_model(self, fallback_model: str = "") -> str:
        """获取生效的 Model 名称"""
        if self.model and self.model.strip():
            return self.model.strip()
        return fallback_model.strip() if fallback_model else "qwen-max"


class DashScopeConfig(BaseModel):
    """阿里云百炼服务专属配置 (保留用于向量化 Embedding 与重排序 Rerank)"""
    api_key: str | None = ""
    api_base: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    embedding_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    chat_model: str = "qwen-max"
    embedding_model: str = "text-embedding-v4"


class ModelsConfig(BaseModel):
    llm: LLMConfig = Field(default_factory=LLMConfig)
    dashscope: DashScopeConfig = Field(default_factory=DashScopeConfig)


class RAGRetrievalConfig(BaseModel):
    dense_top_k: int = 10
    sparse_top_k: int = 10
    rrf_top_k: int = 10
    final_top_k: int = 3


class RAGRerankConfig(BaseModel):
    enabled: bool = True
    model: str = "gte-rerank"
    threshold: float = 0.45
    base_url: str = "https://dashscope.aliyuncs.com"


class RAGChunkingConfig(BaseModel):
    max_size: int = 800
    overlap: int = 100


class RAGConfig(BaseModel):
    model: str = "qwen-max"
    retrieval: RAGRetrievalConfig = Field(default_factory=RAGRetrievalConfig)
    rerank: RAGRerankConfig = Field(default_factory=RAGRerankConfig)
    chunking: RAGChunkingConfig = Field(default_factory=RAGChunkingConfig)


class MCPServerItem(BaseModel):
    transport: str = "streamable-http"
    url: str = ""


class PrometheusConfig(BaseModel):
    url: str = "http://localhost:9090"
    timeout_seconds: float = 5.0
    max_range_hours: int = 24
    max_series: int = 50
    max_points_per_series: int = 300


class MonitoringConfig(BaseModel):
    prometheus: PrometheusConfig = Field(default_factory=PrometheusConfig)


# ============================================================
# 2. 根配置对象（所见即所得，清晰分层）
# ============================================================

class AppConfig(BaseModel):
    server: ServerConfig = Field(default_factory=ServerConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    models: ModelsConfig = Field(default_factory=ModelsConfig)
    rag: RAGConfig = Field(default_factory=RAGConfig)
    mcp_servers: Dict[str, MCPServerItem] = Field(default_factory=dict)
    monitoring: MonitoringConfig = Field(default_factory=MonitoringConfig)


def load_config(config_path: str = "config.yaml") -> AppConfig:
    """加载并解析 YAML 配置文件"""
    yaml_file = Path(config_path)
    if not yaml_file.exists():
        # 如果文件不存在，返回默认配置
        return AppConfig()

    with open(yaml_file, "r", encoding="utf-8") as f:
        raw_text = f.read()

    expanded_text = _expand_env_vars(raw_text)
    data = yaml.safe_load(expanded_text) or {}
    return AppConfig(**data)


# 全局单例
config = load_config()
