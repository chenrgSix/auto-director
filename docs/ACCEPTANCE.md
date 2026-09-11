# 验收记录

## C16 画面提示词来源与旁白分离（2026-09-11）

- 完整 `make check`：**293 passed**（63.13 秒），63 个 Python 文件 Ruff/格式、前端 lint/typecheck/build 通过。新增 15 项 schema 重复角色拒绝/一次纠正、无旁白仍要求画面非空、首尾帧/视频/参考最终 patch、自定义 AI/高级覆盖、90 秒旧记录归档保存及原子回滚回归。
- 隔离浏览器：旁白脚本独立标明尚未合成音频；修改视频画面提示词、保存并刷新后两者分别保留，开始按钮可用。截图：[旁白独立展示](evidence/preview-narration-separated.png)。未做真实模型质量或远端 CI 验收。
- 正式 8000 已加载 C16；当前短片原始记录备份于 ignored `data/repairs/c16-preview-81006c7c/before.json`。实际保存被旧云端视频标识丢失问题拒绝（错误的 3 秒显存上限与工作流最低 4 秒冲突）；version=28、无渲染提交，实际修复保存待 C17 完成。

## C15 空 AI 提示词与开始按钮修复（2026-09-11）

复现：实际短片 `81006c7c-da6b-41bc-8ac9-4f4de8184490` 为 AWAITING_REVIEW，18×5 秒=90 秒。第 4、7、9 镜的 AI-owned 图像 prompt 是空字符串，原始 start/end_frame_prompt 完整，但保存的 preview_prompt_view 被覆盖为空；原网页把字段失败统一显示成“请检查各镜时长与提示词”。

完整 `make check` 通过，Ruff/格式（62 Python 文件）、ESLint、TypeScript/Vite 及 pytest **278 passed**（61.15 秒，无跳过）。新增 10 项回归验证空/纯空白 AI prompt 回退到既有提示词且进入最终 patch、其他 AI 参数保留、不额外调用模型、旧视图只读更新、非法 AI 类型/owner/未知键仍拒绝、显式用户覆盖（含空字符串）保持、未使用 I2V 尾帧和具体字段错误。

隔离浏览器：空白视频提示词明确显示“第 1 镜…视频提示词不能为空”，点击可定位；合计 9/10 秒时显示“还差 1.00 秒”且仍拦截开始。正式服务无活动任务后更新，实际 90 秒页面显示开始按钮 enabled=true，并解释空值回退。[页面证据](evidence/preview-blank-prompt-fixed.png)。

数据库完整记录前后相同，SHA-256 为 `28e4dbe57face536b538c3c8bb5b67d34c0007c2bc23ad624965db149e222464`，version 仍为 28。所有原始 prompts、plan 和镜头时长不变，API 仅更新派生预览；该单集无渲染 Job。未点击真实开始、未调用真实 LLM/ComfyUI 渲染，未执行远端 CI。

## C14 分镜预览与确认生成（2026-09-11）

工程门禁：完整 `make check` 成功，Ruff/格式（62 个 Python 文件）、ESLint、TypeScript 与 Vite 构建通过；pytest **268 passed**，58.76 秒，无跳过。新增 `test_episode_preview.py` 16 项回归覆盖：

- 预览完成前后均无 ComfyUI POST /prompt、Job 或生成 Asset；60/90 秒每镜提示词齐全，SSE 在待确认时结束。
- 编辑标题/时长同步 plan，首帧/尾帧/视频提示词进入最终 patched JSON；原 AI 自定义 denoise 参数保留，固定用户 prompt 优先；I2V 无尾帧渲染。
- 总时长/单镜范围/固定时长、重复或缺少 ID、空白提示词、任意 AI 映射注入与锁定输入原子拒绝；过期版本不能保存/确认，其他渲染入口不能绕过确认。
- 取消并继续准备、待确认状态重启、工作流版本变化、重绑定后历史 Job 兼容；原全量流水线、QA、OOM、媒体与旧 API 回归继续通过。

隔离浏览器 `127.0.0.1:8011`：10 秒/2 镜预览，修改标题和视频提示词；时长改成合计 9 秒时禁止开始，恢复合法后保存并刷新保留。第二页面更新时，第一页未保存文本保持且明确提示版本冲突；重新载入后恢复编辑。未经独立保存直接开始，会保存新提示词、确认并最终导出 10.00 秒 MP4。390px 视口的 `clientWidth=scrollWidth=390`；桌面与手机截图见 [桌面](evidence/storyboard-preview-desktop.png)、[手机](evidence/storyboard-preview-mobile.png)。上述模型/ComfyUI 使用夹具，FFmpeg 为真实执行。

正式 `127.0.0.1:8000`：确认无活动 Episode/未完成 Job 后重启，health=200，OpenAPI 含 POST/PATCH preview 与 POST approve，网页入口已更新。原 `43a9ba95-46cb-4d97-b836-051c42de9c17` 完整记录 SHA-256 前后均为 `94b4a22b48bfe05d39b65de32c342d07aadc2d77cbce336a3705d26819d42731`，仍为 COMPLETED/10 秒。未执行本轮真实 LLM/ComfyUI 生成、视觉质量评审或远端 CI。

## 当前结论（2026-09-10）

