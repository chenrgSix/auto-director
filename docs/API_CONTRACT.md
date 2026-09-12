# API 与工作流契约

## C46 剧本 AI 审查

新增活动阶段 `REVIEWING_SCRIPT`。Episode 可含 `script_review`：`status=pending/passed/corrected/needs_attention`、摘要、逐镜修正/遗留问题、原稿、审后剧本摘要散列与审查时间。旧记录字段缺省，不自动改写。编辑预览后的审查记录仍指向原审稿，`edited_after_review` 表示之后有过用户改动。

`POST /episodes/{id}/approve` 额外接受 `accept_script_review: boolean=false`。对 `needs_attention`，须显式 true，否则返回 `SCRIPT_REVIEW_REQUIRED`，不排队渲染；已有版本/工作流校验保持。未要求预览的客户端遇到无法自动修正的问题，也会得到 AWAITING_REVIEW 并保留纯文字预览，必须批准后继续。

## C45 视频提示词内的动作节奏

Episode/预览编辑 API 字段保持兼容；`prompts.video_prompt` 可含末尾 `Shot timing (seconds):` 时间块，格式为每行 `起点-终点s: English action`。新生成长镜由受约束 Agent 生成并编译；旧字符串继续有效，无数据库迁移。

PATCH 预览：若仅改时长且未改视频提示词，服务端按比例更新原时间块。显式修改视频提示词时以提交内容为准；保留时间块则须从 0 连续覆盖新时长，否则返回 `PREVIEW_INVALID`，不部分保存。高级固定 prompt 不自动调整。用户可改写为普通文字；无时间块不推断时间。优化接口对有时间块的来源禁止模型无声移除时间块，非法模型输出走既有纠正/拒绝机制。

## C44 提示词定向优化

- `POST /episodes/{id}/shots/{shot_id}/prompt-optimization`：`{expected_version, feedback?}`。短片空闲且该启用镜头有提示词和视频时生成优化预览；不生成图片/视频、不更改 Episode。feedback 最长 1000 字，只作为希望检查的问题。响应含方案 ID、源版本、模型判断/置信度/解释、逐字段原文/新文/原因、需重做关键帧、受影响镜头列表。模型失败或请求取消不失效现有素材。
- `POST /episodes/{id}/prompt-optimizations/{proposal_id}/apply`：`{expected_version}`，202。仅确认服务端保存的方案，禁止客户端注入补丁；保存新提示词、归档旧版并沿原队列重跑/合成。过期版本、工作流变化、非 revise/低置信度、禁用镜头、未结算作业均拒绝；重复提交不会重复入队。
- `GET /episodes/{id}/shots/{shot_id}/prompt-optimization`：返回最新方案或 null，支持刷新恢复预览；已应用/过期方案不可再次确认。删除 Episode 同步删除其方案记录。

仅 start_frame_prompt/end_frame_prompt/video_prompt 可修改，字段由 capability、连续性、固定素材和覆盖规则进一步限制。方案不改变时长、计划、AI 参数或其他设置；旧数据不需结构迁移。

## C42 质检策略

`EpisodeCreate.qa_policy` 接受 `advisory|strict`，API 与旧数据缺省 strict；页面显式提交默认 advisory。`PATCH /episodes/{id}/qa-policy` 接受 `{expected_version, qa_policy}`，忙碌、版本冲突或 QUEUED/RUNNING/UNKNOWN 作业返回 409。保存不入队、不重写剧本或删除素材；归档旧策略/失败执行状态，仅清理已结算语义失败的重试状态，技术故障和提交恢复身份保留。

advisory 跳过候选比较与视频前语义质检，视频后检查一次。`Shot.status=PASSED` 仍表示可参与合成的执行结果；`needs_review=true` 与 `review_notes[{stage,code,message,asset_ids}]` 单独表达画面待复核。QA 原始分数保留，`qa[].disposition=warning` 表示继续而非质量合格。模型服务错误仅在视觉检查边界转为复核提示；媒体、时长和执行错误仍失败。缓存绑定实际视频、前镜尾帧、目标、质量策略和模型端点；更换素材或主动重跑后重新检查。无表结构迁移，字段按 JSON 可选项兼容。

