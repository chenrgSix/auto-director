# API 与工作流契约

## 基础约定

Base `/api/v1`，JSON；ID 由服务端生成；UTC ISO 时间。验证失败 422，不存在 404，状态冲突 409，外部执行失败提供可读 `error.code/message/details`。仅本地单用户使用，默认绑定 loopback；跨域与公网 ComfyUI 需要显式配置。

## 已实现 API

| 路径 | 行为 |
| --- | --- |
| `GET /health` | 本地进程健康 |
| `GET/PATCH /settings` | 在线配置读取/局部保存，成功立即生效；密钥仅写入、不回显 |
| `GET /comfyui/status`, `GET /comfyui/system`, `POST /comfyui/test` | 连接、设备、节点/模型检查 |
| `GET /workflows`, `POST /workflows/import` | 列表、API JSON 导入 |
| `GET/PATCH/DELETE /workflows/{id}` | profile、bindings、capabilities、删除保护 |
| `POST /workflows/{id}/validate` | 检查节点、枚举模型、字段、输入角色 |
| `POST /workflows/{id}/test-run` | 参数/资产 ID 试跑，返回异步 RenderJob |
| `POST /workflows/{id}/default` | 设置图像或视频默认项 |
| `GET /jobs/{id}` | 作业状态、prompt_id、错误与资产 |
| `GET /jobs`, `POST /jobs/{id}/reconcile` | 作业列表与按自身 client_id 核对服务端队列/历史 |
| `POST /jobs/{id}/resume`, `POST /jobs/{id}/cancel` | 恢复已核对 prompt_id 的试跑、取消自身试跑 |
| `POST /jobs/{id}/resolve` | 操作员核对后填写至少 10 字结论，解除 UNKNOWN；不能自动调用 |
| `POST/GET /episodes`, `GET/DELETE /episodes/{id}` | 单集创建/列表/详情/可选资产清理 |
| `POST /episodes/{id}/generate`, `POST /episodes/{id}/cancel` | 入队/取消 |
| `GET /episodes/{id}/progress`, `GET /episodes/{id}/events` | 快照/SSE 进度 |
| `GET /episodes/{id}/shots`, `PATCH /episodes/{id}/timeline` | 镜头与排序/启用状态 |
| `GET /episodes/{id}/qa` | 不可变质检历史，包含阶段、镜头、资产与原始得分；重试保留旧记录 |
| `POST /shots/{id}/retry[-keyframes\|-video]` | 定向重试 |
| `POST /episodes/{id}/compose` | 根据当前启用镜头重新导出 |
| `POST /episodes/{id}/assets`, `GET /assets/{id}/file` | 有界媒体上传/本地预览下载 |
| `POST/GET /assets` | 工作流试跑上传与资产列表；GET 支持 episode_id 过滤 |

## WorkflowProfile

统一 capability 命名为 `supports_start_frame/end_frame/video_reference/multi_reference` 与 `max_duration`。图像至少 `prompt` + `image` output；视频至少 `prompt/start_frame` + `video` output，声明支持尾帧时必须绑定 `end_frame`。缺尾帧只允许显式的 I2V capability 降级。

Binding 为 `{node_id, input, transform}`；`transform` 默认 `identity`，可指定 `duration_to_frames`（根据 fps 对齐模型帧数）。输出绑定到 node，不修改输入。角色从 `_meta.title` 中的 `(Input:role)` / `(Output:role)` 提取；`width_height` 展开。不明确的字段要求用户编辑绑定，不猜测固定节点位置。参数 schema 保留字段类型、默认值、min/max、step、enum 及绑定角色。

参数覆盖用 `node_id.field` 键，例如 `{"sampler.steps": 20}`。单集参数只覆盖其选择的原 profile；低显存替代项使用自身 profile 参数。嵌套 DynamicCombo 原样保留，表单当前只编辑标量。修改 profile 会清空旧校验结论，已创建 RenderJob 的快照保持不变。

## 调用示例

`EpisodeCreate.target_duration` 接受 **1～600 秒**，与 `max_shot_duration` 分开校验。后者仍为 1～30 秒，并受所选工作流能力和显存预算进一步约束。总时长超出范围返回 422；600 秒请求通过多个镜头完成。

`GET/PATCH /settings` 响应包含只读产品策略 `duration_policy: {"min":1,"max":600,"presets":[5,10,15,30,60,90]}`。首页据此生成预设和自定义输入边界；该字段不接受 PATCH 修改。