P0 + Phase 1/2 MVP 的本地工程闭环已完成。ComfyUI/LLM/VLM 依赖使用隔离夹具验证协议和编排，媒体处理执行真实 FFmpeg；尚未进行真实 AI 生成或视觉效果验收。按用户要求，没有部署 ComfyUI、安装节点或下载权重。

## 已执行门禁

| 门禁 | 结果 | 证据与范围 |
| --- | --- | --- |
| 文档与贡献指南 | 通过 | 任务、模块、接口、运行、决策、迭代与验收同步；AGENTS.md 为当前目录和命令 |
| 锁定依赖安装 | 通过 | `make install`；受限环境使用临时 uv/Python/npm 缓存，33 个 Python 包、175 个 npm 包 |
| 统一本地检查 | 通过 | 根目录 `make check` 成功退出 |
| Python 检查 | 通过 | Ruff lint + format check，46 个 Python 文件 |
| 后端测试 | 通过 | **91 passed**，无跳过；耗时 42.04 秒；pytest/pytest-asyncio |
| 前端检查 | 通过 | ESLint、`tsc --noEmit`、`tsc -b && vite build`；Vite 7.3.6 |
| FFmpeg 媒体 | 通过 | 真实 H.264/AAC 编码、混合尺寸/FPS、有声/无声拼接、尾帧和裁切边界 |
| 5/10/15/90 秒流水线 | 通过（模拟模型） | API 创建→计划→参考→首尾帧→QA→视频→实际尾帧→真实 FFmpeg 导出，误差 ≤0.5 秒；90 秒覆盖跨批规划 |
| 600 秒计划与合成 | 通过（分层验证） | 240 镜分批规划、旧 600 镜时间线兼容与真实 FFmpeg 120 段合成；不包含真实 AI 渲染 |
| 浏览器交互 | 通过（本地） | 真实未配置工作台 + 8011 隔离夹具，见下方步骤和截图 |
| 真实 ComfyUI/LLM/VLM | 未执行 | 用户将在工程交付后提供服务；没有把协议夹具当作真实集成 |
| 真实视觉/性能指标 | 未执行 | 需真实 5/10/15 秒、狮子案例和多次样本统计 |
| CI / 生产准入 | 未核验 | 已加入 CI 配置，本迭代未核对远端 CI；当前定位为本地单用户应用 |

环境：macOS arm64，Python 3.12.11，Node 22.23.1，FFmpeg/ffprobe 8.1.1。测试有两项依赖弃用提醒：Starlette TestClient 的 httpx 适配和 AnyIO BlockingPortal 别名；当前不影响通过，不等同于运行故障。

安装时默认 npm 缓存写入受沙箱限制，改用 `npm_config_cache=/private/tmp/autodirector-npm-cache` 后成功；没有修改用户缓存权限。文档本地链接检查覆盖 11 份文件，未发现失效链接。

## 自动回归的关键断言

- API JSON 图、循环/链接错误、数字角色、重复输出、模型与字段校验、绑定 patch、duration→frames。
- 导演 JSON 修复、Pydantic schema、总时长与单镜上下限、质量/显存预算和 QA 分流。
- 提交超时、损坏响应和 5xx 保持 UNKNOWN，不重复 POST；已知 prompt_id 恢复只读取 history；取消不 interrupt 他人作业。
- 取消未退出前拒绝重复入队/删除；重启保留 prompt_id 和已有资产；时间线变更失效依赖镜头。
- 视频单独重试保留关键帧和不相关镜头；动作 QA 失败只重做视频，旧 QA 结果保留。
- OOM 预算 1 时有界失败，预算 2 时短分段完成；替代工作流重命名节点仍可工作；分段继承实际尾帧。
- 红/蓝两段真实视频验证连续性取的是导出范围内的尾帧，而非裁掉的后半段。
- 上传拒绝伪图片、文件路径穿越、敏感配置返回与不可信网页写入。

对应文件：`backend/tests/test_{store_security,workflows,comfyui,agents,media,api_pipeline}.py`。生产入口没有引用 tests 中的模型夹具。

## 浏览器验收记录

1. `make build` 后启动真实应用，检查创建页、工作流绑定/参数、设置页；5 秒切换更新选择状态，Idea 输入后生成按钮可用。
2. 桌面 1280px 查看布局；390×844 下创建页和设置页 `scrollWidth == clientWidth == 390`，无横向溢出。
3. 用 `make browser-fixture` 启动独立临时数据服务；UI 创建“隔离浏览器验收：三只狮子进入侏罗纪。”5 秒短片，完成后显示 **5.02 秒**、首尾帧、视频和下载链接。
4. 点击“仅重试视频”，完成后成片资产 ID 由 `d0e61074-901b-4140-878c-8fd10f0307a5` 更新为 `3ea9fa7c-8e88-4d3f-82b9-cde0e284ca57`；API 回归另外确认下载内容为 MP4。
5. 检查以上流程没有应用控制台 error。截图记录在 [创作页面](evidence/studio-create.png) 和 [隔离夹具成片页](evidence/fixture-episode.png)。后者的绿图和彩条视频为明确的合成测试素材，不是狮子生成效果。

## 真实接入与视觉验收门禁 T02

需要用户提供 ComfyUI 服务根地址、可用节点/模型信息或 API JSON 工作流，以及 OpenAI-compatible 模型端点、导演模型名和可选视觉模型名。通过“连接与设置”填写端点、模型名和密钥。

