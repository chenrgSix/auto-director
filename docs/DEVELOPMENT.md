# 开发与运行

## 本地启动

要求 Python 3.12、uv、Node 22、npm，以及 PATH 中可用的 FFmpeg/ffprobe。当前验证环境为 macOS arm64、Python 3.12.11、Node 22.23.1、FFmpeg 8.1.1。依赖分别锁定在 `backend/uv.lock` 和 `frontend/package-lock.json`。

在仓库根目录执行：

```sh
make install
cp -n .env.example .env
make build
make run
```

访问 `http://127.0.0.1:8000`；API 交互文档为 `/docs`，健康检查为 `/api/v1/health`。已有 `.env` 请直接编辑，不覆盖。未配置 ComfyUI/LLM 也可以浏览工作台、导入和编辑工作流；生成时会报告依赖错误。

开发时分别在两个终端运行 `make dev-api`、`make dev-web`，访问 `http://127.0.0.1:5173`；Vite 将 `/api` 转发至 8000。后端 reload 会中断任务，正在生成时使用 `make run`。只启动一个 API 进程，不加多个 uvicorn workers。

## 模型与连接配置

按用户要求，本项目不在本机部署 ComfyUI，也不安装模型权重。工程和本地验证交付后再接入用户提供的服务。

| 环境变量 | 用途 |
| --- | --- |
| `AD_COMFYUI_URL` | 官方 ComfyUI HTTP 服务根地址，默认 `http://127.0.0.1:8188`；也可在设置页保存 |
| `AD_LLM_BASE_URL` | OpenAI-compatible API 根地址，包含 `/v1`，默认 `http://127.0.0.1:11434/v1` |
| `AD_LLM_MODEL` | 导演/Bible/Shot 模型，必须支持 Chat Completions JSON mode |
| `AD_LLM_API_KEY` | 模型服务密钥，只在本机环境或 `.env` 配置，不返回浏览器 |
| `AD_VLM_MODEL` | 同一模型端点的视觉模型，需支持 `image_url` 输入；为空则明确跳过视觉 QA |
| `AD_DATA_DIR` | SQLite 与资产根目录，默认仓库 `data/`；相对路径以仓库为基准 |
| `AD_RENDER_TIMEOUT` | 单个 ComfyUI 作业等待上限，默认 1800 秒 |
| `AD_MAX_ASSET_MB` | 单资产上限，默认 512 MiB |
| `AD_ALLOW_PUBLIC_COMFYUI` | 默认 false，仅允许 loopback/LAN；公网地址需要显式 true |

修改环境变量后重启 API。设置页保存的 ComfyUI 地址优先于环境变量；有进行中或 UNKNOWN 作业时不能切换。当前适配器直连官方 HTTP/WS，不含厂商云认证、代理登录或多租户支持。

## 工作流准备与首个生成

1. 在“连接与设置”保存 ComfyUI 地址并检查设备、节点及模型。
2. 打开“工作流”：选择内置模板或导入 **API Format JSON**；普通 UI JSON 需要先在 ComfyUI 导出 API 格式。
3. 确认语义绑定、输出节点、capabilities 和模型枚举；设置 `duration_to_frames` 时检查 FPS 与帧数对齐规则。上传首尾帧，分别验证并试跑两类 profile，再设置默认项。
4. 在“开始创作”输入 Idea，选择 5/10/15 秒、画幅和质量。单集页展示参考、镜头、QA、作业和成片；视频单独重试会复用关键帧，排序/禁用变化会失效相关镜头及旧成片。

内置 SD1.5 和 Wan2.1 FLF2V 的具体依赖见 [工作流说明](../bundled_workflows/README.md)。默认 SD1.5 没有视觉 reference 输入；要进行角色身份一致性的真实验收，应替换为支持参考图的工作流。High 在启用 VLM 时比较两个首帧候选；VLM 缺失时不宣称完成视觉筛选。

## 验证与浏览器夹具

```sh
make check                         # Ruff、格式、ESLint、类型、构建、pytest
make test                          # 后端与真实 FFmpeg；ComfyUI/LLM 使用隔离夹具
make browser-fixture               # 8011 端口，合成测试媒体，关闭后清理临时数据
```

浏览器夹具只用于复现 [验收记录](ACCEPTANCE.md) 的交互步骤，不是生产启动方式。没有自动运行真实模型的测试；远端 CI 配置见 `.github/workflows/checks.yml`，当前执行证据以验收表为准。

## 失败恢复与数据

`data/autodirector.sqlite3` 保存版本化聚合、作业快照和 QA 历史；`data/assets/<episode_id>/` 保存 UUID 命名媒体。备份前停止 API，整体复制数据目录；恢复时保留数据库和对应媒体，不单独复制运行中的 SQLite 主文件。

取消会尽快停止自身 ComfyUI 作业；正在执行的 LLM/媒体步骤可能需要结束或超时后退出，退出前禁止重新入队。重启保留已生成资产，将中断作业标记为可诊断失败或 UNKNOWN。先在单集/设置页核对队列和历史：已知 prompt_id 可以恢复读取，未知受理状态不能直接重发。只有人工查明后，才填写明确结论解除 UNKNOWN。

删除单集默认保留媒体；API 的 `delete_assets=true` 才同时清理资产。没有后台自动删盘或过期清理任务。

## 官方技术参考

- [ComfyUI HTTP 路由](https://docs.comfy.org/development/comfyui-server/comms_routes)、[WebSocket 消息](https://docs.comfy.org/development/comfyui-server/comms_messages)
- [Wan 首尾帧官方教程](https://docs.comfy.org/tutorials/video/wan/wan-flf)、[官方服务端源码](https://github.com/Comfy-Org/ComfyUI/blob/master/server.py)
- [Chat Completions API](https://developers.openai.com/api/reference/resources/chat)

这些资料用于协议核对；产品范围以原始设计和 [设计决策](DECISIONS.md) 为准。