## C41 质检与编辑输入一致

模型请求不再含历史判决或内部运行数据；真实图片与当前审核视觉目标为评价依据。模型 schema 用 artifact_severity 表示瑕疵严重度（0 无瑕疵、1 严重损坏），兼容旧 artifact_score 输入，内部字段及 API/历史输出继续 artifact_score；不反转数值或改变阈值。现有 qa_retry.rejected_assets 在图生图局部纠错时用于 reference_image，最终仍受用户覆盖、资产归属和类型校验。无新增接口、表结构或自动重写剧本行为。

## C40 结构化关键帧质检

QAResult 可选 failed_frames（start_frame/end_frame，最多两项且不重复）与 frame_corrections（仅失败帧键，非空建议每项最多 1500 字）。缺省为空时兼容旧分数分流；I2V 不接受尾帧反馈，视频 QA 沿用原分流。Episode 镜头 JSON 可包含内部 qa_retry/qa_frame_corrections 恢复状态，无表结构迁移。继续生成不会重写故事或删除历史文件；QA 耗尽后的新尝试使用新版本，UNKNOWN 仍核对原作业。高级 prompt 覆盖不被自动纠错或 negative 拼接改写。

## C39 生成前参数检查

生成前错误提供工作流身份及节点/字段问题，在参考图/首尾帧生成前检查可确定的配置，在资产上传前复核最终参数与全部资产。预检不提交 ComfyUI，不改变故事或素材绑定；已提交的 UNKNOWN 作业继续按原图恢复。合法高级覆盖可替代失效 Workflow 默认值，AI 非法值仍拒绝。参数的 step/downstream_constraints 同步给前端与 Agent，旧记录在实时校验时补齐，无新增迁移。

## C38 直连数值参数的约束

Workflow 参数可选 downstream_constraints 列表记录 PrimitiveInt/PrimitiveFloat.value 输出 0 的直连消费者字段 schema；实时依赖检查和生成前刷新补齐旧记录，无需重导入或表迁移。源参数与各消费者的 type/min/max/step/enum 必须同时满足；非法值返回 WORKFLOW_INVALID 并指出源参数与消费者字段。自动尺寸向下适配，用户显式覆盖仍严格拒绝，失败续跑保持原计划及素材。

## C37 已有短片的质量模式

`PATCH /episodes/{id}/quality` 接受 `expected_version` 与 `quality`（fast/standard/high），返回更新后的 Episode。仅空闲且本片没有 QUEUED/RUNNING/UNKNOWN 作业时允许，取消后仍在退出的 busy 任务也拒绝；版本冲突返回 409，非法模式或额外字段返回 422。相同模式无副作用。

模式实际改变时，`max_retries` 恢复 null，按模式默认 1/2/3 次，已有 budget 的 candidates 更新为 1/1/2。现有分辨率、FPS、时长预算和 qa_enabled 保留，后续质检门槛从 quality 读取。API 不入队、不调用模型或 ComfyUI，不撤销分镜确认，不重写计划/提示词或移除素材绑定。

`generation_settings_history` 保留旧质量、重试配置、预算与执行游标；未完成且已有作业/游标的镜头递增 retry_version，清理旧 render_cursor/render_recovery，避免新模式重放旧预算或耗尽的尝试。已通过镜头、所有 Job 快照与资产文件保持。字段沿用 JSON 记录，旧数据缺少历史时按空列表处理，不需要表结构迁移。

## C35 提示词与素材一起重跑

POST /episodes/{id}/rerun 的 scope 增加 "prompts"：先归档旧成片和受影响镜头（含完整 prompts），清除所选镜头的提示词及关键帧/视频绑定，再入队调用 Shot Agent，重写画面/视频/负面提示词、旁白脚本和 AI-owned 参数。计划、顺序、时长、Bible、参考图及旧文件保留；不重新请求 Director/Bible。原 video/keyframes 语义不变。

shot_ids 中明确选中的镜头（省略则全部启用镜头）均重写提示词，包括连续镜头。未被选择的连续依赖仍只重做关键帧/视频，独立切镜和禁用镜头保持。高级固定覆盖继续优先；allow_static_end_frame 省略时沿用原设定，显式值优先于重新生成的 AI 输出。陈旧预览提示词视图/提示词编辑标记清理，旧内容保留在 rerun_history。