1. 记录 ComfyUI 版本、GPU/显存、节点/模型版本、workflow hash、LLM/VLM 模型与配置（无密钥）。
2. 两类 Workflow 完成导入→角色识别→参数编辑→校验→试跑→设置默认；检查参考图实际进入条件输入。
3. Idea 分别生成 5/10/15 秒；保存 Episode/RenderJob ID、耗时、重试、MP4 与 ffprobe 时长，误差 ≤0.5 秒。
4. “三只狮子进入侏罗纪”：人工检查数量、外观、生态、光线、黑帧/闪屏和无关画面；保存 QA 与人工结论。
5. 真实断线、缺节点/模型、OOM、取消与重启；验证提示及恢复流程。
6. 固定 10 个 Idea 覆盖动物、科幻、历史、环境、灾难、微剧情，多次实跑后再评估原文 §59 指标。

## 当前能力边界

- 内置 SD1.5 只有文本锚点，视觉参考/身份一致性需要兼容替换工作流；Wan 14B FP8 不保证在任意 GPU 上可运行。
- 动态表单覆盖标量，嵌套 DynamicCombo 通过 API JSON 保留；编辑嵌套结构需重新导入。
- VLM 可选；未启用时不会进行视觉 QA 和 High 候选评分。首/中/尾采样不能证明逐帧没有短暂瑕疵，真实验收仍需观看视频。
- 单用户、单 worker、无外网用户认证；Phase 3 的音频生成、多机/云市场等见任务路线图。

每次验证同步更新命令、结果与限制，禁止用文件存在或按钮可点击替代功能验收。

## C01 在线配置验收（2026-09-10）

`make check` 全部通过：63 个测试（含 21 个配置回归）、Ruff/格式、前端 lint/类型与构建。新断言覆盖保存后现有 Provider/渲染器读取新值、重启恢复、环境/旧设置兼容、0600 文件权限、空密钥保留与显式清除、凭据不回显、端点变更确认、运行中/取消未退出/UNKNOWN 保护、DNS 校验期间入队竞争、写入失败不发布配置及清理临时文件。

浏览器在隔离夹具中填写测试端点/模型/密钥，保存后提示即时生效，输入框清空；刷新后模型仍保留且不回显密钥；勾选清除后保存，密钥已配置状态消失；检测已保存 ComfyUI 连接成功。390×844 视口无横向溢出。正式服务检查没有活动作业后已重启，在线配置页加载正常；真实模型服务仍未接入。页面截图见 [在线配置](evidence/online-settings.png)。

## C02 总时长验收（2026-09-10）

`make check` 成功：**77 passed**，无跳过，Ruff/格式、ESLint、TypeScript 和 Vite 构建全部通过。本次新增 14 个测试用例，未调用真实 ComfyUI/模型服务。

- API 创建/读取覆盖 1、各预设、31、599.99 和 600 秒；拒绝 0/601 秒和每镜 600 秒。600 个一秒镜头可以完整重排、保存、读回。
- Director 夹具覆盖 600 秒 / 5 秒每镜，以及 600、599 秒 / 1 秒每镜；含小数能力边界。断言每批 ≤12 镜、全局索引连续、总和准确、最近三镜上下文与跨批连续转场保留；取消后不继续发请求。
- 90 秒 API 完整流水线使用模拟模型和真实 FFmpeg，成片误差 ≤0.5 秒。独立媒体回归将 120 个 64×64、有声音轨的合成片段分别拼为 600 秒和 598.8 秒；视频帧数等于累计时长对齐后的帧数，音视频时长误差均 <0.1 秒，末帧可解码。修复前，120 × 4.99 秒实际输出约 600.021 秒并触发失败。
- 浏览器确认六个预设，1/600 秒可提交，0/601/空值不可提交；选择预设会清空自定义值。隔离夹具中自定义 1 秒生成成功，页面显示 **1.00 秒 MP4** 和下载链接。
- 正式工作台已重启加载新策略；1280px 和 390×844 页面检查通过，移动端 `scrollWidth == clientWidth == 390`。截图：[桌面时长](evidence/duration-desktop.png)、[移动时长](evidence/duration-mobile.png)。

600 秒是产品输入上限；上述长视频使用合成素材，不能证明真实十分钟故事质量、生成耗时、显存或成功率。真实模型验收继续沿用 T02，并按实际能力增加长时长样本。


## C03 Workflow Capability 与 owner 验收（2026-09-10）

最终 `make check` 成功：**91 passed**，无跳过，42.04 秒；46 个 Python 文件 Ruff/格式、前端 ESLint/TypeScript/Vite 全部通过。相对 C02 新增 14 个用例；原有“600 个一秒镜头可新建”断言按新 240 镜策略调整为 600 秒 / 240 镜，原有 600 镜存量重排测试继续通过。

