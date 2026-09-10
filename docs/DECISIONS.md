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