请求体与响应字段的完整类型见运行中的 `/docs` 和 `/openapi.json`。生成为异步操作：

```http
POST /api/v1/episodes
Content-Type: application/json

{"idea":"三只狮子进入侏罗纪","target_duration":5,"quality":"standard","aspect_ratio":"9:16"}
```

创建成功返回 201 与 Episode（含 `id`）。随后 `POST /episodes/{id}/generate` 返回 202，轮询 `GET /episodes/{id}` 或读取 `GET /episodes/{id}/events` 的 `progress` 事件；当前 Web UI 采用轮询。完成后读取 `final_video_asset_id`，通过 `GET /assets/{asset_id}/file?download=true` 下载 MP4。

上传为 multipart 字段 `file`，返回 Asset；工作流试跑请求通过 `asset_bindings` 将角色关联到资产 ID，例如 `{"values":{"prompt":"A lion walking","duration":2,"fps":16},"asset_bindings":{"start_frame":"<asset_id>","end_frame":"<asset_id>"}}`。视频试跑要求相应首尾帧已上传。

时间线 PATCH 必须提供全部镜头 ID，且不重复、至少启用一个，例如 `{"shots":[{"id":"<shot_id>","enabled":true}]}`。新计划最多 240 镜，索引 0～239；旧版本超过 240 镜的时间线保留原 ID 集合，可重排/启用和导出，不能借此添加镜头。不直接编辑生成中的镜头；发生依赖失效后先重新生成，再 compose。

## 状态与错误

Episode 状态遵循原文 §36；增加 `QUEUED` 表示已入队。Shot 状态遵循 §37，增加 `STALE` 表示依赖已变。RenderJob 区分 `QUEUED/RUNNING/COMPLETED/FAILED/CANCELLED/UNKNOWN`，UNKNOWN 不等于失败或可以重发。

错误码保留 §52：COMFYUI_OFFLINE、WORKFLOW_INVALID、MISSING_NODE、MISSING_MODEL、OUT_OF_MEMORY、PROMPT_REJECTED、EXECUTION_ERROR、OUTPUT_NOT_FOUND、QA_FAILED、COMPOSE_FAILED；扩展 CONFIGURATION_REQUIRED、LLM_INVALID_OUTPUT、SUBMISSION_UNKNOWN、JOB_TIMEOUT、CONFLICT、INVALID_MEDIA。

## 在线配置契约

`PATCH /settings` 支持 `comfyui_url`、`allow_public_comfyui`、`llm_base_url`、`llm_model`、`vlm_model`、`llm_api_key`、`clear_llm_api_key`、`render_timeout`、`request_timeout`、`max_asset_mb` 和 `poll_interval`。省略字段保持原值；模型名空字符串可清除；除密钥外不接受 null。

`llm_api_key` 空字符串/null/省略均保留原密钥；非空替换，`clear_llm_api_key: true` 明确清除，不能同时替换与清除。更改模型端点而已有密钥时，必须同时重新提供密钥或明确清除，否则返回 409 `CREDENTIAL_REQUIRED`。GET/PATCH 只返回 `llm_api_key_configured` 布尔值。

保存成功 200，无需重启；有运行中、未退出或 UNKNOWN 作业返回 409 `CONFLICT`。输入无效 422；原子写入失败 500 `CONFIGURATION_SAVE_FAILED`，旧配置继续生效。数据目录/网页来源不是在线可修改字段。


## Workflow Capability 与参数覆盖（C03）

`WorkflowImport` 使用 `media_type: image|video` 与 `capability: TEXT_TO_IMAGE|IMAGE_TO_IMAGE|FIRST_LAST_TO_VIDEO|IMAGE_TO_VIDEO`；只传 capability 时可推导 media_type。旧 `type` 仍兼容并在响应中作为别名保留；显式矛盾返回 422。`capabilities` 继续保存单镜 max_duration、视频参考、多图参考和低显存替代项。首尾帧能力以 capability 为准。

`parameters[]` 增加 `owner`（ai/director/asset_resolver/system/workflow/user）、`editable`、`override_policy: advanced|never`。`PATCH /workflows/{id}` 接受 capability 与 `parameter_rules: {"node.field":{"owner":"ai","editable":true,"override_policy":"advanced"}}`；有语义角色或素材语义的 owner 由系统确定，自定义字段可在 ai/workflow/user 间选择。重新绑定、节点校验后重新计算元数据。profile 的 `parameter_values` 属于工作流默认值。