- 四类 capability 导入、旧 type 推导、矛盾 media_type 拒绝；基于动态绑定识别 owner，重绑和 object_info 校验后保持一致。
- 图生图夹具实际经过文生图参考生成 → 图生图首尾帧 → 视频与 FFmpeg。断言 reference_image 为已存在的 Episode 资产，首帧输入角色参考、尾帧输入刚生成的首帧，连续镜头继承前镜实际尾帧。
- IMAGE_TO_VIDEO 夹具生成 6 秒短片，用户 49 帧 / 16 FPS / 偏移 1 覆盖参与规划，得到两镜各 3 秒；提示词、negative、steps、尺寸覆盖进入实际提交图。视频仅要求首帧。
- 模板默认 steps/cfg/model、自动 AI/system 参数和用户覆盖按顺序解析；规则锁定、普通模式覆盖、越界尺寸/时长拒绝。素材覆盖实际上传，同步到 Shot；媒体类型/归属不符、LoadImage 伪绑定 prompt 或伪改 owner 均拒绝。
- 60/90 秒分批规划通过；600 秒以 240 镜覆盖边界，241 秒 / 每镜 1 秒在调用模型前报 LIMIT_EXCEEDED。固定时长不整除总时长明确失败，OOM 分段保留实际尾帧并覆盖原始用户尺寸/时长输入。
- 迁移幂等且失败时整批回滚；工作流参数、旧 600 镜计划、引用资产、patched_workflow、prompt_id 与 UNKNOWN 状态保持。正式本地库已在停机备份后迁移至版本 1，公开 API 返回 240 镜限制和完整 owner。
- 浏览器隔离夹具：默认模式仅填写 Idea/时长完成 **1.00 秒 MP4**；高级模式覆盖视频 prompt 与 steps=9 后也导出 1.00 秒，作业记录确认两项来源均为 user。参数覆盖权限关闭后保存、刷新仍关闭；四类导入选项存在。390×844 下 `scrollWidth == clientWidth == 390`。

截图：[默认模式](evidence/capability-default-mode.png)、[高级移动表单](evidence/capability-advanced-mobile.png)、[owner 与覆盖规则](evidence/workflow-owners.png)。默认/高级模式截图来自正式页面，owner 规则保存截图来自隔离夹具；生成媒体均为合成测试素材。

未安装 ComfyUI/权重、未执行真实模型生成或视觉/性能验收；没有将四类 capability 支持等同于四套已验证的模型模板。内置仍为文生图和首尾帧视频，图生图/I2V 通过导入使用。远端 CI/生产准入未执行。

## C04 能力分支、AI 参数与路由收尾（2026-09-10）

完整 `make check` 成功：**119 passed**，无跳过，pytest 56.50 秒；50 个 Python 文件 Ruff/格式、前端 ESLint/TypeScript/Vite 构建全部通过。相对 C03 新增 28 项用例；保留原有 91 项，并加强 I2V 的资产/作业断言。仅有既有 Starlette/httpx 和 AnyIO 弃用提醒。

- I2V 6 秒双镜生成断言没有 SHOT_END_FRAME 作业、没有 end_frame 素材绑定、每次关键帧 QA 仅一个资产；实际视频尾帧存在，下一镜仍继承它。转场 QA 失败只重新生成首帧和视频。
- OOM 回归同时覆盖 I2V/FLF、重试预算不足、短分段和重命名节点的替代模板。I2V 不产生 SHOT_INTERMEDIATE_FRAME，分段仍继承上一段实际尾帧并保持时间线；FLF 保留目标首尾帧行为。
- Bible/Shot 的自定义 `sampler.denoise` 在同名键的图像/视频 workflow 中分别生成不同值，最终 patched_workflow 与 ComfyUI 夹具实际接收的 JSON 一致；用户高级覆盖优先，默认模式下用户锁不妨碍 AI 自动填写，模板保持不变。
- schema 与 Resolver 拒绝未知 workflow/key、错误 owner、数字字符串、布尔冒充数字、小数冒充整数、越界、非法枚举、null/嵌套对象、NaN/Infinity；非法 AI 值不能被有效用户覆盖掩盖。真实 Provider JSON 校验路径沿用一次纠正机会；两次非法则 Episode 失败，未提交任何镜头渲染。执行前 object_info 约束变化也会阻止 /prompt。
- Router 验证四类能力默认项、显式 ID、旧媒体默认回退、只有能力映射的设置、缺失/错误能力拒绝。图生图 + I2V 路由集成在创建后更改默认项，仍使用原 Episode ID 选择完成真实 FFmpeg 合成。
- 旧数据缺少 ai_parameters/ai_parameter_values 时按空映射处理，无数据库版本升级；原有迁移、UNKNOWN 恢复、60/90 秒规划及长视频 FFmpeg 回归全部通过。

本轮未部署 ComfyUI/模型、未调用真实 AI 服务、未重跑浏览器验收，也未执行远端 CI 或生产验收。

## C05 工作流接入向导（2026-09-10）

完整 `make check` 成功：**127 passed**，无跳过，pytest 44.74 秒；52 个 Python 文件 Ruff/格式、前端 ESLint/TypeScript/Vite 构建全部通过。相对 C04 新增 8 项识别/API 用例，保留既有 119 项。仅有既有 Starlette/httpx 和 AnyIO 弃用提醒。

- 无标签 T2I、I2I、首尾帧视频与 I2V：通过实际条件/素材连线识别 prompt、negative、参考/首尾帧与保存输出；deepcopy patch 保持原模板和节点链接。
- 多候选不自动猜测，PreviewImage 不当作最终保存输出；未知模型帧数规则及错误类型字段不自动绑定。补齐保留已有手动 prompt、占用目标及自定义帧数倍数。
- analyze 不写数据库；未声明媒体类型的导入可推断用途，旧显式 API 保持兼容；绑定问题与缺失模型分别返回。既有记录不会在读取时被改写。

