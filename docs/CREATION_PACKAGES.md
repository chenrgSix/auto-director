# C53 创作包与 MCP

## 冻结目标（2026-09-13）

用户在自己的 Codex 持续会话中统筹故事、设定、分镜与提示词；AutoDirector 提供制作约束、版本化成果交付、校验、预览确认、制作与反馈。完整创作包路径不调用文字模型 API。页面导入导出与 MCP 使用同一契约和业务服务。

AutoDirector 是本地 MCP 服务端，Codex 是客户端。服务不启动、恢复或占用 Codex 会话，不读取 Codex 登录凭据或会话数据库。MCP 连接生命周期与已受理制作分离；用户查看、打断、结束 Codex 时，AutoDirector 继续可用。“查看会话读锁”尚未复现，不作为已确认缺陷。

## 第一期范围

1. 独立创作项目，持久化用户需求、工作流选择、不可变创作包版本与制作交付记录。
2. JSON 包导入、导出、草稿保存、版本冲突处理；同一字段只有一份事实源。
3. 导出制作约束/schema，校验身份、时长、能力、提示词、声音格式与 AI owner 参数。草稿允许缺项，通过完整校验的版本才可提交预览。
4. 提交创建独立 Episode 制作快照，进入待确认分镜；不自动调用导演、审稿、Bible 或 Shot Agent。确认后沿用 ComfyUI、媒体校验、取消和 UNKNOWN 恢复。视觉 QA 明确选择现有模型或人工复核。
5. 本地 Streamable HTTP MCP 提供上下文、版本读写、校验、提交预览、确认制作、状态/素材反馈和取消工具。
6. 修改携带基础版本；提交和确认携带幂等请求身份，重复请求不得重复制作。长任务受理后立即返回身份，由后台执行；断线不隐式取消。
7. 页面提供工作台、导入导出、校验、历史版本和制作入口，保留原有 API 创作流程。

## 交付边界

创作项目后续修改不改写已交付 Episode。预览中的人工修改继续受 Episode 版本保护；制作快照标明原创作包版本。文件与 MCP 提交同一格式。创作记录保存关键决定、待解决问题和修改摘要，以便上下文压缩或更换会话后恢复。

工作流约束来自服务器，包不能自称校验通过。提交重新检查当前配置；过期配置要求重新获取上下文。草稿保存不要求 ComfyUI 在线，实际制作仍执行实时预检。包不包含运行状态、密钥或任意资产路径。

MCP 不负责唤醒 Codex；首版提供用户会话的连接说明。自动启动/接管 Codex、多人云服务、账号管理、任意文件访问和自动视觉审美验收不在本期。

## 验收门禁

- 无文字模型配置，完整包经过导入、预览、确认、FakeComfy 与真 FFmpeg 合成；断言零文字模型调用。
- 草稿缺项可保存；非法镜号、时长、参数和提示词阻断提交，错误可定位。
- CAS 冲突不覆盖；提交/确认丢失响应后重试复用身份；过期版本和配置被拒绝。
- 官方 MCP 客户端验证初始化、工具发现、读写、错误、重连及制作闭环。
- 页面/MCP 交替修改、导入导出、历史、预览及窄屏交互验收。
- 客户端断开时服务、版本与受理任务保持，显式取消可用。
- 完整 make check；真实 Codex 创作质量、真实模型渲染、CI 和生产验收单独记录。

