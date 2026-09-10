# auto-director

## 本地运行

当前已实现文档中的 P0 + Phase 1/2 MVP：工作流库、导演与质检 Agent、单集生成流水线、连续性/重试、FFmpeg 导出和 Web 工作台。真实 ComfyUI/LLM/VLM 接入与视觉验收待用户提供服务；本项目不自动部署 ComfyUI 或下载模型。

准备 Python 3.12、uv、Node 22、FFmpeg/ffprobe，在仓库根目录运行：

```sh
make install
make build
make run
```

打开 `http://127.0.0.1:8000`。在“连接与设置”填写 ComfyUI 地址、模型端点、模型名和密钥，保存后立即生效，无需编辑 `.env` 或重启。开发时分别运行 `make dev-api`、`make dev-web`；`make check` 执行本地完整检查。

## 项目文档

- [开发与运行](docs/DEVELOPMENT.md)：配置、启动、工作流准备和恢复。
- [开发任务](docs/DEVELOPMENT_TASKS.md)、[模块设计](docs/MODULE_DESIGN.md)、[接口契约](docs/API_CONTRACT.md)。
- [验收记录](docs/ACCEPTANCE.md)：区分模拟依赖、真实 FFmpeg、浏览器和真实模型门禁。
- [文档索引与维护规则](docs/INDEX.md)、[内置工作流依赖](bundled_workflows/README.md)。