隔离浏览器服务 `python -m tests.browser_server` 使用临时数据库与合成媒体，执行：

1. 实际上传去掉标签、含两个保存节点的文生图 JSON；用途自动显示文生图，只出现一个最终输出待确认项。选择主输出，保存并检查依赖，重载后选择保留。
2. 点击“保存并试跑”，夹具作业进入 `COMPLETED` 并显示合成图片；重命名后保存立即显示“配置已保存”，依赖检查成功。
3. I2V 只显示起始画面上传，概览与高级绑定均无结束画面。清空 prompt 后点击自动补齐恢复 `positive.text`，保留 `negative.text`。
4. 390×844 视口下 `scrollWidth = innerWidth = 390`，高级设置默认收起，应用控制台无 error。截图：[桌面 I2V](evidence/workflow-guide-desktop.png)、[移动端](evidence/workflow-guide-mobile.png)。

确认正式服务无运行 Episode/Job 后重启 8000，健康检查返回 200。浏览器读取现有 SD1.5 profile，输入输出已识别，模型区域单独显示缺少 `v1-5-pruned-emaonly.safetensors` 与处理提示；未触发真实渲染。修正依赖提示受全局 flex 样式影响的换行后，前端 lint、typecheck、build 再次通过。截图：[模型依赖](evidence/workflow-guide-dependencies.png)。

限制：自动识别覆盖已知常见字段与连线，特殊节点、歧义及未知帧数规则仍需高级确认；旧记录媒体类型选错需重新导入。夹具试跑证明交互与作业链路，不代表真实模型或视觉质量验收。本轮未下载模型、未调用真实 AI 生成，未执行远端 CI。


## C06 快速导入、AI 建议与实际字段（2026-09-10）

完整 `make check` 成功：**142 passed**，无跳过，pytest 119.61 秒。52 个 Python 文件 Ruff/格式、前端 ESLint/TypeScript/Vite 构建通过；最后前端准备状态简化后再次通过 lint/typecheck/build。保留既有 119 项（Motion fixture 显式标注用途），以 23 项新用例替代 8 项 C05 规则识别测试。仅有既有 Starlette/httpx 和 AnyIO 弃用提醒。

### 耗时证据

旧导入依次执行 analyze → import → validate，最后一步拉取 ComfyUI 全部 object_info。对 `10.0.1.10:8188/object_info` 的单次只读测量在 8.005 秒截止，HTTP 200、声明大小 3074993 字节，仅收到 368627 字节。此结果说明依赖清单下载会明显拖延旧导入，不代表规则分析本身耗时 8 秒。

新导入断言 ComfyUI client、LLM provider 不得被调用；列表/详情也断言不得重新 analyze。隔离服务 8011 的一次三字段 JSON 导入为 201 / **20.52ms**，validation=null、bindings={}。该值仅为本机小样本，不作为任意工作流性能保证。

### 自动化回归

- 四类 capability 的 AI 建议经已有 HTTP Provider JSON 通道返回并实际进入 deepcopy patch；自定义节点/字段无需注册识别规则，模板与节点链接保持。
- 未知节点/字段、链接覆盖、重复用途目标、文本/数值类型错误、未知角色、非法输出节点/媒体、非 duration 帧数转换、未给出帧数规则、能力冲突、额外输出字段均被拒绝；不存在规则回退。
- AI 接口只读，不拉 object_info；未配置、超时、上下文过大、敏感字段脱敏、部分建议、浏览器断开取消模型请求均覆盖。导入明确要求用途，旧 auto-bind 返回 410。

### 浏览器验收

在临时数据库的浏览器夹具中使用固定模型响应（无真实 LLM/ComfyUI 请求）：

1. 上传含 `custom.words`、`custom.strength`、`save.filename_prefix` 三个标量的 JSON，直接导入成功；未触发 analyze/validate。
2. 参数表恰好 3 行，固定 binding-row 为 0，文生图用途选项中的参考/起始/结束项目为 0。AI 建议确认前仍显示待配置，确认后保存并进入真实字段绑定。
3. 手动将 prompt 指向其他实际字段，并让 negative 占用 AI 建议目标，再应用 AI；两项手动选择保留。自定义 user owner 字段确认改为 prompt 后可保存，服务端 owner 为 ai，覆盖权限保留。
4. 慢 AI 夹具等待时，“直接导入”仍可点击；取消立即恢复操作，识别中导入后页面关闭识别等待且数据已保存。页面没有第二套固定参数卡片。截图：[实际字段表](evidence/workflow-actual-fields.png)。

正式本地服务无运行任务后重启 8000，健康检查 200；未配置导演模型时 AI 接口返回 409 / CONFIGURATION_REQUIRED，实测约 88ms，不阻塞导入。真实模型识别效果、真实渲染及远端 CI 未执行；本轮不将历史移动端或 C05 规则验收当作新界面的验收结果。

## C07 默认工作流设置修复（2026-09-10）

现场复现：用户导入的文生图与首尾帧视频均为 `binding_issues=[]`、`validation=null`，两个“设为默认”按钮均被禁用。原因是前端仍要求 `validation.valid`，而 C06 已将依赖检查从导入流程中分离；后端默认设置本来只要求本地绑定完整。

