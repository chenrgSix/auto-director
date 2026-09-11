# API 与工作流契约

## 基础约定

Base `/api/v1`，JSON；ID 由服务端生成；UTC ISO 时间。验证失败 422，不存在 404，状态冲突 409，外部执行失败提供可读 `error.code/message/details`。仅本地单用户使用，默认绑定 loopback；跨域与公网 ComfyUI 需要显式配置。

### 分镜预览与确认（C14）

网页创建时携带 `preview_required: true`，然后 `POST /episodes/{id}/preview`（202）。队列完成导演规划、Visual Bible 与逐镜 ShotPrompts，停在 `AWAITING_REVIEW`，不创建渲染 Job 或生成 Asset；仅向 ComfyUI 读取设备及节点约束。`PREPARING_PROMPTS` 为活动状态，可取消；未完成时重试 preview 复用已有计划和提示词。

`PATCH /episodes/{id}/preview` 接收 `{expected_version, shots:[{id,title,duration,start_frame_prompt,end_frame_prompt,video_prompt}]}`。必须包含当前全部镜头及原顺序；只允许待确认时修改，检查标题/文本长度、总时长、单镜 workflow/显存约束及固定时长覆盖。失败原子回滚。只编辑创作提示词，不接受任意 `ai_parameters` 注入。高级固定 prompt、锁定参数、连续镜头复用首帧以及 I2V 不使用的尾帧不能编辑，详情 `shots[].preview_prompt_view` 返回实际输入与锁定原因。

`POST /episodes/{id}/approve` 接收 `{expected_version}`（202），原子记录 `preview_approved_at` 并入队渲染。保存和确认都使用最新版本；配置变动返回 `PREVIEW_STALE`（409），更新 preview 后再确认。未经确认调用 generate/compose/镜头 retry/timeline 返回 `PREVIEW_REQUIRED`（409）。确认后的失败重试沿用原 generate 行为，已有提示词不会重新请求模型。

Episode 的 `preview` 保存工作流版本、capability、时间线单镜 min/max、合法渲染上限和固定时长；Shot 保存 `preview_prompt_view` 与 `preview_edited_fields`。这些是现有 JSON 聚合的增量字段，旧记录缺省 `preview_required=false`，无需数据库表迁移或重算已有短片；旧客户端省略该字段仍可直接 generate。更换工作流清空预览及确认状态，保留历史作业和素材。

### 模型测试（C09）

`POST /models/test` 接收 `{ "kind": "director" | "vision" }`，只使用已保存的端点、模型和密钥，调用与生成相同的 JSON Provider。导演测试最小 JSON；视觉测试附带临时色块图片并校验识别结果，文件随后删除。模型诊断结果返回 200 / `{kind, model, success, elapsed_seconds, timeout_seconds, checks, error}`；未配置、鉴权/限流、请求错误和无效 JSON 记录在 error 中，非法请求字段仍返回 422。

测试总截止时间读取 `llm_timeout`，默认 600 秒；`GET/PATCH /settings` 支持读取与保存该字段，范围 1～3600 秒，越界或 null 返回 422。普通模型请求也使用此值作为 HTTP 连接/读写/连接池超时，单次调用开始时固定配置；保存后用于后续请求。旧配置文件缺少字段时继承环境 `AD_LLM_TIMEOUT` 或默认值，无需迁移。

浏览器断开时取消下游等待。测试不保存配置、不创建 Episode/Job/Asset，不返回密钥、请求正文或上游原始报错。测试通过仅表示小请求的 JSON/图像输入检查成功，不代表长任务或视觉质量验收。

### 更换已创建短片的工作流（C08）

`PATCH /episodes/{id}/workflows` 接收 `expected_version`（详情响应的 version）及显式 `image_workflow_id`、`reference_workflow_id`、`video_workflow_id`。参考项必须为 TEXT_TO_IMAGE，其余按媒体类型校验；全部要求本地绑定完整，不请求 AI/ComfyUI。版本过期、Episode 运行或仍有 QUEUED/RUNNING/UNKNOWN 作业返回 409，失败不修改数据。

