# 迭代记录

## 2026-09-10 · D01 文档基线

- 固定 P0 + Phase 1/2 的整体 MVP，分离 Phase 3 与真实视觉验收。
- 建立任务、模块、API、开发运行、验收、决策和索引文档。
- 实测环境：Python 3.14.6、Node 22.23.1、FFmpeg 8.1.1；默认 ComfyUI 尚未取得有效响应。
- 本迭代仅完成文档基线，尚无代码实现验收。

## 2026-09-10 · B01 存储与配置基础

- 建立 Python 3.12 uv 锁定环境、配置/结构化异常、SQLite WAL versioned 聚合仓储和资产路径保护。
- ComfyUI 地址只允许配置来源；默认 loopback/LAN，阻止 metadata/link-local，公网需显式开关。
- 验证：`backend/.venv/bin/pytest -q`（backend 目录）3 passed；Ruff 检查通过。
- 用户明确先完成项目，不在本机部署 ComfyUI；真实 API 接入待整体交付后。

## 2026-09-10 · W01–W03 工作流与 ComfyUI 协议

- API JSON 图校验、角色解析、动态 schema、模型/字段错误、不可变 patch、秒数到模型帧数转换。
- 实现 HTTP 上传/下载、prompt/history/WS 进度、超时与按 prompt_id 取消；未知提交不自动重发。
- 内置 SD1.5 文生图、Wan2.1 FLF2V API 模板及依赖说明，profile 可独立编辑和替换。
- 结构与客户端测试使用固定夹具；不安装 ComfyUI、不宣称真实模型兼容/质量验收通过。

## 2026-09-10 · A01 导演与模型适配

- 四个 schema 受限的 Agent；统一 JSON Provider、图像输入、token/call 统计、拒绝和无效输出分类。
- Director 强制 1 秒下限、workflow 上限与总时长；Bible/Shot 继承单集设定；QA 根据实际图像得分分流重试。
- 支持显存/质量模式预算，High 规划两个候选；模型未配置时返回配置错误。
- OpenAI-compatible 层采用 Chat Completions `json_object` 兼容模式 + 本地 Pydantic 强校验；不把 JSON mode 当远端 schema 保证。[官方 API](https://developers.openai.com/api/reference/resources/chat)

## 2026-09-10 · M01 本地媒体链路

- 资产按单集分区、服务端 UUID 命名并记录 SHA256/大小/媒体元数据；检查图片内容与扩展名。
- FFmpeg 参数数组调用、进程超时/取消清理、实际视频尾帧提取、尺寸/FPS/音轨规范化与 MP4 合成。
- 真实 FFmpeg 测试混合 320×180/180×320、16/24 FPS、有声/无声片段，验证 2 秒 H.264 成片、音轨和可解码尾帧。

## 2026-09-10 · G01/G02/API 单集流水线

- FastAPI 实现工作流库/试跑、设置检查、单集/镜头、进度 SSE、资产上传预览、成片下载与定向重试。
- 单 worker、持久化步骤、不可变 RenderJob 快照、未知提交核对/恢复；取消中的旧任务结束前禁止再次入队。
- 参考资产→首尾帧→VLM QA→视频→采样 QA→实际尾帧→FFmpeg；连续镜头依赖与时间线变化显式失效。
- 使用仅存在于 tests 的 ComfyUI/LLM 固定夹具及真实 FFmpeg，完成 5 秒双镜头生成、视频单独重试（关键帧和其他镜头保留）、工作流上传试跑、错误边界。
- 本地 27 项测试通过；依赖中有 Starlette/httpx 和 AnyIO 的弃用提醒，不影响当前结果；真实 ComfyUI/VLM 仍未验收。

## 2026-09-10 · U01 Web 创作工作台

- React/TypeScript/Vite 实现创作、短片库、Episode 时间线、工作流库/绑定/参数/试跑、连接与设置。
- 接入首尾帧/视频预览、成片下载、镜头重试、拖动/按钮排序与启用、UNKNOWN 作业核对和人工结论。
- 响应式深色布局、表单 label、禁用状态、明确错误提示；密钥不进入浏览器。
- `npm run build`、`npm run lint` 通过；浏览器核对创建页布局、时长切换、输入后按钮状态、工作流绑定与设置页，无应用控制台 error。