完整 `make check` 最终通过：**149 passed**、无跳过，pytest 46.01 秒；53 个 Python 文件 Ruff/格式、ESLint、TypeScript 和 Vite 构建通过。首次执行有 148 passed，新测试误将既有业务错误状态 400 写成 422；核对 `AppError` 契约后纠正断言，再执行完整门禁通过，未修改后端错误行为。

- 新增 7 项 API 回归：图像/视频分别覆盖未检查、检查失败、保存后检查失效；断言媒体与 capability 默认项持久化、另一媒体默认项保留、没有调用 ComfyUI/AI、没有试跑或改写检查结果。缺少绑定返回 400 / WORKFLOW_INVALID，设置保持原值。
- 隔离浏览器实际点击“保存并设为默认”，名称与默认选择一起保存；清空名称时 PATCH 被拒绝、默认项未变。缺失绑定点击后显示“默认工作流必须具备完整角色绑定”。视频无需依赖检查即可直接设置默认，整页重载后两个默认标记保留。
- 正式 8000 页面实际点击设置并通过 GET settings 核对：图像默认为 `5afaab0b-0f34-43de-871e-5689bcaadb95`，视频默认为 `e952a89f-1273-4be8-88c6-767bb8c2bd1f`，对应 capability 映射一致。截图：[默认工作流](evidence/workflow-default-selection.png)。
- 失败 Episode `a9c6390b-a1dd-4774-9c9d-c67141c68d56` 仍为 FAILED，图像/视频 ID 仍为旧 `default_image/default_video`；本轮未修改或重跑该短片。

本轮只验证默认选择和保存交互。依赖检查与实际生成仍需单独执行，未调用真实模型生成或远端 CI；没有将默认设置成功等同于工作流可运行。

## C08 短片工作流更换（2026-09-10）

完整 `make check` 通过：**165 passed**、无跳过，pytest 148.91 秒；54 个 Python 文件 Ruff/格式、ESLint、TypeScript、Vite 构建通过。新增 16 项测试，保留既有 149 项。最终前端默认填充改为点击时读取最新配置后，再次通过 lint/typecheck/build。

- API 检查图像/视频媒体类型、参考图 TEXT_TO_IMAGE、必需绑定与保留覆盖；未知 ID、能力不符、缺失绑定、已锁定覆盖均原子拒绝。活动状态、busy、QUEUED/RUNNING/UNKNOWN 作业、版本冲突均返回 409，不改写短片。
- 更换后旧计划/Bible、镜头/参考/成片与参数覆盖进入历史快照，当前状态为 DRAFT；原始创作参数、统计、旧作业和素材文件保持。相同 ID 不重置进度。上传素材覆盖在其工作流保留时继续授权，工作流换走后移除覆盖但保留文件；多次更换保留历史且不嵌套历史。
- 两个集成用例分别在完成 4 秒短片后更换相同图结构的 profile、切换到 2 秒上限的 I2V，再走完整生成与真实 FFmpeg。新作业使用新 ID 和 `binding:1:` step_key，参考图不复用旧缓存；旧作业快照和素材文件保持。I2V 重新分镜满足每镜 ≤2 秒且无 SHOT_END_FRAME，成片仍为 4 秒。
- 隔离浏览器从已完成短片打开更换表单，点击当前默认后保存；响应变为草稿、旧成片归档、没有自动渲染。整页重载后新选择保留，点击继续生成得到 **4.00 秒**夹具成片，历史记录仍在。
- 确认正式服务没有活动 Episode/Job 后重启 8000；健康检查 200，OpenAPI 已注册更换接口。原失败短片实际显示旧绑定，当前默认填充为用户导入的图像/视频 ID，保存按钮可用；随后取消，原计划与 FAILED 状态未修改。1280px 视口无横向溢出。截图：[更换工作流表单](evidence/episode-workflow-rebinding.png)。

测试依赖为模拟 ComfyUI/模型和合成媒体，FFmpeg 实际执行；本轮未进行真实模型生成、移动端专项验收或远端 CI。原失败短片需用户保存新绑定后再继续生成，真实工作流依赖与效果仍需实际运行验证。

## C11 动态模型与视频时长（2026-09-11）

最终完整 `make check` 通过：**206 passed**、无跳过，pytest 51.15 秒；58 个 Python 文件 Ruff/格式、ESLint、TypeScript、Vite 构建通过。新增 12 项测试，原有 194 项保留，仅有既有两项依赖弃用提醒。

