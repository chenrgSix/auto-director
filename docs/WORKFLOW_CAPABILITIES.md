# Workflow Capability 与参数归属（C03 / C04 / C05 / C06）

## 目标与兼容

当前迭代将 Workflow 升级为 `media_type + capability`：`TEXT_TO_IMAGE`、`IMAGE_TO_IMAGE`、`FIRST_LAST_TO_VIDEO`、`IMAGE_TO_VIDEO`。旧 `type` 请求与响应暂保留兼容；启动时使用事务化、带版本的数据库迁移补全已有 profile、作业快照与 Episode 默认字段，保留已提交图、资产与旧时间线。

统一 limits：总片长 1～600 秒，新计划最多 240 镜。单镜仍受 workflow 能力和显存策略限制；无法在 240 镜内完成时明确报错，不偷偷增加单镜时长。旧超限时间线可读取和导出，不截断数据。

C11：Shot.duration 表示时间线长度，视频作业另记 timeline_duration 和实际渲染 duration。秒数节点根据已选模型的类型、min/max/step/enum 向上取合法值（2.86 秒可生成 4 秒后裁切），不超出 workflow 与适用显存上限；帧数节点保留既有转换。云端 API 视频以 duration 节点的 api_node 元数据判定，免除本地视频时长降级，图片继续遵守本地显存策略。无合法时长时提前失败，不生成参考图。

## 参数契约

每个动态参数包含 `owner`、`editable`、`override_policy`（`advanced` 或 `never`）。角色绑定决定语义归属：prompt/negative/camera_motion/motion_strength 归 AI；duration 归 Director；媒体输入归 AssetResolver；尺寸/FPS/batch/seed 归 system；steps/cfg/sampler/scheduler/model 及未映射字段默认归 workflow。无角色且非素材的自定义字段可显式声明 ai/user owner。

优先级：原始模板 < profile 默认参数 < 自动填充 < 高级用户 override。节点链接不可覆盖；高级覆盖不绕过数值、节点、真实素材归属、单镜时长和显存约束。素材覆盖提交 asset ID，由服务端上传并转换为 ComfyUI 输入名。单镜时长覆盖参与规划，当前支持统一每镜固定时长，须整除用户总时长；帧数输入按 binding 偏移与 FPS 换算，不能只修改渲染帧数而破坏总时间线。

## 自动生成

默认模式只需要 Idea、总时长、比例、风格。Director 分批规划；文生图生成 Visual Bible 参考资产；图生图可作为关键帧工作流，自动接收角色参考或前一真实关键帧；视频按 capability 消费首帧或首尾帧。ContinuityManager 决定前镜实际尾帧/视频继承；AssetResolver 校验并上传实际资产，禁止使用模板占位路径。

高级模式显示所选工作流的全部可编辑动态参数、owner 和逐项覆盖开关；未勾选项目继续自动或继承工作流。OOM 降级优先遵守更严格的系统约束，并保留现有共享重试预算、替代模板、QA 和 FFmpeg 拼接。

### C04 视频能力分支

`FIRST_LAST_TO_VIDEO` 生成首尾两张关键帧并做双帧 QA；`IMAGE_TO_VIDEO` 只生成/绑定首帧并做单帧 QA，不创建 `SHOT_END_FRAME`。两者均从实际视频提取尾帧用于连续性。I2V 转场重试重新生成首帧；OOM 分段只继承上一段实际尾帧，不生成中间目标帧。低显存替代工作流按其自身 capability 决定是否需要目标尾帧。

### C04 通用 AI 参数

Bible/Shot 使用 `ai_parameters: {"workflow_id": {"node.field": value}}`。运行时按当前选定工作流的 AI owner 字段构造输出 schema，两层映射均禁止未知键，参数限制来自最新 object_info；本地严格检查 type/min/max/enum，拒绝数字字符串、布尔冒充数字、非有限数和错误 owner。Provider 沿用一次纠正机会，仍非法则返回 `LLM_INVALID_OUTPUT`。C16 起 `prompt` 角色由当前阶段的画面描述自动绑定，不再允许在映射中重复生成；首帧、尾帧、视频使用各自提示词，旁白进入独立的 `narration_text` 脚本字段。上下文提供 media_type/capability/source；其他 AI 参数和高级覆盖契约保持不变，旧重复映射由预览/渲染兼容过滤，保存时归档保留。

