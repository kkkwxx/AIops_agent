"""Loki 异步 HTTP 客户端

基于 httpx.AsyncClient 实现连接池、硬超时控制、优雅降级与标准化异常映射。
支持 LogQL 范围查询 (query_range)、瞬时/指标聚合查询 (query)、标签元数据检索与健康探活。
"""

from typing import Any, Dict, List, Optional
import httpx
from loguru import logger

from app.config import config


class LokiError(Exception):
    """Loki 基础异常"""
    pass


class LokiConnectionError(LokiError):
    """无法连接到 Loki 服务 (服务未启动或网络中断)"""
    pass


class LokiQueryError(LokiError):
    """LogQL 查询执行异常 (语法错误、语法不支持或服务端超时)"""
    pass


class LokiClient:
    """Loki 异步客户端单例"""

    def __init__(
        self,
        base_url: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
    ):
        self.base_url = (
            base_url or config.logging.loki.url
        ).rstrip("/")
        self.timeout = (
            timeout_seconds
            if timeout_seconds is not None
            else config.logging.loki.timeout_seconds
        )
        self._client: Optional[httpx.AsyncClient] = None
        logger.info(
            f"LokiClient 初始化: base_url={self.base_url}, timeout={self.timeout}s"
        )

    async def get_client(self) -> httpx.AsyncClient:
        """获取或惰性初始化 httpx 异步连接池"""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(self.timeout, connect=2.0),
                limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
            )
        return self._client

    async def check_health(self) -> bool:
        """检查 Loki 实例健康状态 (探活 /ready)"""
        try:
            client = await self.get_client()
            resp = await client.get("/ready")
            return resp.status_code == 200 and resp.text.strip() == "ready"
        except Exception as e:
            logger.warning(f"Loki 健康检查失败: {e}")
            return False

    async def query_range(
        self,
        logql: str,
        start: Optional[int] = None,
        end: Optional[int] = None,
        limit: int = 100,
        direction: str = "backward",
        step: Optional[str] = None,
    ) -> Dict[str, Any]:
        """执行区间日志与聚合查询 (GET /loki/api/v1/query_range)

        Args:
            logql: 合法的 LogQL 查询表达式
            start: 开始时间戳 (Unix 纳秒整数)
            end: 结束时间戳 (Unix 纳秒整数)
            limit: 单次拉取最大日志条数限制
            direction: 扫描方向: 'backward' (最新优先) 或 'forward' (最旧优先)
            step: 针对指标聚合统计查询的步长，如 '1m', '15s'
        """
        params: Dict[str, Any] = {
            "query": logql,
            "limit": limit,
            "direction": direction,
        }
        if start is not None:
            params["start"] = str(start)
        if end is not None:
            params["end"] = str(end)
        if step is not None:
            params["step"] = step

        return await self._request("GET", "/loki/api/v1/query_range", params=params)

    async def query_instant(
        self,
        logql: str,
        time: Optional[int] = None,
        limit: int = 100,
        direction: str = "backward",
    ) -> Dict[str, Any]:
        """执行瞬时单点 LogQL 查询 (GET /loki/api/v1/query)

        Args:
            logql: 合法的 LogQL 表达式
            time: 评估时间戳 (Unix 纳秒整数)，默认为当前时刻
            limit: 最大返回行数
            direction: 扫描方向
        """
        params: Dict[str, Any] = {
            "query": logql,
            "limit": limit,
            "direction": direction,
        }
        if time is not None:
            params["time"] = str(time)

        return await self._request("GET", "/loki/api/v1/query", params=params)

    async def get_labels(self) -> List[str]:
        """获取系统当前已知的所有有效 Label 名称列表 (GET /loki/api/v1/labels)"""
        data = await self._request("GET", "/loki/api/v1/labels")
        return data if isinstance(data, list) else []

    async def get_series(
        self,
        match_patterns: List[str],
        start: Optional[int] = None,
        end: Optional[int] = None,
    ) -> List[Dict[str, str]]:
        """获取满足 Selector 的日志流元数据序列 (GET /loki/api/v1/series)

        Args:
            match_patterns: 匹配选择器数组，例如 ['{job="syslog"}']
            start: 开始时间戳 (纳秒)
            end: 结束时间戳 (纳秒)
        """
        params: Dict[str, Any] = {}
        if match_patterns:
            params["match[]"] = match_patterns
        if start is not None:
            params["start"] = str(start)
        if end is not None:
            params["end"] = str(end)

        data = await self._request("GET", "/loki/api/v1/series", params=params)
        return data if isinstance(data, list) else []

    async def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """底层 HTTP 请求执行器，统一处理超时、连接断开与服务端异常响应"""
        client = await self.get_client()
        try:
            resp = await client.request(method, path, params=params, data=data)
            payload = resp.json()

            if resp.status_code != 200 or payload.get("status") != "success":
                err_msg = payload.get("error", resp.text)
                err_type = payload.get("errorType", "bad_request")
                raise LokiQueryError(
                    f"Loki 查询执行失败 [{err_type}]: {err_msg}"
                )

            return payload.get("data", {})

        except httpx.ConnectError as e:
            logger.error(f"无法连接到 Loki 服务 ({self.base_url}): {e}")
            raise LokiConnectionError(
                f"无法连接到 Loki 日志服务 ({self.base_url})，请确认 Loki 容器是否正在运行"
            ) from e
        except httpx.TimeoutException as e:
            logger.error(f"Loki 请求超时 (>{self.timeout}s): {e}")
            raise LokiQueryError(
                f"Loki 查询超时 (超过 {self.timeout} 秒)，请尝试缩短查询时间范围或精简关键词过滤"
            ) from e
        except (LokiError, LokiConnectionError, LokiQueryError):
            raise
        except Exception as e:
            logger.error(f"Loki 请求发生非预期异常: {e}")
            raise LokiError(f"日志检索异常: {str(e)}") from e

    async def close(self):
        """安全关闭客户端连接池"""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            logger.info("LokiClient 异步连接池已安全关闭")


# 全局单例
loki_client = LokiClient()