实际更换时增加 `workflow_binding_revision`，旧绑定、计划/Bible、镜头/参考/成片与相关覆盖归档到 `workflow_binding_history[].previous_state`。当前生成状态重置为 DRAFT；启用预览的短片需再次 preview/approve，旧式短片可 generate 重新规划；保留 Idea/时长/比例/风格与历史作业/素材文件。仅保留仍选中工作流的覆盖；已换走媒体的旧式参数覆盖清空。相同 ID 提交不重置进度。渲染缓存按绑定版本隔离，旧数据缺失版本按 0 处理，无数据库迁移。

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
| `POST /workflows/{id}/default` | 本地绑定校验通过后设置媒体与 capability 默认项；绑定不完整返回 400 / WORKFLOW_INVALID。不要求依赖检查或试跑成功，不请求 ComfyUI/AI，不修改已有 Episode |
| `GET /jobs/{id}` | 作业状态、prompt_id、错误与资产 |
| `GET /jobs`, `POST /jobs/{id}/reconcile` | 作业列表与按自身 client_id 核对服务端队列/历史 |
| `POST /jobs/{id}/resume`, `POST /jobs/{id}/cancel` | 恢复已核对 prompt_id 的试跑、取消自身试跑 |
| `POST /jobs/{id}/resolve` | 操作员核对后填写至少 10 字结论，解除 UNKNOWN；不能自动调用 |
| `POST/GET /episodes`, `GET/DELETE /episodes/{id}` | 单集创建/列表/详情/可选资产清理 |
| `POST /episodes/{id}/generate`, `POST /episodes/{id}/cancel` | 入队/取消 |
| `POST/PATCH /episodes/{id}/preview`, `POST /episodes/{id}/approve` | 准备/编辑分镜预览、确认并入队渲染 |
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

参数覆盖用 `node_id.field` 键，例如 `{"sampler.steps": 20}`。单集参数只覆盖其选择的原 profile；低显存替代项使用自身 profile 参数。DynamicCombo 的现有扁平标量（如 `28.model.duration`）读取已选模型分支约束，选择器 enum 返回 key 字符串；依赖检查会更新已保存 schema，生成前再次按实际选择校验。不新增不存在的分支字段或递归编辑器。修改 profile 会清空旧校验结论，已提交 RenderJob 的 JSON 保持不变。

视频秒数输入按 type/min/max/step/enum 自动向上对齐，严格限制在 workflow 与适用显存预算内。Shot 的 `duration` 是时间线时长；视频作业 `input_values.timeline_duration` 记录原值，`input_values.duration` 是实际渲染秒数。例如 MiniMax H3 的 2.86 秒片段请求整数 4 秒，导出裁切回 2.86 秒；不把非法高级覆盖静默取整。无合法交集返回 `WORKFLOW_INVALID`，在生成参考图前拦截。

Episode 预算新增 `render_max_duration`；由绑定 duration 节点的 `api_node=true` 判断云端视频，云端不采用本地 3 秒显存上限，图片仍按本地资源策略。旧 Episode/Job JSON 缺少新增字段时自动兼容，无数据库迁移或重新导入要求。

## 工作流配置检查信息（C12）

WorkflowProfile 响应新增 `execution_info: {mode: "unknown" | "local" | "cloud", api_nodes: [{id, class_type, title}]}`。导入及旧记录默认 unknown；显式 `POST /workflows/{id}/validate` 从 ComfyUI `object_info` 更新后，GET 列表/详情和 PATCH 响应返回缓存结果。API 节点依据 `api_node=true`，不根据名称猜测，local 不代表真实渲染通过。更新值保留节点执行信息，但清空旧依赖结论。