状态：第一期本地交付完成，后端、MCP、网页与隔离验收通过；验证范围与限制见 [C53 验证记录](ACCEPTANCE.md#c53-分阶段验证)。

## 创作包与 HTTP 契约

格式标识 `autodirector.creation/v1`。`brief` 保存需求、时长、画幅、风格、质量、三类工作流选择、种子和 `visual_review=manual|model`（默认人工）；`title/logline/bible/shots` 保存创作成果。每镜继承 ShotPlan，额外提供跨版本稳定的 `id` 和可缺省的 `prompts`；`decisions/open_questions/notes` 保存创作上下文。完整字段取 `GET /api/v1/creation/schema`。包不存模型 Key 或运行时资产身份。

所有路径前缀为 `/api/v1/creation`：

| 路径 | 职责 |
| --- | --- |
| `GET /schema` / `POST /normalize` | 读取静态契约 / 校验文件结构并补齐缺省值，不保存 |
| `GET/POST /projects` | 项目列表/创建；创建携带 UUID request_id 与 document |
| `GET /projects/{id}` | 最新文档、版本列表和制作列表 |
| `GET /projects/{id}/context` | 包、当前制作约束/hash、动态 Bible/提示词 schema |
| `GET /projects/{id}/export?revision=N` | 导出指定不可变版本，省略 N 为最新 |
| `POST /projects/{id}/revisions` | document + expected_revision + request_id，原子保存新版本 |
| `POST /projects/{id}/validate` | 本地校验，返回 valid/issues/revision/constraints_hash |
| `POST /projects/{id}/submit` | expected_revision + constraints_hash + request_id，创建待确认 Episode |
| `GET /projects/{id}/productions/{episode_id}` | 状态、镜头、素材链接与作业反馈 |
| `POST /projects/{id}/productions/{episode_id}/confirm` | expected_version + request_id + confirm=true，确认当前预览并排队 |
| `POST /projects/{id}/productions/{episode_id}/cancel` | 显式取消此项目的制作 |

request_id 必须为 UUID；同一逻辑请求重试保持 ID 和内容，ID 对应不同内容返回 IDEMPOTENCY_CONFLICT。版本与提交收据在一个 SQLite 事务保存；确认收据与 QUEUED 状态原子保存，启动恢复既有队列。提交只准备预览，不请求 ComfyUI；制作时再执行实时依赖和媒体检查。外部创作拒绝内部“重新生成提示词”，需提交包新版本。

`context` 与 `export` 支持 `download=true`，返回 UTF-8 JSON 附件及带实际版本号的文件名；省略时保持 JSON API 响应。网页的已保存包、历史版本和上下文使用直接附件链接，编辑中的草稿仍从本页导出并可复制 JSON。

## 网页工作台

侧栏“创作包”支持新建需求草稿、导入为新项目、载入修改后的包、保存新版本、导出指定历史与复制当前 JSON。编辑中的版本发生冲突时保留本页内容，禁止覆盖；先导出/复制草稿，再通过页内确认载入最新版本并合并。高级 JSON 编辑须先应用到编辑区再保存，未应用的内容不会被轮询覆盖。

保存后点击“校验创作包”，通过后“提交分镜预览”；此时仍未开始渲染。在短片页面确认当前分镜才排队制作，网页确认与 MCP 确认共用版本/幂等契约。短片标明来源版本并可返回项目，人工预览调整仅影响该次制作快照。

默认由用户和 Codex 查看实际画面，基础媒体检查始终运行。若选择已配置视觉模型，模型配置缺失会阻止交付/确认或使任务明确失败，不静默更换复核方式。修改外部创作提示词请保存包新版本；原有关键帧/视频重跑、素材播放和生成诊断继续可用。

## 连接自己的 Codex

启动 AutoDirector 后，在 Codex 的 MCP 设置中添加 Streamable HTTP 服务，URL 为 `http://127.0.0.1:8000/mcp/`。本机 CLI 也可执行：

```sh
codex mcp add autodirector --url http://127.0.0.1:8000/mcp/
```

8000 是默认端口，改过端口则使用实际地址。上述配置由用户执行，本项目不代写 Codex 配置。服务只面向本机；继续遵守现有 Host/Origin 限制，无 API Key 或 Codex 凭据交换。连接方式参考 [Codex 官方 MCP 文档](https://learn.chatgpt.com/docs/extend/mcp)。

网页建立创作项目后，复制创作指令到自己的 Codex 会话。Codex 先读取 `get_creation_context`，根据实际 schema 创作并保存版本；校验通过后交付预览。用户可以在网页确认制作，也可以在自己的会话授权 Codex 确认该版本。未授权时只提交预览。

| MCP 工具 | 用途 |
| --- | --- |
| `list_creation_projects` / `create_creation_project` | 找到或建立项目 |
| `get_creation_context` | 最新包、制作约束/hash、动态输出 schema |
| `read_creation_package` / `save_creation_package` | 读取指定版本 / CAS 保存新版本 |
| `validate_creation_package` / `submit_creation_package` | 本地校验 / 交付待确认预览 |
| `list_creations_in_production` | 此项目已交付的预览和制作 |
| `confirm_production` | 携带当前 expected_version、UUID request_id、confirm=true 受理制作 |
| `get_production_feedback` / `cancel_production` | 查询镜头/作业/素材 / 显式取消制作 |
| `inspect_production_media` | 返回项目内图片或视频的五点抽帧，供 Codex 实际查看 |

读写工具调用同一 CreationService；工具失败返回 `isError` 与结构化错误，冲突后先读取并合并。协议层采用官方 Python SDK v1 维护线、无状态 HTTP；业务版本和幂等收据存于 SQLite，既有 GenerationService 持有后台任务。断开或重连 MCP 不持有、不释放 Codex 会话锁，也不取消制作。视频抽帧不能证明完整运动、对白或口型质量；声音仍需实际播放复核。
