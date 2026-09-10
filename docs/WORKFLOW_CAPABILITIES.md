# Workflow Capability 与参数归属（C03 / C04）

## 目标与兼容

当前迭代将 Workflow 升级为 `media_type + capability`：`TEXT_TO_IMAGE`、`IMAGE_TO_IMAGE`、`FIRST_LAST_TO_VIDEO`、`IMAGE_TO_VIDEO`。旧 `type` 请求与响应暂保留兼容；启动时使用事务化、带版本的数据库迁移补全已有 profile、作业快照与 Episode 默认字段，保留已提交图、资产与旧时间线。

统一 limits：总片长 1～600 秒，新计划最多 240 镜。单镜仍受 workflow 能力和显存策略限制；无法在 240 镜内完成时明确报错，不偷偷增加单镜时长。旧超限时间线可读取和导出，不截断数据。

## 参数契约

每个动态参数包含 `owner`、`editable`、`override_policy`（`advanced` 或 `never`）。角色绑定决定语义归属：prompt/negative/camera_motion/motion_strength 归 AI；duration 归 Director；媒体输入归 AssetResolver；尺寸/FPS/batch/seed 归 system；steps/cfg/sampler/scheduler/model 及未映射字段默认归 workflow。无角色且非素材的自定义字段可显式声明 ai/user owner。

优先级：原始模板 < profile 默认参数 < 自动填充 < 高级用户 override。节点链接不可覆盖；高级覆盖不绕过数值、节点、真实素材归属、单镜时长和显存约束。素材覆盖提交 asset ID，由服务端上传并转换为 ComfyUI 输入名。单镜时长覆盖参与规划，当前支持统一每镜固定时长，须整除用户总时长；帧数输入按 binding 偏移与 FPS 换算，不能只修改渲染帧数而破坏总时间线。

## 自动生成

默认模式只需要 Idea、总时长、比例、风格。Director 分批规划；文生图生成 Visual Bible 参考资产；图生图可作为关键帧工作流，自动接收角色参考或前一真实关键帧；视频按 capability 消费首帧或首尾帧。ContinuityManager 决定前镜实际尾帧/视频继承；AssetResolver 校验并上传实际资产，禁止使用模板占位路径。

高级模式显示所选工作流的全部可编辑动态参数、owner 和逐项覆盖开关；未勾选项目继续自动或继承工作流。OOM 降级优先遵守更严格的系统约束，并保留现有共享重试预算、替代模板、QA 和 FFmpeg 拼接。

### C04 视频能力分支

`FIRST_LAST_TO_VIDEO` 生成首尾两张关键帧并做双帧 QA；`IMAGE_TO_VIDEO` 只生成/绑定首帧并做单帧 QA，不创建 `SHOT_END_FRAME`。两者均从实际视频提取尾帧用于连续性。I2V 转场重试重新生成首帧；OOM 分段只继承上一段实际尾帧，不生成中间目标帧。低显存替代工作流按其自身 capability 决定是否需要目标尾帧。

### C04 通用 AI 参数

Bible/Shot 增加 `ai_parameters: {"workflow_id": {"node.field": value}}`。运行时按当前选定工作流的 AI owner 字段构造输出 schema，两层映射均禁止未知键，参数限制来自最新 object_info；本地严格检查 type/min/max/enum，拒绝数字字符串、布尔冒充数字、非有限数和错误 owner。Provider 沿用一次纠正机会，仍非法则返回 `LLM_INVALID_OUTPUT`。

ParameterResolver 将合法 AI 值覆盖旧语义自动值或工作流默认值，再应用高级用户覆盖。非角色参数经独立 AI 通道进入 deepcopy patch，不受用户 editable 锁影响；锁只控制用户覆盖。作业保存 `ai_parameter_values` 并纳入缓存签名，执行前按最新节点约束再次校验。旧 Bible/Shot/Job 无映射时按空值读取，继续使用原语义自动值及模板默认，无需重写已提交图或数据库迁移。

### C04 Capability Router 基础层

`CapabilityRouter.resolve(capability, workflow_id=None)` 查询四类 `default_capabilities`；显式 ID 优先且必须匹配请求能力。未配置能力默认项时，仅允许同能力的旧 `default_image/default_video` 回退；缺失或错误映射明确失败，不替换为不匹配的工作流。

`select(media_type, workflow_id=None)` 保持现有 Episode/UI 兼容：显式 ID 按媒体类型校验；未指定时，旧媒体默认项决定所选能力，再经能力映射取得 profile。只有能力映射的设置也可读取。创建时将选中的图像、视频和文生图参考 ID 写入 Episode，生成/恢复使用这些 ID，后续修改默认项不改变已创建任务。当前仅提供路由基础层，不增加新的选择 UI 或智能自动选型。

## 交付门禁

状态：**VERIFIED（本地工程门禁）**。C04 完整 `make check` 为 **119 passed**，Ruff/格式、ESLint、TypeScript、构建通过，新增 28 项用例。覆盖 I2V 单帧与 OOM/QA 分支、通用 AI 参数最终 patch/非法值拒绝、四类能力路由及显式 ID 兼容；原有迁移、60/90 秒计划、600 秒/240 镜边界、素材、Continuity 和 FFmpeg 回归全部保持通过。详见 [C04 验收](ACCEPTANCE.md#c04-能力分支ai-参数与路由收尾2026-09-10)。C03 浏览器证据保留为历史记录，本轮未重跑浏览器或真实模型验收。