生成提示词时状态为 PREPARING_PROMPTS；失败保留历史，可通过 /generate 继续。已保存的新提示词不会因后续渲染失败而再次调用 Agent。原版本、任务占用、UNKNOWN 与审批前置校验全部沿用。没有表结构迁移或启动时数据改写。

## C33 首尾帧变化意图与技术门禁

- ShotPrompts 新增严格布尔 allow_static_end_frame，默认 false；仅有意定格可为 true，静止摄像机不等于静止主体。
- PATCH /episodes/{id}/preview 的 shots 项可带 allow_static_end_frame；省略/null 保持原值，继续使用原 expected_version 和状态校验。
- POST /episodes/{id}/rerun 可带同名字段；仅覆盖明确选择的镜头（未指定 shot_ids 则全部启用镜头），连续依赖镜头保持各自设定。原设定随 rerun_history 保存，省略保持兼容。
- Shot.keyframe_comparison 可包含 near_duplicate、correlation、normalized_mae、start_frame_asset_id、end_frame_asset_id。重跑失效时清理派生比较结果。
- FIRST_LAST_TO_VIDEO 检出近重复且重试耗尽时，Episode.error.code=KEYFRAMES_TOO_SIMILAR，details 含 shot_id 与数值指标。不会提交新视频；原资产可下载。用户固定尾帧直接报错，避免反复生成被固定覆盖的素材；I2V 不执行该门禁。

C31：`DELETE /workflows/{id}` 成功后，内置工作流与用户工作流均保持删除状态，服务重启不重新导入。首次初始化仍提供两份内置模板；既有默认选择及 Episode 引用保护保持，409 明确显示“未删除”，引用冲突的 details.episodes 包含 ID/title。旧 settings 的默认能力映射只迁移现存工作流，不补建缺失记录；无 API 请求格式或表结构变更。

C30：Shot 可选 render_cursor 保存当前尝试号、retry_version、起始重试余量、基础参数和重试范围；Episode 可选 render_recovery 保存恢复游标及 step_key→Job ID 映射。均为现有 JSON 聚合的扩展，无表迁移。继续生成按原作业快照恢复 UNKNOWN，包括 OOM 降分辨率、分段与中间帧；已确认的 OOM 决策重放但不重新提交，已完成步骤复用。恢复上下文跨重启和本地后处理失败保留，镜头通过后清理；显式重跑/换绑/时间线修改清除旧恢复上下文。无 prompt_id、跨短片未结算或步骤身份不符仍拒绝，不能通过恢复绕过未知提交保护。

C29：Workflow 增加 configuration_version，标识最后一次可执行配置更改；原 version 继续记录全部更新。预览 workflow_versions 兼容配置版本到当前记录版本区间，重复检测、无变化保存和名称更新不阻止确认；真正的参数/绑定/能力变更仍返回 PREVIEW_STALE。旧 Workflow 首次检查使用原 version 作为配置基线，无表迁移。

C28：新增 VIDEO_TOO_SHORT，details 包含 actual/required/fps。视频技术校验始终启用，与视觉 QA 开关独立；失败产物保留在 Job.output_asset_ids，自动重试只重做视频并消耗共享预算。继续生成时自动归档并修复旧版错误通过的短视频及连续依赖，其他镜头与已有文件保持；重新导出也拒绝过短片段。

C27：帧数型 duration 的节点 min/max/step/enum 参与合法渲染秒数与导演上限计算，采用最终生成 FPS 和绑定倍数/偏移。无交集在素材创建前返回 WORKFLOW_INVALID；已准备故事保持不变。preview 增加同版本的 video_parameter_snapshot，读取/保存/确认沿用规划时实时约束，实际渲染仍复检远端元数据。

C26：详情 GET 的 warnings 不再显示当前已绑定 reference_image 的旧「无输入」提示；只读派生，不增加版本。更换工作流和继续生成时持久化相同规则，先前警告保留在 workflow_binding_history，其他警告和所有素材保持。