保存继续使用现有 `bindings/outputs/parameter_values/parameter_rules`，没有新增第二套固定参数。未绑定完整的草稿仍可保存；设为默认需完整绑定，依赖检查与真实试跑独立。AUTOGROW 的必需容器认可其声明的实际槽位与最小数量，原来的缺失字段/越界链接错误仍有效。

## 导演规划时间契约（C13）

API 请求与存储结构保持兼容。新计划的内部 Agent context 增加 `min_shot_duration`、`recommended_shots`、`recommended_shot_duration`，与既有 `max_shot_duration/min_shots/max_shots` 同时渲染到 system 提示词。动态输出 schema 对每镜秒数和镜头数量使用相同 min/max；默认争取每镜至少 2.5 秒，但 1 秒总片长、较低工作流上限或合法固定时长仍可采用更低值。2.5 秒是规划偏好，不是 Episode/Workflow API 新增的硬下限。

例如总长 10 秒、有效上限 3 秒：每镜 2.5～3 秒、恰好 4 镜、建议每镜 2.5 秒。总长 3.01 秒、上限 3 秒：下限 1.5 秒、恰好 2 镜，最终分配 1.51 与 1.5 秒。原始合法比例保持，总和按百分之一秒精确归一化，不向后续镜头转嫁累计偏差。

已有 EpisodePlan 不重算；继续生成保留旧时间线，新建短片采用新约束。不更改 ComfyUI 真实渲染时长、显存策略或高级覆盖优先级。

## 调用示例

`EpisodeCreate.target_duration` 接受 **1～600 秒**，与 `max_shot_duration` 分开校验。后者仍为 1～30 秒，并受所选工作流能力和显存预算进一步约束。总时长超出范围返回 422；600 秒请求通过多个镜头完成。

`GET/PATCH /settings` 响应包含只读产品策略 `duration_policy: {"min":1,"max":600,"presets":[5,10,15,30,60,90]}`。首页据此生成预设和自定义输入边界；该字段不接受 PATCH 修改。

请求体与响应字段的完整类型见运行中的 `/docs` 和 `/openapi.json`。生成为异步操作：

```http
POST /api/v1/episodes
Content-Type: application/json

{"idea":"三只狮子进入侏罗纪","target_duration":5,"quality":"standard","aspect_ratio":"9:16"}
```

上述旧式请求创建成功返回 201 与 Episode（含 `id`），随后可直接 `POST /episodes/{id}/generate`（202）。网页默认额外携带 `preview_required:true`，按 C14 的 preview → approve 流程运行。轮询 `GET /episodes/{id}` 或读取 events 的 progress 事件；SSE 在待确认、完成、失败或取消时结束，网页继续采用轮询。完成后读取 `final_video_asset_id`，通过 `GET /assets/{asset_id}/file?download=true` 下载 MP4。

上传为 multipart 字段 `file`，返回 Asset；工作流试跑请求通过 `asset_bindings` 将角色关联到资产 ID，例如 `{"values":{"prompt":"A lion walking","duration":2,"fps":16},"asset_bindings":{"start_frame":"<asset_id>","end_frame":"<asset_id>"}}`。视频试跑要求相应首尾帧已上传。

时间线 PATCH 必须提供全部镜头 ID，且不重复、至少启用一个，例如 `{"shots":[{"id":"<shot_id>","enabled":true}]}`。新计划最多 240 镜，索引 0～239；旧版本超过 240 镜的时间线保留原 ID 集合，可重排/启用和导出，不能借此添加镜头。不直接编辑生成中的镜头；发生依赖失效后先重新生成，再 compose。

## 状态与错误

Episode 状态遵循原文 §36；增加 `QUEUED` 表示已入队，`PREPARING_PROMPTS` 表示准备文字提示词，`AWAITING_REVIEW` 表示暂停等待用户确认。Shot 状态遵循 §37，增加 `STALE` 表示依赖已变。RenderJob 区分 `QUEUED/RUNNING/COMPLETED/FAILED/CANCELLED/UNKNOWN`，UNKNOWN 不等于失败或可以重发。

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
