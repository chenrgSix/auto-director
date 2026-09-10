# Workflow Capability 与参数归属（C03）

## 目标与兼容

当前迭代将 Workflow 升级为 `media_type + capability`：`TEXT_TO_IMAGE`、`IMAGE_TO_IMAGE`、`FIRST_LAST_TO_VIDEO`、`IMAGE_TO_VIDEO`。旧 `type` 请求与响应暂保留兼容；启动时使用事务化、带版本的数据库迁移补全已有 profile、作业快照与 Episode 默认字段，保留已提交图、资产与旧时间线。

统一 limits：总片长 1～600 秒，新计划最多 240 镜。单镜仍受 workflow 能力和显存策略限制；无法在 240 镜内完成时明确报错，不偷偷增加单镜时长。旧超限时间线可读取和导出，不截断数据。

## 参数契约

每个动态参数包含 `owner`、`editable`、`override_policy`（`advanced` 或 `never`）。角色绑定决定语义归属：prompt/negative/camera_motion/motion_strength 归 AI；duration 归 Director；媒体输入归 AssetResolver；尺寸/FPS/batch/seed 归 system；steps/cfg/sampler/scheduler/model 及未映射字段默认归 workflow。自定义字段可声明 user owner。

优先级：原始模板 < profile 默认参数 < 自动填充 < 高级用户 override。节点链接不可覆盖；高级覆盖不绕过数值、节点、真实素材归属、单镜时长和显存约束。素材覆盖提交 asset ID，由服务端上传并转换为 ComfyUI 输入名。单镜时长覆盖参与规划，当前支持统一每镜固定时长，须整除用户总时长；帧数输入按 binding 偏移与 FPS 换算，不能只修改渲染帧数而破坏总时间线。

## 自动生成

默认模式只需要 Idea、总时长、比例、风格。Director 分批规划；文生图生成 Visual Bible 参考资产；图生图可作为关键帧工作流，自动接收角色参考或前一真实关键帧；视频按 capability 消费首帧或首尾帧。ContinuityManager 决定前镜实际尾帧/视频继承；AssetResolver 校验并上传实际资产，禁止使用模板占位路径。

高级模式显示所选工作流的全部可编辑动态参数、owner 和逐项覆盖开关；未勾选项目继续自动或继承工作流。OOM 降级优先遵守更严格的系统约束，并保留现有共享重试预算、替代模板、QA 和 FFmpeg 拼接。

## 交付门禁

状态：**VERIFIED（本地工程门禁）**。覆盖四类 capability 契约、迁移幂等/回滚、旧 API、60/90 秒计划、600 秒与 240 镜边界、图生图真实素材引用、owner 自动填充、用户覆盖优先级/拒绝越界、连续性和 OOM 回归；完整 `make check` 为 91 passed，Ruff/格式、ESLint、TypeScript、构建通过；默认与高级模式的隔离浏览器生成、覆盖权限保存/刷新及 390px 布局通过。详细证据见 [C03 验收](ACCEPTANCE.md#c03-workflow-capability-与-owner-验收2026-09-10)。真实模型质量验收仍独立于本地夹具。