ParameterResolver 将合法 AI 值覆盖旧语义自动值或工作流默认值，再应用高级用户覆盖。非角色参数经独立 AI 通道进入 deepcopy patch，不受用户 editable 锁影响；锁只控制用户覆盖。作业保存 `ai_parameter_values` 并纳入缓存签名，执行前按最新节点约束再次校验。旧 Bible/Shot/Job 无映射时按空值读取，继续使用原语义自动值及模板默认，无需重写已提交图或数据库迁移。

### C04 Capability Router 基础层

`CapabilityRouter.resolve(capability, workflow_id=None)` 查询四类 `default_capabilities`；显式 ID 优先且必须匹配请求能力。未配置能力默认项时，仅允许同能力的旧 `default_image/default_video` 回退；缺失或错误映射明确失败，不替换为不匹配的工作流。

`select(media_type, workflow_id=None)` 保持现有 Episode/UI 兼容：显式 ID 按媒体类型校验；未指定时，旧媒体默认项决定所选能力，再经能力映射取得 profile。只有能力映射的设置也可读取。创建时将选中的图像、视频和文生图参考 ID 写入 Episode，生成/恢复使用这些 ID，后续修改默认项不改变已创建任务。当前仅提供路由基础层，不增加新的选择 UI 或智能自动选型。

C08 增加显式更换入口：已创建短片可通过 `PATCH /episodes/{id}/workflows` 更新三类 ID，但必须停止并核对未完成作业。C18 起未渲染故事保留计划、镜头、时长及提示词，只校验新能力与参数；不兼容则保留原配置。C19 起已有渲染进度也保留故事和素材，新绑定只生成缺失部分或显式重试项。继续生成时检查新工作流及实时显存，自动选择仍不随全局默认变动。详见 [接口契约](API_CONTRACT.md#更换已创建短片的工作流c08)。

## C06 快速导入与实际字段配置

导入只保存明确用途与工作流 JSON，不等待 ComfyUI 或 AI。C05 节点/连线规则识别已移除；旧显式标签、手动绑定和历史记录继续生效。AI 识别为可取消的独立操作，复用导演模型，先返回用途/绑定/输出建议与原因；服务端拒绝虚构目标、链接覆盖、类型错误、重复字段和能力冲突。未知项目允许留空，失败不回退规则。用户确认后应用空缺项，已有选择优先。

页面只显示工作流真实可写字段，在每个字段上指定用途、值与覆盖策略，固定角色表与动态参数表合并。文生图不显示预设参考图/首尾帧，I2V 不显示结束画面；用途标签是内部自动填充契约，不额外创造 Workflow 参数。依赖检查仍单独执行，试跑前保留全部校验。

## 交付门禁

C06 完整 `make check` 为 **142 passed**，Ruff/格式、ESLint、TypeScript 与构建通过。23 项新测试替代 8 项已移除规则推断测试，原有 119 项保留（Motion fixture 改为显式用途标签）。浏览器验收与限制见 [验收记录](ACCEPTANCE.md)。

C05 状态：**VERIFIED（本地工程与浏览器门禁）**。完整 `make check` 为 **127 passed**；新增 8 项识别/API 用例，原有能力与流水线回归保持通过。浏览器验证导入、歧义确认、保存、夹具试跑、I2V 输入、自动补齐与移动布局；正式服务核对缺失模型提示。详见 [C05 验收](ACCEPTANCE.md#c05-工作流接入向导2026-09-10)。真实模型生成与远端 CI 未执行。

状态：**VERIFIED（本地工程门禁）**。C04 完整 `make check` 为 **119 passed**，Ruff/格式、ESLint、TypeScript、构建通过，新增 28 项用例。覆盖 I2V 单帧与 OOM/QA 分支、通用 AI 参数最终 patch/非法值拒绝、四类能力路由及显式 ID 兼容；原有迁移、60/90 秒计划、600 秒/240 镜边界、素材、Continuity 和 FFmpeg 回归全部保持通过。详见 [C04 验收](ACCEPTANCE.md#c04-能力分支ai-参数与路由收尾2026-09-10)。C03 浏览器证据保留为历史记录，本轮未重跑浏览器或真实模型验收。


## C13 分镜节奏与时间约束

新计划把每镜最短/最长秒数、建议时长/镜头数与允许数量同时发给 Agent，并通过动态 JSON schema 验证。默认争取 2.5 秒下限；片长或能力不允许时按可行区间下降，全局 1 秒边界和用户合法固定时长继续生效。镜头最多时长仍是 workflow、显存与用户上限的交集，不能因想要长镜头跳过显存保护。

时间线归一化采用同时按权重分配与百分之一秒最大余数，保留合法原比例；10 秒、上限 3 秒的默认新计划收敛到 4×2.5 秒。旧计划和资产不修改，60/90/600 秒继续分批规划。
