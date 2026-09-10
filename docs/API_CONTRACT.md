# API 与工作流契约

## 基础约定

Base `/api/v1`，JSON；ID 由服务端生成；UTC ISO 时间。验证失败 422，不存在 404，状态冲突 409，外部执行失败提供可读 `error.code/message/details`。仅本地单用户使用，默认绑定 loopback；跨域与公网 ComfyUI 需要显式配置。

## API 清单（实现目标）

| 路径 | 行为 |
| --- | --- |
| `GET /health` | 本地进程健康 |
| `GET/PATCH /settings` | 非敏感配置读取与修改；密钥只由环境提供 |
| `GET /comfyui/status`, `GET /comfyui/system`, `POST /comfyui/test` | 连接、设备、节点/模型检查 |
| `GET /workflows`, `POST /workflows/import` | 列表、API JSON 导入 |
| `GET/PATCH/DELETE /workflows/{id}` | profile、bindings、capabilities、删除保护 |
| `POST /workflows/{id}/validate` | 检查节点、枚举模型、字段、输入角色 |
| `POST /workflows/{id}/test-run` | 参数/资产 ID 试跑，返回异步 RenderJob |
| `POST /workflows/{id}/default` | 设置图像或视频默认项 |
| `GET /jobs/{id}` | 作业状态、prompt_id、错误与资产 |
| `POST/GET /episodes`, `GET/DELETE /episodes/{id}` | 单集创建/列表/详情/可选资产清理 |
| `POST /episodes/{id}/generate`, `POST /episodes/{id}/cancel` | 入队/取消 |
| `GET /episodes/{id}/progress`, `GET /episodes/{id}/events` | 快照/SSE 进度 |
| `GET /episodes/{id}/shots`, `PATCH /episodes/{id}/timeline` | 镜头与排序/启用状态 |
| `POST /shots/{id}/retry[-keyframes\|-video]` | 定向重试 |
| `POST /episodes/{id}/compose` | 根据当前启用镜头重新导出 |
| `POST /episodes/{id}/assets`, `GET /assets/{id}/file` | 有界媒体上传/本地预览下载 |

## WorkflowProfile

统一 capability 命名为 `supports_start_frame/end_frame/video_reference/multi_reference` 与 `max_duration`。图像至少 `prompt` + `image` output；视频至少 `prompt/start_frame` + `video` output，声明支持尾帧时必须绑定 `end_frame`。缺尾帧只允许显式的 I2V capability 降级。

Binding 为 `{node_id, input, transform}`；`transform` 默认 `identity`，可指定 `duration_to_frames`（根据 fps 对齐模型帧数）。输出绑定到 node，不修改输入。角色从 `_meta.title` 中的 `(Input:role)` / `(Output:role)` 提取；`width_height` 展开。不明确的字段要求用户编辑绑定，不猜测固定节点位置。参数 schema 保留字段类型、默认值、min/max、step、enum 及绑定角色。

## 状态与错误

Episode 状态遵循原文 §36；增加 `QUEUED` 表示已入队。Shot 状态遵循 §37，增加 `STALE` 表示依赖已变。RenderJob 区分 `QUEUED/RUNNING/COMPLETED/FAILED/CANCELLED/UNKNOWN`，UNKNOWN 不等于失败或可以重发。

错误码保留 §52：COMFYUI_OFFLINE、WORKFLOW_INVALID、MISSING_NODE、MISSING_MODEL、OUT_OF_MEMORY、PROMPT_REJECTED、EXECUTION_ERROR、OUTPUT_NOT_FOUND、QA_FAILED、COMPOSE_FAILED；扩展 CONFIGURATION_REQUIRED、LLM_INVALID_OUTPUT、SUBMISSION_UNKNOWN、JOB_TIMEOUT、CONFLICT、INVALID_MEDIA。
