"""Prometheus 异步 HTTP 客户端

基于 httpx.AsyncClient 实现连接池、硬超时控制、优雅降级与标准化异常映射。
支持 Instant Query、Range Query 与 Target 采集端探活。
"""

from typing import Any, Dict, Optional
import httpx
from loguru import logger

from app.config import config


class PrometheusError(Exception):
    """Prometheus 基础异常"""
    pass


class PrometheusConnectionError(PrometheusError):
    """无法连接到 Prometheus 服务 (服务宕机或网络中断)"""
    pass


class PrometheusQueryError(PrometheusError):
    """PromQL 查询执行异常 (语法错误或计算超时)"""
    pass


class PrometheusClient:
    """Prometheus 异步客户端单例"""

    def __init__(
        self,
        base_url: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
    ):
        self.base_url = (
            base_url or config.monitoring.prometheus.url
        ).rstrip("/")
        self.timeout = (
            timeout_seconds
            if timeout_seconds is not None
            else config.monitoring.prometheus.timeout_seconds
        )
        self._client: Optional[httpx.AsyncClient] = None
        logger.info(
            f"PrometheusClient 初始化: base_url={self.base_url}, timeout={self.timeout}s"
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
        """检查 Prometheus TSDB 实例健康状态 (探活 /-/ready)"""
        try:
            client = await self.get_client()
            resp = await client.get("/-/ready")
            return resp.status_code == 200
        except Exception as e:
            logger.warning(f"Prometheus 健康检查失败: {e}")
            return False

    async def query_instant(
        self,
        promql: str,
        time: Optional[float] = None,
    ) -> Dict[str, Any]:
        """执行瞬时单点查询 (POST /api/v1/query)

        Args:
            promql: 合法 PromQL 表达式
            time: 评估时间戳 (Unix 浮点秒)，默认为当前时刻
        """
        params: Dict[str, Any] = {"query": promql}
        if time is not None:
            params["time"] = str(time)

        return await self._request("POST", "/api/v1/query", data=params)

    async def query_range(
        self,
        promql: str,
        start: float,
        end: float,
        step: str = "1m",
    ) -> Dict[str, Any]:
        """执行区间时序查询 (POST /api/v1/query_range)

        Args:
            promql: 合法 PromQL 表达式
            start: 开始时间戳 (Unix 浮点秒)
            end: 结束时间戳 (Unix 浮点秒)
            step: 查询分辨率步长 (如 15s, 1m, 5m)
        """
        params = {
            "query": promql,
            "start": str(start),
            "end": str(end),
            "step": step,
        }
        return await self._request("POST", "/api/v1/query_range", data=params)

    async def get_targets(self, state: Optional[str] = None) -> Dict[str, Any]:
        """查询 Prometheus 抓取目标元数据与存活性 (GET /api/v1/targets)

        Args:
            state: 目标状态过滤，可选 'active', 'dropped', 'any'
        """
        params = {}
        if state:
            params["state"] = state
        return await self._request("GET", "/api/v1/targets", params=params)

    async def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """通用底层请求执行器"""
        client = await self.get_client()
        try:
            resp = await client.request(method, path, params=params, data=data)
            payload = resp.json()

            if resp.status_code != 200 or payload.get("status") != "success":
                err_msg = payload.get("error", resp.text)
                err_type = payload.get("errorType", "bad_request")
                raise PrometheusQueryError(
                    f"Prometheus 查询执行失败 [{err_type}]: {err_msg}"
                )

            return payload.get("data", {})

        except httpx.ConnectError as e:
            logger.error(f"无法连接到 Prometheus 服务 ({self.base_url}): {e}")
            raise PrometheusConnectionError(
                f"无法连接到 Prometheus 监控服务 ({self.base_url})，请确认 Prometheus 容器是否正在运行"
            ) from e
        except httpx.TimeoutException as e:
            logger.error(f"Prometheus 请求超时 (>{self.timeout}s): {e}")
            raise PrometheusQueryError(
                f"Prometheus 查询超时 (超过 {self.timeout} 秒)，请尝试缩短查询时间范围或增大 step 步长"
            ) from e
        except (PrometheusError, PrometheusConnectionError, PrometheusQueryError):
            raise
        except Exception as e:
            logger.error(f"Prometheus 请求发生非预期异常: {e}")
            raise PrometheusError(f"监控查询异常: {str(e)}") from e

    async def close(self):
        """关闭客户端连接池"""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            logger.info("PrometheusClient 异步连接池已安全关闭")


# 全局单例
prometheus_client = PrometheusClient()