`EpisodeCreate` 新增 `advanced_mode`、`reference_workflow_id`、`workflow_overrides: {"workflow_id":{"node.field":value}}`。默认模式无需参数配置；显式 false 携带覆盖值返回 422。旧 image_parameters/video_parameters 和旧显式尺寸/FPS输入可推导高级模式，两个旧参数映射仍支持，新映射优先。只允许覆盖所选图像、视频、初始参考图工作流中的可编辑参数。

素材覆盖值必须是上传所得 asset ID；不得填模板路径。后端验证媒体类型及 Episode 归属，通用上传仅在显式指定后授权给该 Episode。视频首尾帧覆盖同步到 Shot，用同一真实素材进行 QA 和后续绑定。缺少必须素材返回 `ASSET_REQUIRED`，不会提交带占位路径的图。

时长参数保持原始节点单位：若 binding 为 duration_to_frames，则按 `(帧数 - frame_offset) / FPS` 得到每镜秒数。当前高级时长覆盖为每镜固定时长，须满足能力上限、整除总时长且总镜数 ≤240；例如 16 FPS、偏移 1、49 帧对应每镜 3 秒。否则规划返回 `OVERRIDE_INVALID`。无法在 240 镜内满足总时长返回 `LIMIT_EXCEEDED`，不会提高单镜上限。

`GET /settings` 同时提供只读 `limits` 和 `default_capabilities`。`GET /jobs` 返回 `requested_parameter_values` 与 `parameter_sources`，记录实际参数来自 user、对应 owner 或 OOM 强制降级；提交图与内部 profile_snapshot 仍不直接公开。

Agent 的 Bible/Shot 输出增加 `ai_parameters: {"workflow_id":{"node.field":value}}`，只能包含当前上下文内 owner=ai 的参数。严格校验失败返回 `LLM_INVALID_OUTPUT`，高级用户 override 不能掩盖非法 AI 输出。Episode 读取保留该映射；旧记录缺省为空。I2V 的 `end_frame_asset_id` 可为空，其 `actual_end_frame_asset_id` 仍是实际视频提取的尾帧。

C04 路由不增加必填请求字段：显式 image/video/reference_workflow_id 优先；省略时通过 `default_capabilities` 解析默认项，旧媒体默认项保留能力选择及同能力回退语义。创建响应中保存最终 ID，生成时不随默认项变化重新选型。reference_workflow_id 始终要求 TEXT_TO_IMAGE。

## C06 快速导入与 AI 识别（替代 C05 识别接口）

`POST /workflows/analyze` 接受 `{workflow, capability?}`，显式调用已配置的导演模型，只读返回 `{capability, bindings, outputs, reasons, notes}`。不拉 ComfyUI 节点清单、不修改 profile；绑定只能指向实际可写字段，输出只能指向已有节点。非法类型、链接目标、重复字段、未知角色、能力冲突及额外字段被拒绝。省略 capability 时允许 AI 建议用途，提供时必须匹配。

`POST /workflows/import` 必须提供 capability、media_type 或旧 type 之一；否则返回 422。导入只做本地结构校验并保存；不隐式调用 AI、ComfyUI 或依赖检查。显式 bindings/outputs 和既有用途标签保持兼容。Workflow 仍返回 binding_issues；旧 binding_assistance 形状保留，但候选为空，不再规则推断。

旧 `POST /workflows/{id}/auto-bind` 返回 410 / WORKFLOW_RULES_REMOVED，提示改用 AI 识别或手动绑定。AI 未配置返回 409 / CONFIGURATION_REQUIRED；超过 45 秒返回 504 / WORKFLOW_AI_TIMEOUT；超过上下文限制返回 422 / WORKFLOW_TOO_LARGE_FOR_AI；非法模型输出沿用一次纠正机会，仍非法返回 LLM_INVALID_OUTPUT。没有规则回退，取消识别不写数据。

AI 建议经用户确认后，通过普通 import/PATCH 保存；依赖检查仍为 `/workflows/{id}/validate`，试跑前继续验证。绑定完成、依赖满足和试跑成功是独立状态，导入返回不代表工作流可运行。