C25：系统自动宽高必须同时满足视频/图像工作流尺寸约束，向下对齐且不提高预算；不可满足时在生成前返回 WORKFLOW_INVALID，包含具体尺寸和工作流约束。Episode 顶层 width/height、高级覆盖与手动试跑维持显式值并严格校验。budget 可选 explicit_dimensions 记录用户顶层尺寸，旧记录恢复时从 Episode 补齐；无表结构迁移。适配不重新规划、不删除首尾帧或旧视频。

C24：`POST /episodes/{id}/cancel` 仍将状态标为 CANCELLED，取消本地模型及只读预检等待，后台清理完成前 DELETE/重入继续返回 409；已排队但未执行的短片移出本地队列。DELETE 的冲突消息区分后台停止中、生成中、未结算 ComfyUI 作业，并附作业 ID/状态便于核对。不能通过取消或删除绕过 UNKNOWN 防重复执行保护，删除资产仍只由 delete_assets 显式控制。

C23：网页「整片重新生成」沿用 `POST /episodes/{id}/rerun`，省略 shot_ids，默认 scope=video，可选 keyframes；确认后才提交。旧成片 ID 与时长在入队前归档，即使新一轮生成失败或取消，先前全部 rerun_history 与资产文件仍保留，预览/下载复用资产接口。没有新增接口或数据库迁移。

C22：`Binding.frame_offset` 扩展为 0～31，`frame_multiple` 保持 1～32，支持 17n+5；可选 `frame_fps`（1～120）只用于 `duration_to_frames`。有固定生成时钟时，规划/预览/试跑/Resolver/patch 统一使用该 FPS，显式 FPS 参数覆盖与之冲突时拒绝。旧绑定未指定时保持原 FPS 策略和逆变换兼容；指定固定时钟后的显式帧数时长按帧数/FPS 计算。自动分镜按秒数×FPS 向上对齐帧数，不改变时间线秒数；生成/保存节点 FPS 必须与模型时钟一致。

C21 重跑：`POST /episodes/{id}/rerun` 接受 `{expected_version, scope:"video"|"keyframes", shot_ids?:string[], new_seed?:boolean}`，返回 202。省略 shot_ids 表示全部启用镜头；空列表、重复、其他短片或禁用镜头拒绝。`video` 沿用已有关键帧，`keyframes` 重做关键帧及视频；后续连续性依赖一并失效，跳过禁用镜头，到独立切镜停止。保存 `rerun_history`（旧成片、镜头/提示词/QA/资产绑定与影响范围），不删资产或改写计划、Bible、参考图。默认递增镜头 seed_offset；new_seed=false 沿用原种子，明确高级 seed 覆盖仍优先。新结果完成后重新导出整片。

重跑版本不匹配/运行中返回 409 CONFLICT；存在任何 UNKNOWN 作业返回 409 UNRESOLVED_JOB；未确认分镜返回 409 PREVIEW_REQUIRED。所有拒绝均不修改原成果、不排队。旧 `/shots/{id}/retry[-video|-keyframes]` 委托同一实现，亦保留历史和执行未知状态保护。重跑与恢复原任务是独立操作，未知任务不得借重跑解除防重复提交保护。

C21 连接恢复：已取得 prompt_id 的作业保持 RUNNING，在 `render_timeout` 内对历史查询和产物下载重连，`progress.connection=reconnecting|connected`、`reconnect_attempt`、`retry_in` 表示连接状态，保留最后节点/采样进度。WebSocket 中断会重新连接，HTTP 轮询持续有效。超时仍为 UNKNOWN，既有 `/episodes/{id}/generate` 复用相同输入的原作业；未取得 prompt_id 必须先 reconcile。断网不取消远端，不自动重发 POST /prompt。取消请求无法确认时仍保留 UNKNOWN。

C20：每镜时间线按 workflow.max_duration、用户显式 max_shot_duration 和模型实际合法时长校验；显存/质量模式不再额外施加 3/4 秒硬上限。旧预算在读取待确认预览、保存/批准和继续生成时刷新时长字段，保留故事、图片尺寸和素材。OOM 降级仍受工作流/模型与共享重试预算约束，不改变目标时间线。

