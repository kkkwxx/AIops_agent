# 电商秒杀大促场景数据库锁超时与连接池耗尽应急处理 SOP

## 1. 故障现象与触发特征
- **业务表现**: 电商大促/秒杀活动准点开启后，下单结算接口响应时间骤增，网关大量出现 `HTTP 504 Gateway Timeout`，用户无法正常下单。
- **监控特征**: 
  - 主机 CPU 利用率与 1 分钟系统负载剧烈突刺；
  - 数据库活跃连接数急剧飙升至上限（如 100/100），应用连接池耗尽抛出 `ConnectionPoolTimeoutException`。
- **日志特征**:
  - PostgreSQL 错误日志频繁刷屏 `ERROR: canceling statement due to statement timeout`；
  - 伴随死锁报错 `ERROR: deadlock detected, Process ... waits for ExclusiveLock on tuple ... of relation product_stock`。

## 2. 根因分析
在高并发秒杀瞬间，海量请求同时执行同一热点商品（如 `product_id = 8848`）的库存扣减事务：
```sql
UPDATE product_stock SET stock = stock - 1 WHERE product_id = 8848 AND stock > 0;
```
由于没有在入口层实施分布式排队，数据库层面发生极其激烈的**单行行级排他锁（ExclusiveLock）争抢**。前序事务因并发冲突执行缓慢，后序事务锁等待超过数据库 `statement_timeout` 限制被强制切断。未释放的连接迅速占满数据库连接池，引发微服务级联雪崩。

## 3. 应急止血处置措施 (立刻执行)

### 3.1 API 网关层限流与降级
- **操作**: 在 API 网关（如 Spring Cloud Gateway / OpenResty）立即对热点秒杀接口开启基于 **Redis 令牌桶算法** 的紧急限流降级策略。
- **目标**: 将 90%~95% 的超额突刺流量在网关层直接拦截并返回“活动太火爆，请稍后再试”，仅放行匹配库存体量的有效流量进入后端，瞬间为数据库减压。

### 3.2 强制终止数据库挂起长事务 (释放连接与行锁)
- **操作**: 运维人员通过超级用户会话登录 PostgreSQL，查询并使用 `pg_terminate_backend` 强制杀掉运行时间超过 5 秒的挂起事务与死锁进程：
```sql
-- 查询阻塞在 product_stock 上的死锁长事务
SELECT pid, usename, client_addr, state, query_start, query 
FROM pg_stat_activity 
WHERE state != 'idle' 
  AND query LIKE '%product_stock%' 
  AND (now() - query_start) > interval '5 seconds';

-- 强制终止挂起的异常后端进程
SELECT pg_terminate_backend(pid) 
FROM pg_stat_activity 
WHERE state != 'idle' 
  AND query LIKE '%product_stock%' 
  AND (now() - query_start) > interval '5 seconds';
```

### 3.3 临时调大连接池容量
- **操作**: 
  - 临时将 PostgreSQL 的 `max_connections` 适当上调；
  - 微服务配置中心动态调大 HikariCP `maximum-pool-size`，缓解突发排队等待。

---

## 4. 架构优化与长期根治方案

### 4.1 Redis + Lua 脚本库存预扣减
- 绝不允许高并发请求直接穿透更新关系型数据库。
- 架构改造：秒杀库存预热存入 Redis，利用 Redis 单线程执行 Lua 脚本进行原子**预扣减**库存。扣减成功后直接向用户返回排队中，并通过消息队列（Kafka/RocketMQ）异步消费入库生成最终订单。

### 4.2 热点商品库存分桶 (Sub-stocking)
- 针对单个超高并发热点商品（如 8848），在数据库实施**库存分桶**设计。
- 例如将 1000 个库存拆分为 10 个批次桶（`bucket_id 1~10`，每桶 100 件）。下单请求哈希路由到不同分桶更新，将单一单行锁争夺稀释 10 倍，极大降低行锁冲突概率。

