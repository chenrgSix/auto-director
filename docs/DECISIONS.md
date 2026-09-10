# 设计决策记录

## ADR-001：MVP 与事实层级（2026-09-10，已采用）

原文 §2 P0 与 §57 Phase 1/2 分阶段拆分了部分共同能力。本轮将两者作为整体 MVP 目标，Phase 3 进入后续清单；VLM 为可选外部能力，启用时必须真实检查图像，未启用不能声称视觉 QA 已通过。原文不改写；实现细则在本文和模块设计迭代，任务完成必须引用验证。

## ADR-002：单进程本地持久队列（已采用）

使用 FastAPI lifespan 管理 worker，SQLAlchemy + SQLite 保存任务和实体，asyncio 单 consumer 处理队列，渲染并发 1。当前无需 Redis，禁止多个 uvicorn workers 共享该本地队列。重启可诊断中断和保留产物，不盲目重发状态不确定的外部渲染。

## ADR-003：独立 Provider 与真实/模拟边界（已采用）

生产只启用真实 ComfyUI adapter 与 OpenAI-compatible JSON/Vision Provider（不绑定 Agent Framework）。模拟服务只存在于测试/验收夹具，不作为 UI 中的默认“生成”能力。未配置依赖时提供可读配置错误。后续其他协议 Provider 可按接口加入。

## ADR-004：角色协议归一化（已采用）

原文 §10、§30、附录 C 的 capability 字段形状不同；统一为 `supports_*` + `max_duration`。动态参数按 ComfyUI object_info 校验；length/frame_count 通过明确 transform 与 fps 换算，不把 duration 秒直接写进帧数。缺少尾帧能力显式降级 I2V；没有 reference 输入时使用文本 Bible 锚点并报告能力限制。

## ADR-005：前端与权限范围（已采用）

采用 React/TypeScript/Vite 与共享 CSS 实现 UI，按需求引入组件状态管理，不为建议技术列表强制添加尚不需要的依赖。默认 loopback 单用户，不具备公网多租户权限模型。媒体与数据库本地存储；LLM 密钥仅环境配置，公共网络 ComfyUI 需显式开关。

## ADR-006：SQLite 聚合记录（已采用）

MVP 使用 SQLAlchemy 的 versioned JSON records：Episode 聚合包含有序 Shots、plan、bible、continuity；Workflow、Asset、RenderJob、设置独立记录并按 parent_id 索引。SQLite WAL、事务和 version CAS 保证更新原子性。此实现没有独立 Shot 外键表；跨记录资源约束由服务层检查，后续关系查询压力出现时再迁移规范化表。数据库不在网络等待期间保持事务。

## ADR-007：先实现、后接入真实服务（用户明确要求）

不在本机部署 ComfyUI、不下载模型，先完成全部 MVP 实现与本地测试。ComfyUI 协议核对官方 docs 与 Comfy-Org 源码。用户仅在工程交付完成后提供真实 API/模型，外部门禁保持未执行。