C19：`PATCH /episodes/{id}/workflows` 始终保留计划、镜头、视觉设定、已保存提示词和已有素材绑定。未渲染故事继续按 C18 校验并等待确认；已开始生成的故事保留原批准，不返回文字预览，不自动生成。已有结果沿用，缺失结果及显式重试使用新工作流；继续生成时重新读取节点与显存预算，不兼容时保留故事和素材并报错。换走的专属覆盖和 AI 映射不迁移到新 ID，原值、作业 JSON 与素材文件保留在历史中。

`POST /episodes/{id}/workflows/restore` 接受 `{expected_version, history_revision}`，仅恢复空短片中的历史故事、进度及素材，不改变当前绑定。已有计划/镜头/素材、运行中/未结算作业、版本冲突、缺失或越权资产均拒绝且不写入。详情中 `recoverable_workflow_revision` 提供可恢复的最近历史版本；恢复记录保存来源，不启动 LLM 或 ComfyUI。

## 基础约定

Base `/api/v1`，JSON；ID 由服务端生成；UTC ISO 时间。验证失败 422，不存在 404，状态冲突 409，外部执行失败提供可读 `error.code/message/details`。仅本地单用户使用，默认绑定 loopback；跨域与公网 ComfyUI 需要显式配置。

### 分镜预览与确认（C14）

C16 替代 C15 的空值回退：生成阶段的 `prompt` 角色统一使用该阶段的分镜/参考画面提示词，旧 AI 映射中的同角色值（包括非空旁白）不能覆盖它。先验证旧映射的身份和类型再排除重复项，其他动态字段和高级用户覆盖保持原值。新 Agent schema 不允许重复填写 `prompt` 角色，`ShotPrompts.narration_text` 单独保存旁白脚本（旧记录缺省为空；不表示已生成配音）。详情 GET 只读刷新派生视图，不改变版本；保存预览时将重复项归档至 `shots[].legacy_ai_prompt_parameters`，保留原始内容，刷新持久化视图，沿用版本与状态保护。

真正缺少可编辑提示词时，保存/确认返回 `PREVIEW_INVALID`，message 包含镜头编号、标题和字段，details 包含 `shot_id/field`。未使用或锁定的提示词允许保留空值，不能通过编辑接口改变锁定值。网页校验区分别显示总时长差额、每镜时长范围和具体字段错误，支持定位镜头。

网页创建时携带 `preview_required: true`，然后 `POST /episodes/{id}/preview`（202）。队列完成导演规划、Visual Bible 与逐镜 ShotPrompts，停在 `AWAITING_REVIEW`，不创建渲染 Job 或生成 Asset；仅向 ComfyUI 读取设备及节点约束。`PREPARING_PROMPTS` 为活动状态，可取消；未完成时重试 preview 复用已有计划和提示词。

`PATCH /episodes/{id}/preview` 接收 `{expected_version, shots:[{id,title,duration,start_frame_prompt,end_frame_prompt,video_prompt}]}`。必须包含当前全部镜头及原顺序；只允许待确认时修改，检查标题/文本长度、总时长、单镜 workflow/显存约束及固定时长覆盖。失败原子回滚。只编辑创作提示词，不接受任意 `ai_parameters` 注入。高级固定 prompt、锁定参数、连续镜头复用首帧以及 I2V 不使用的尾帧不能编辑，详情 `shots[].preview_prompt_view` 返回实际输入与锁定原因。

`POST /episodes/{id}/approve` 接收 `{expected_version}`（202），原子记录 `preview_approved_at` 并入队渲染。保存和确认都使用最新版本；配置变动返回 `PREVIEW_STALE`（409），更新 preview 后再确认。未经确认调用 generate/compose/镜头 retry/timeline 返回 `PREVIEW_REQUIRED`（409）。确认后的失败重试沿用原 generate 行为，已有提示词不会重新请求模型。

