"""FastAPI 应用入口

主应用程序，配置路由、中间件、静态文件等
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from contextlib import asynccontextmanager
import os

from app.config import config
from loguru import logger
from app.api import chat, health, file, aiops
from app.core.postgres_client import postgres_manager
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    # 启动时执行
    logger.info("=" * 60)
    logger.info(f"🚀 {config.server.app_name} v{config.server.app_version} 启动中...")
    logger.info(f"📝 环境: {'开发' if config.server.debug else '生产'}")
    logger.info(f"🌐 监听地址: http://{config.server.host}:{config.server.port}")
    logger.info(f"📚 API 文档: http://{config.server.host}:{config.server.port}/docs")
    
    # 连接 PostgreSQL (pgvector) 并确保 LangGraph 持久化表就绪
    logger.info("🔌 正在检查 PostgreSQL (pgvector) 连接...")
    is_healthy = await postgres_manager.health_check()
    if is_healthy:
        logger.info("✅ PostgreSQL 连接池初始化成功并就绪")
        try:
            await postgres_manager.get_checkpointer()
            logger.info("✅ LangGraph 会话持久化管理器就绪 (PostgreSQL)")
        except Exception as e:
            logger.warning(f"⚠️ LangGraph checkpointer 初始化提示: {e}")
    else:
        logger.warning("⚠️ PostgreSQL 数据库连接不可用，请检查容器状态")
    
    logger.info("=" * 60)
    
    yield
    
    # 关闭时执行
    logger.info("🔌 正在关闭 PostgreSQL 连接池...")
    await postgres_manager.close()
    logger.info(f"👋 {config.server.app_name} 安全退出")


# 创建 FastAPI 应用
app = FastAPI(
    title=config.server.app_name,
    version=config.server.app_version,
    description="基于 LangChain 的智能oncall运维系统",
    lifespan=lifespan
)

# 配置 CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 生产环境应该限制具体域名
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册路由
app.include_router(health.router, tags=["健康检查"])
app.include_router(chat.router, prefix="/api", tags=["对话"])
app.include_router(file.router, prefix="/api", tags=["文件管理"])
app.include_router(aiops.router, prefix="/api", tags=["AIOps智能运维"])

# 挂载静态文件
static_dir = "static"
app.mount("/static", StaticFiles(directory=static_dir), name="static")

@app.get("/")
async def root():
    """返回首页"""
    index_path = os.path.join(static_dir, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return {
        "message": f"Welcome to {config.server.app_name} API",
        "version": config.server.app_version,
        "docs": "/docs"
    }


if __name__ == "__main__":
    import uvicorn
    
    uvicorn.run(
        "app.main:app",
        host=config.server.host,
        port=config.server.port,
        reload=config.server.debug,
        log_level="info"
    )
