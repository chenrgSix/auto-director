# 设计决策记录

## ADR-010：页面配置与即时生效（2026-09-10，已采用）

用户要求将配置文件改为在线配置。“连接与设置”管理 ComfyUI、LLM/VLM、密钥和运行限制，保存后无需重启。优先级为在线配置 > 既有 ComfyUI 设置 > 环境变量/.env > 默认值；数据目录和网页来源属于启动参数。

在线配置在数据目录的 `runtime-settings.json` 中原子保存，文件权限 0600。密钥写入后不通过 GET/PATCH 响应回显；空输入保留，显式清除覆盖环境密钥。切换模型端点时须同时重新提供或清除已有密钥。进行中、取消尚未退出、UNKNOWN 作业存在时拒绝变更，避免混用服务与凭据。

## ADR-001：MVP 与事实层级（2026-09-10，已采用）

原文 §2 P0 与 §57 Phase 1/2 分阶段拆分了部分共同能力。本轮将两者作为整体 MVP 目标，Phase 3 进入后续清单；VLM 为可选外部能力，启用时必须真实检查图像，未启用不能声称视觉 QA 已通过。原文不改写；实现细则在本文和模块设计迭代，任务完成必须引用验证。

## ADR-002：单进程本地持久队列（已采用）

使用 FastAPI lifespan 管理 worker，SQLAlchemy + SQLite 保存任务和实体，asyncio 单 consumer 处理队列，渲染并发 1。当前无需 Redis，禁止多个 uvicorn workers 共享该本地队列。重启可诊断中断和保留产物，不盲目重发状态不确定的外部渲染。

## ADR-003：独立 Provider 与真实/模拟边界（已采用）

生产只启用真实 ComfyUI adapter 与 OpenAI-compatible JSON/Vision Provider（不绑定 Agent Framework）。模拟服务只存在于测试/验收夹具，不作为 UI 中的默认“生成”能力。未配置依赖时提供可读配置错误。后续其他协议 Provider 可按接口加入。

## ADR-004：角色协议归一化（已采用）

原文 §10、§30、附录 C 的 capability 字段形状不同；统一为 `supports_*` + `max_duration`。动态参数按 ComfyUI object_info 校验；length/frame_count 通过明确 transform 与 fps 换算，不把 duration 秒直接写进帧数。缺少尾帧能力显式降级 I2V；没有 reference 输入时使用文本 Bible 锚点并报告能力限制。

## ADR-005：前端与权限范围（已采用）

采用 React/TypeScript/Vite 与共享 CSS 实现 UI，按需求引入组件状态管理，不为建议技术列表强制添加尚不需要的依赖。默认 loopback 单用户，不具备公网多租户权限模型。媒体与数据库本地存储；公共网络 ComfyUI 需显式开关；密钥配置方式已由 ADR-010 升级为后端保存的页面配置，环境仍可提供初始值。

## ADR-006：SQLite 聚合记录（已采用）

MVP 使用 SQLAlchemy 的 versioned JSON records：Episode 聚合包含有序 Shots、plan、bible、continuity；Workflow、Asset、RenderJob、QAResult、设置独立记录并按 parent_id 索引。SQLite WAL、事务和 version CAS 保证更新原子性。此实现没有独立 Shot 外键表；跨记录资源约束由服务层检查，后续关系查询压力出现时再迁移规范化表。数据库不在网络等待期间保持事务。

## ADR-007：先实现、后接入真实服务（用户明确要求）

不在本机部署 ComfyUI、不下载模型，先完成全部 MVP 实现与本地测试。ComfyUI 协议核对官方 docs 与 Comfy-Org 源码。用户仅在工程交付完成后提供真实 API/模型，外部门禁保持未执行。

## ADR-008：参数编辑与内置模板能力（已采用）

动态表单编辑 object_info 中的标量参数，包括模型枚举、数值、文本和布尔值。嵌套 DynamicCombo JSON 保持原样随模板传递，当前不生成递归编辑器；需要修改时重新导入 API JSON。内置模板提供协议和执行起点，不能代替真实节点/权重兼容验收。SD1.5 默认模板仅靠文本锚点，视觉参考输入由兼容替换模板承担。

## ADR-009：验收工具与质量策略（已采用）

`make check` 是统一的本地工程门禁，CI 运行同一命令。浏览器交互通过隔离夹具和实际浏览器记录，没有保留未实现的 Playwright 命令或空测试套件。VLM 未配置或 QA 关闭时，High 不进行候选视觉比较；VLM 缺失时提示跳过视觉检查；主动关闭 QA 的选择保存在单集配置中。参考资产、关键帧和视频均来自配置的真实 ComfyUI，应用代码没有模拟生成分支。