Episode 的 `preview` 保存工作流版本、capability、时间线单镜 min/max、合法渲染上限、固定时长及 `remote_video` 执行方式。后者只在相同 workflow 版本下复用，旧记录按已验证的 duration API 节点身份兼容，避免保存时误套本地 3 秒上限。Shot 保存 `preview_prompt_view` 与 `preview_edited_fields`。这些是现有 JSON 聚合的增量字段，旧记录缺省 `preview_required=false`，无需数据库表迁移或重算已有短片；旧客户端省略该字段仍可直接 generate。更换工作流时，未渲染故事更新预览并清除确认；已有渲染进度按 C19 保留批准与素材，不重新规划。

### 模型测试（C09）

`POST /models/test` 接收 `{ "kind": "director" | "vision" }`，只使用已保存的端点、模型和密钥，调用与生成相同的 JSON Provider。导演测试最小 JSON；视觉测试附带临时色块图片并校验识别结果，文件随后删除。模型诊断结果返回 200 / `{kind, model, success, elapsed_seconds, timeout_seconds, checks, error}`；未配置、鉴权/限流、请求错误和无效 JSON 记录在 error 中，非法请求字段仍返回 422。

测试总截止时间读取 `llm_timeout`，默认 600 秒；`GET/PATCH /settings` 支持读取与保存该字段，范围 1～3600 秒，越界或 null 返回 422。普通模型请求也使用此值作为 HTTP 连接/读写/连接池超时，单次调用开始时固定配置；保存后用于后续请求。旧配置文件缺少字段时继承环境 `AD_LLM_TIMEOUT` 或默认值，无需迁移。

浏览器断开时取消下游等待。测试不保存配置、不创建 Episode/Job/Asset，不返回密钥、请求正文或上游原始报错。测试通过仅表示小请求的 JSON/图像输入检查成功，不代表长任务或视觉质量验收。

### 更换已创建短片的工作流（C08）

`PATCH /episodes/{id}/workflows` 接收 `expected_version`（详情响应的 version）及显式 `image_workflow_id`、`reference_workflow_id`、`video_workflow_id`。参考项必须为 TEXT_TO_IMAGE，其余按媒体类型校验；全部要求本地绑定完整，不请求 AI/ComfyUI。版本过期、Episode 运行或仍有 QUEUED/RUNNING/UNKNOWN 作业返回 409，失败不修改数据。

实际更换时增加 `workflow_binding_revision`，旧绑定、计划/Bible、镜头/参考/成片与相关覆盖归档到 `workflow_binding_history[].previous_state`。C18 起，已有故事但当前绑定无渲染作业/素材时保留创作内容：完整分镜返回 AWAITING_REVIEW，部分准备结果保留并允许继续准备；需再次 approve，但不重新调用已完成的导演/Bible/Shot。C19 起已有渲染进度也保留故事及全部素材绑定：未完成短片返回 DRAFT 供继续生成，已完成短片保持 COMPLETED，批准记录不清除。设置 refresh_workflow_budget，继续生成时使用实时显存/节点重新计算预算；不兼容只报错，不清空故事或素材。仅保留仍选中工作流的覆盖，已换走媒体的旧式参数覆盖清空；AI 映射按阶段绑定筛选，不复制到新 ID。相同 ID 提交不重置进度。渲染缓存按绑定版本隔离，旧版本缺省 0，无数据库迁移。未渲染预览的镜头时长不兼容以 PREVIEW_INVALID 返回具体镜头；已有渲染进度在继续生成前检查剩余镜头，失败仍保留原内容。

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
| `POST /episodes/{id}/workflows/restore` | 恢复空短片的最近历史故事/素材，保留当前绑定，不自动生成 |
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

Episode 预算新增 `render_max_duration`；时长由显式工作流和用户配置决定，模型类型/min/max/step/enum 继续严格校验；`api_node=true` 用于执行方式展示，C20 起本地与远端视频均不按显存猜测时长上限，图片仍按本地资源策略。旧 Episode/Job JSON 缺少新增字段时自动兼容，无数据库迁移或重新导入要求。

## 工作流配置检查信息（C12）

WorkflowProfile 响应新增 `execution_info: {mode: "unknown" | "local" | "cloud", api_nodes: [{id, class_type, title}]}`。导入及旧记录默认 unknown；显式 `POST /workflows/{id}/validate` 从 ComfyUI `object_info` 更新后，GET 列表/详情和 PATCH 响应返回缓存结果。API 节点依据 `api_node=true`，不根据名称猜测，local 不代表真实渲染通过。更新值保留节点执行信息，但清空旧依赖结论。