- DynamicCombo 读取选中分支的整数范围、步长、分辨率枚举和必填项；模型切换后重新检查约束，原模板未改动。模型下拉项为 key 字符串，修复选项对象被当作值的校验问题。
- 1/2.86/4/4.2/5 秒分别映射到 4/4/4/5/5 整数；离散枚举和浮点步长也受校验。工作流上限 4.8 秒时最大合法值为 4，不能把 4.2 向上扩成 5。非法高级覆盖、类型、布尔值及步长被拒绝；合法模型与 duration 覆盖真实进入最终 patched JSON，来源保持 user。
- 6 GiB / low 模式保留图片尺寸限制，云端视频允许按 workflow 生成 4 秒；同样约束的本地节点仍受 3 秒上限并在模型/图片调用前失败。
- 隔离流水线模拟未受理的视频失败，保留参考图、第一镜首尾帧与旧 3 秒预算，继续生成后复用原资产。时间线 `[2.86, 2.12, 1.88, 1.72, 1.42]` 保持不变，5 个视频请求均为 4 秒整数，真实 FFmpeg 导出 10 秒。依赖输出为合成夹具，不代表真实视觉质量。
- 只读查询 `10.0.1.10:8188/object_info/MinimaxHailuo03FirstLastFrameNode`，确认实际 H3 为整数 4～15 秒、step 1、api_node=true；对实际 Episode 的全部 5 镜执行本地 patch 和依赖校验，均为整数 4 秒且无问题，没有调用 `/prompt`。
- 确认无活动任务后更新正式 8000 服务，通过 `POST /workflows/e952a89f-1273-4be8-88c6-767bb8c2bd1f/validate` 刷新实际字段：3 种模型选项、duration min=4/max=15/step=1，3 类节点依赖检查通过。原 Episode 仍为 FAILED，updated_at 保持 `2026-09-10T15:47:01.381037+00:00`，首尾帧 ID 保留；可点击“继续生成”。本次未执行真实视频渲染、真实视觉 QA、浏览器操作或远端 CI。

## C10 模型超时在线配置（2026-09-10）

完整 `make check` 通过：**194 passed**、无跳过，pytest 49.13 秒；56 个 Python 文件 Ruff/格式、ESLint、TypeScript、Vite 构建通过。新增 7 项回归，原有测试全部通过，仅有既有两项依赖弃用提醒。

- 回归验证默认 600 秒、旧配置兼容、900 秒在线生效与重启恢复；检查真实 Provider 经 MockTransport 构造的 HTTP 连接/读写/连接池超时，以及模型测试返回值使用同一配置。合法边界 1/3600 秒可保存，越界、null、非有限值返回 422 且不修改配置。
- 使用短模拟截止验证测试超时、错误提示与临时图清理；没有实际等待 600 秒。隔离浏览器确认默认 600、改为 900 后保存/刷新仍为 900、提示同步，0 和 3601 均被输入范围校验拦截。
- 确认正式服务没有活动任务后重启；API 与设置页均显示 `llm_timeout=600`，已有模型/密钥配置保留。截图：[模型超时设置](evidence/model-timeout-settings.png)。本次没有调用真实模型、重跑失败短片或检查远端 CI。

## C09 模型测试与错误诊断（2026-09-10）

最终完整 `make check` 通过：**187 passed**、无跳过，pytest 63.30 秒；56 个 Python 文件 Ruff/格式、ESLint、TypeScript、Vite 构建通过。新增 22 项回归，既有 165 项全部保留；仅有既有 Starlette/httpx、AnyIO 弃用提醒。

- 22 项新增回归：导演/视觉分别选择已保存模型与 Bearer 密钥，实际构造 Chat Completions JSON 请求；视觉传入可解码的 32×32 蓝色 JPEG。响应不含密钥，测试不新增或修改 settings/workflow/episode/job/asset。
- HTTP 401/403/404/429/400/422/503 与 Connect/Read/Write/Pool timeout、ConnectError、RemoteProtocolError 分类验证；上游报错和异常中嵌入测试密钥，断言响应不泄露原文。
- 无效 JSON、错误字段、错误色块识别及畸形响应外壳均失败，不误报成功；缺少模型和非法请求不发起请求。短截止时间与浏览器断开均取消等待并清理临时图片。
- 隔离浏览器测试导演和视觉成功；修改模型立即清除旧结果并提示先保存。`TEST-AUTH-FAIL` 显示鉴权失败/HTTP 401，`TEST-SLOW` 等待中取消按钮可用，取消后按钮恢复；再次等待时编辑配置也会取消旧请求，不回填旧结果。
- 正式服务重启后，在设置页使用现有端点 `https://open.bigmodel.cn/api/paas/v4` 和 `glm-5.3` 实际点击导演测试，**7.82 秒**收到符合 schema 的 JSON。截图：[真实模型测试](evidence/model-test-live.png)。视觉模型为空，按钮禁用并提供提示；没有改动端点、密钥或模型配置。
- 原失败短片 `43a9ba95-46cb-4d97-b836-051c42de9c17` 仍为 FAILED，updated_at 仍为 `2026-09-10T15:09:22.206427+00:00`，渲染作业仍为 0。

真实导演测试仅证明当次最小请求可用，不证明此前失败是何种网络异常，也不代表较大 Visual Bible 请求、真实视觉模型、ComfyUI 生成、移动端或远端 CI 已验收。

## C12 工作流配置可用性（2026-09-11）

完整 `make check` 通过：**222 passed**、无跳过，pytest 52.26 秒；59 个 Python 文件 Ruff/格式、前端 ESLint、TypeScript 与 Vite 构建通过。新增 16 项测试覆盖 AUTOGROW 命名/前缀槽位、最少输入数、缺失/非法槽名、普通必填/链接索引保留，以及 API 节点元数据分类、未检查兼容和列表/详情持久化。最后补充保存错误自动定位后再次通过前端 lint/typecheck/build。

