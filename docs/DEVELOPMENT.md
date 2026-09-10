# 开发与运行

## 当前阶段

2026-09-10 建立开发基线，代码实现尚在进行。以下环境和命令将在对应模块交付时补齐可复现结果；不要把本页计划视为已经启动成功。

## 环境契约

- Python 3.12+、uv；Node 22+、npm。
- FFmpeg 与 ffprobe，真实合成验收必须安装。
- 用户管理的 ComfyUI，默认 `http://127.0.0.1:8188`；不自动安装模型或 Custom Nodes。
- 独立 OpenAI-compatible LLM；可选支持图像输入的 VLM。密钥从环境读取，不提交。
- 本地数据放 `data/`；测试使用独立临时目录。

## 工作流准备

内置 profile 不包含模型权重。首次连接时读取 `/object_info` 和 `/system_stats`，检查缺失节点与模型枚举，用户修复或导入满足角色协议的替代工作流。图像和视频默认项独立可替换；普通 UI JSON 需要在 ComfyUI 导出 API Format。

## 官方技术参考

- [ComfyUI 服务路由](https://docs.comfy.org/development/comfyui-server/comms_routes)
- [ComfyUI WebSocket 消息](https://docs.comfy.org/development/comfyui-server/comms_messages)
- [官方工作流模板](https://github.com/Comfy-Org/workflow_templates)
- [FastAPI lifespan](https://fastapi.tiangolo.com/advanced/events/)

这些资料用于协议核对；项目需求以原始设计和本仓库决策为准。
