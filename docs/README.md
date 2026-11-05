# SuperBizAgent 智能运维系统 — 文档中心 (Docs)

欢迎查阅 SuperBizAgent 项目文档中心。本目录集中收纳了系统重构交接文档、会话流转与前后端交互协议规范，以及底层架构规划全景。

---

## 📚 文档目录索引

| 序号 | 文档名称 | 定位与核心内容 | 对应物理文件 |
| :---: | :--- | :--- | :--- |
| **01** | [**交接文档：架构重构与混合检索交付总结报告**](file:///root/go-study/super_oncall_agent/docs/01_交接文档_架构重构与混合检索交付总结.md) | **架构重构交付与交接核心**<br/>• 废弃 Milvus 并迁移至 PostgreSQL (pgvector) 的全过程<br/>• 双路混合检索 (Dense + jieba Sparse) 与 RRF + gte-rerank 重排架构<br/>• LangGraph 会话持久化与 58MB 内存级轻量化收益基准对比<br/>• 生产运维指令、全链路验证实测记录与文件变更清单 | [01_交接文档_架构重构与混合检索交付总结.md](file:///root/go-study/super_oncall_agent/docs/01_交接文档_架构重构与混合检索交付总结.md) |
| **02** | [**交互文档：会话流转与前后端交互协议规范**](file:///root/go-study/super_oncall_agent/docs/02_交互文档_会话流转与前后端交互协议.md) | **前后端交互与 Agent 状态机制**<br/>• 会话生命周期与 `AsyncPostgresSaver` Checkpoint 物理存储结构<br/>• 多轮追问与上下文检索核心原理解析 (`add_messages` Reducer)<br/>• 消息实体标识字段与角色划分机制 (`human`/`ai`/`tool`/`id`/`tool_calls`)<br/>• RESTful 接口规范 (`/chat`, `/chat_stream`, `/history`, `/clear`)<br/>• SSE 流式事件协议标准与前端 TypeScript 客户端开箱即用代码 | [02_交互文档_会话流转与前后端交互协议.md](file:///root/go-study/super_oncall_agent/docs/02_交互文档_会话流转与前后端交互协议.md) |
| **03** | [**规划文档：系统架构改造规划与实施全景**](file:///root/go-study/super_oncall_agent/docs/03_系统架构改造规划与实施全景.md) | **系统演进技术规格书**<br/>• 架构演进阶段路线图 (Phase 1 ~ Phase 4)<br/>• 数据库 DDL 设计与索引选型论证 (HNSW vs IVFFlat, GIN Inverted Index)<br/>• 容灾演练场景设计与故障自愈方案 | [03_系统架构改造规划与实施全景.md](file:///root/go-study/super_oncall_agent/docs/03_系统架构改造规划与实施全景.md) |

---

## 🚀 快速导航与查阅建议

- **如果你是接手运维/后端研发的同学**：请优先阅读 [01_交接文档_架构重构与混合检索交付总结.md](file:///root/go-study/super_oncall_agent/docs/01_交接文档_架构重构与混合检索交付总结.md) 的“核心技术改造点”与“生产运维与快速上手指南”。
- **如果你是前端研发/联调工程师**：请优先阅读 [02_交互文档_会话流转与前后端交互协议.md](file:///root/go-study/super_oncall_agent/docs/02_交互文档_会话流转与前后端交互协议.md)，重点关注 SSE 事件帧定义、多轮会话追问传递规范与前端 TypeScript 代码示例。
- **如果你需要了解知识库检索评测基准**：请参考根目录脚本 [scripts/eval_retrieval.py](file:///root/go-study/super_oncall_agent/scripts/eval_retrieval.py) 与交接文档中的实测对比表。