保存继续使用现有 `bindings/outputs/parameter_values/parameter_rules`，没有新增第二套固定参数。未绑定完整的草稿仍可保存；设为默认需完整绑定，依赖检查与真实试跑独立。AUTOGROW 的必需容器认可其声明的实际槽位与最小数量，原来的缺失字段/越界链接错误仍有效。

## 导演规划时间契约（C13 / C36）

API 请求与存储结构保持兼容。内部 Agent context 的 `min_shot_duration/max_shot_duration/min_shots/max_shots` 与 system 提示词、动态输出 schema 使用相同的可行范围。C36 移除 `recommended_shots/recommended_shot_duration`，由模型按剧情决定数量与节奏；最少镜头数只表示容量下限，最长时长只表示上限。默认硬下限为 1 秒，`preferred_min_shot_duration`（最多 2.5 秒）仅供完整动作的软节奏提示，允许有剧情依据的短镜；用户明确固定时长时上下限一致。

例如 30 秒 / 5 秒上限可以规划成 8 镜 `[3,4,5,3,4,5,3,3]`，也可按另一剧本选择其他合法数量，不再推荐 6×5。每批最多输出 12 镜，长片给叙事拆镜预留空间；`available_shots` 表示扣除已用和后续最低必要额度后本批可用数量，动态 schema 同时受全片 240 镜约束。原始合法比例保持，总和按百分之一秒精确归一化。

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

Agent 的 Bible/Shot 输出使用 `ai_parameters: {"workflow_id":{"node.field":value}}`，只能包含当前上下文内 owner=ai 且非自动阶段 prompt 的参数。严格校验失败返回 `LLM_INVALID_OUTPUT`，高级用户 override 不能掩盖非法 AI 输出。Episode 读取保留该映射；旧记录缺省为空。I2V 的 `end_frame_asset_id` 可为空，其 `actual_end_frame_asset_id` 仍是实际视频提取的尾帧。

C04 路由不增加必填请求字段：显式 image/video/reference_workflow_id 优先；省略时通过 `default_capabilities` 解析默认项，旧媒体默认项保留能力选择及同能力回退语义。创建响应中保存最终 ID，生成时不随默认项变化重新选型。reference_workflow_id 始终要求 TEXT_TO_IMAGE。

## C06 快速导入与 AI 识别（替代 C05 识别接口）

`POST /workflows/analyze` 接受 `{workflow, capability?}`，显式调用已配置的导演模型，只读返回 `{capability, bindings, outputs, reasons, notes}`。不拉 ComfyUI 节点清单、不修改 profile；绑定只能指向实际可写字段，输出只能指向已有节点。非法类型、链接目标、重复字段、未知角色、能力冲突及额外字段被拒绝。省略 capability 时允许 AI 建议用途，提供时必须匹配。

`POST /workflows/import` 必须提供 capability、media_type 或旧 type 之一；否则返回 422。导入只做本地结构校验并保存；不隐式调用 AI、ComfyUI 或依赖检查。显式 bindings/outputs 和既有用途标签保持兼容。Workflow 仍返回 binding_issues；旧 binding_assistance 形状保留，但候选为空，不再规则推断。

旧 `POST /workflows/{id}/auto-bind` 返回 410 / WORKFLOW_RULES_REMOVED，提示改用 AI 识别或手动绑定。AI 未配置返回 409 / CONFIGURATION_REQUIRED；超过当前 llm_timeout 返回 504 / WORKFLOW_AI_TIMEOUT（details.timeout_seconds 为本次等待上限）；超过上下文限制返回 422 / WORKFLOW_TOO_LARGE_FOR_AI；非法模型输出沿用一次纠正机会，仍非法返回 LLM_INVALID_OUTPUT。没有规则回退，取消识别不写数据。

AI 建议经用户确认后，通过普通 import/PATCH 保存；依赖检查仍为 `/workflows/{id}/validate`，试跑前继续验证。绑定完成、依赖满足和试跑成功是独立状态，导入返回不代表工作流可运行。
