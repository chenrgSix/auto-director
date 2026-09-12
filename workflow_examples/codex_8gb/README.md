# codex · 8GB 工作流包

为 AutoDirector 的四种能力准备的显式绑定示例。模型名按 2026-09-12 实际 ComfyUI 0.35.1 的已安装清单填写；不会随应用启动自动导入，也不会替换已有默认项。

| 工作流 | 用途 | 模板设置 |
| --- | --- | --- |
| codex-ZImage-快速文生图-8GB | 角色/场景参考图、无参考关键帧 | INT8、640×384、8 步、batch 1 |
| codex-H3-图生图-8GB | 基于已有画面的局部变化 | FL2VA、640×384、5 帧短帧组取最后一帧、12 步 |
| codex-H3-首帧视频-Turbo8-8GB | 由一张起始画面生成运动 | INT8 + 匹配 Turbo LoRA、640×384、8 步 |
| codex-H3-首尾帧视频-Turbo8-8GB | 有明确起止状态的镜头 | 同上，另绑定尾帧 |

视频每镜上限先设 5 秒，24 FPS，帧数按 `17n+5` 向上对齐；5 秒生成 124 帧，约 5.17 秒，AutoDirector 按计划裁剪。短片总时长仍由多镜拼接决定。尺寸由系统按比例与显存预算填写，并满足节点的 32 倍数要求。模板以 8GB 为目标，不代表任何尺寸/时长/其他进程占用下都能避免 OOM。

## 导入与绑定

- `*.api.json` 是 ComfyUI API 图，可在 ComfyUI 加载；直接导入 AutoDirector 时仍需填写绑定。
- `*.profile.json` 是完整 AutoDirector 导入请求，包含 API 图、能力、角色绑定、参数 owner 与覆盖规则。通过 `POST /api/v1/workflows/import` 导入，再调用 `POST /api/v1/workflows/{id}/validate` 检查本机依赖。
- 例如：`curl -H 'Content-Type: application/json' --data-binary @workflow_examples/codex_8gb/codex_zimage_t2i.profile.json http://127.0.0.1:8000/api/v1/workflows/import`。重复导入会创建新条目，操作前检查现有名称。
- 图片素材由 AutoDirector 自动上传；独立在 ComfyUI 试跑时，将 `codex_reference.png`、`codex_start.png`、`codex_end.png` 换成实际图片。
- 所有绑定字段允许高级覆盖；视频 FPS 必须与 24 FPS 原生时钟一致。仅绑定真实存在的字段，不添加无效 negative、CFG 或批量输入。

## 选择与限制

文生图作为「参考图工作流」。图生图作为「关键帧工作流」时保留输入画面结构，适合小幅改动；大幅换景或换姿态需要真实视觉验收。已安装的是 FL2VA 权重，故图生图使用匹配的 FL2VA 节点，不能把 REF2VA 节点与 FL2VA 剪枝权重混搭。取生成帧而非被锁定的第 0 帧。

两套视频默认只保存画面，省去音频 VAE 加载与解码；H3 内部仍联合采样音频 latent，不能视为移除了模型本身的音频开销。没有安装新模型、自定义节点或云端 API 节点。串行试跑，首轮包含模型加载成本。

依据：[ComfyUI 官方 H3 节点与模型指南](https://docs.comfy.org/tutorials/video/minimax/minimax-h3-native)、[官方帧数规则](https://github.com/Comfy-Org/embedded-docs/blob/main/comfyui_embedded_docs/docs/MiniMaxH3ImageToVideo/en.md)、[Image Studio 作者说明](https://github.com/astropuzzo/ComfyUI-MiniMax-H3-Image-Studio)。实际验证结果见 `docs/ACCEPTANCE.md` 的 C47 记录。