- 临时数据库的浏览器夹具：导入未带标签的视频图，直接选择 prompt、start_frame、end_frame、duration 及输出，设置 `duration_to_frames` 后保存并检查通过；不存在第二套固定参数值，已占用字段禁选。
- 搜索 `sampler` 定位 6 个实际字段，steps 从 30 改为 24 保存；非法 24.5 被后端类型校验拒绝。从其他页签保存非法值时自动跳到参数页并定位 `sampler.steps`。另将 steps 改为 26 并检查（schema default 随之变为 26），再恢复导入值、保存和整页重载，实际显示 30，`parameter_values` 为空。取消修改有效。设置默认并整页重新加载，默认标记保留，未要求先通过依赖检查。
- 注入错误的 `latent.width`，检查页明确显示字段及错误；点击“去修改此参数”自动打开参数页并搜索该字段，改为 512 后保存/检查通过。文生图只显示实际绑定，无首尾帧必填项。
- 1280px 桌面与 390×844 窄屏检查完成，窄屏 `scrollWidth=innerWidth=390`，保存按钮可见。截图：[直接绑定](evidence/workflow-config-bindings.png)、[窄屏配置](evidence/workflow-config-mobile.png)。正式页面搜索“模型”显示当前 MiniMax 工作流中的 6 个实际字段，包括 VAE、扩散模型、CLIP 和 LoRA 的 ComfyUI 枚举。
- 确认无活动 Episode/Job 后重启正式 8000，健康检查 200。依次刷新两个实际 workflow 的节点元数据：`首尾帧MiniMax` 为 local、api_nodes 为空，`values.a` 不再误报缺少 `values`；剩下 4 个必需用途未绑定及两个素材用途问题。旧 `按首尾帧生成视频` 为 cloud，准确列出 node 28 `MinimaxHailuo03FirstLastFrameNode`。
- 上述检查前后实际绑定、模板参数覆盖及两个默认 workflow ID 保持原值。原 Episode `43a9ba95-46cb-4d97-b836-051c42de9c17` 仍为 FAILED，更新时间保持 `2026-09-10T16:11:22.758940+00:00`。没有自动提交渲染或配置账号。

浏览器交互使用模拟 ComfyUI/模型夹具；完整测试包含真实 FFmpeg，但本轮没有执行真实模型生成、云端认证、真实视觉 QA 或远端 CI。local 只表示所检查节点元数据未声明 ComfyUI 云端 API，不能作为第三方节点完全不联网的证明。切换工作流的未保存确认已实现，未单独验收原生确认框。


## C13 导演时间约束与均衡分镜（2026-09-11）

最终完整 `make check` 通过：**252 passed**、无跳过，pytest 75.47 秒；60 个 Python 文件 Ruff/格式、ESLint、TypeScript 与 Vite 构建通过。新增 30 项回归。严格动态 schema 引入后，既有夹具作了两项必要适配：用精确十进制比例避免 `13.8 / 12` 得到 `1.1500000000000001`；慢导演夹具改为识别 EpisodePlan 子类，继续验证取消时禁止再次入队/删除，且不产生计划和作业。首次完整检查因后者失败（251 passed / 1 failed），适配后全量通过，未放松生产时间范围或删除原断言。

- 分配验证：4×2.5 秒及已合法的不等分配保持；比例缩放、同时触碰上下限、镜头换序、精确上下界、分数和余数均满足百分之一秒总和。均匀权重的误差至多 0.01 秒，输入 plan 不修改。
- 10 秒 / 3 秒上限的 Agent system 提示词、context 和 schema 一致：每镜 2.5～3 秒、必须 4 镜、建议 2.5 秒。1/1.5 秒短片、3.01 秒边界和 2 秒工作流上限按可行区间降低偏好，固定 1 秒覆盖可以规划 5×1 秒；60/90/600 秒分批上下文、连续性和总镜头限制保持。
- 实际 LLMProvider 经 MockTransport 构造 HTTP 消息，验证动态 JSON schema 和数字要求确实写入 system。6 个碎片、低于最短值或高于最长值的 JSON 被拒绝，第二次合法结果通过；连续两次非法返回 LLM_INVALID_OUTPUT，不静默接受。
- API 流水线夹具中创建 10 秒 low 模式短片，得到 4×2.5 秒；4 次最终 ComfyUI JSON 均为 41 帧（16 FPS、4n+1），真实 FFmpeg 成片 10 秒。另将旧 6 镜 `[2.21,1.95,1.57,1.65,1.4,1.22]` 写入失败短片，禁止再次调用 Director.plan 后继续生成成功，镜头 ID、plan 和时长保持，真实 FFmpeg 仍导出 10 秒。
- 用户原短片已于 `2026-09-10T17:23:29.917989+00:00` 完成，原计划仍为 6 镜。重启前 API 确认两条短片分别 COMPLETED/FAILED、活动作业为空；新打开的浏览器页也确认完成及六镜通过。此前自动审批引用旧页面而拒绝过重启，补充最新页面证据后同一操作获准。正式 8000 重启后健康检查 200，原 Episode 完整 JSON SHA256 与重启前相同，未触发重拍。

本轮没有前端改动。模型响应与 ComfyUI 渲染是隔离夹具，FFmpeg 实际执行；用户旧片的真实完成不算新策略的真实模型验收。未调用真实导演模型、重新提交真实生成、验收新策略视觉效果或检查远端 CI。
