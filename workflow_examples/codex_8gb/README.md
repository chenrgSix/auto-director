# codex · 8GB 工作流包

为 AutoDirector 的四种能力准备的显式绑定示例。模型名按 2026-09-12 实际 ComfyUI 0.35.1 的已安装清单填写；不会随应用启动自动导入，也不会替换已有默认项。

| 工作流 | 用途 | 模板设置 |
| --- | --- | --- |
| codex-ZImage-快速文生图-8GB | 角色/场景参考图、无参考关键帧 | INT8、640×384、8 步、batch 1、整图解码 |
| codex-H3-图生图-8GB | 基于已有画面的局部变化 | FL2VA、640×384、5 帧短帧组取最后一帧、12 步 |
| codex-H3-首帧视频-Turbo8-8GB | 由一张起始画面生成运动 | INT8 + 匹配 Turbo LoRA、640×384、8 步、Comfy Kitchen attention |
| codex-H3-首尾帧视频-Turbo8-8GB | 有明确起止状态的镜头 | 同上，另绑定尾帧 |

视频每镜上限先设 5 秒，24 FPS，帧数按 `17n+5` 向上对齐；5 秒生成 124 帧，约 5.17 秒，AutoDirector 按计划裁剪。短片总时长仍由多镜拼接决定。尺寸由系统按比例与显存预算填写，并满足节点的 32 倍数要求。模板以 8GB 为目标，不代表任何尺寸/时长/其他进程占用下都能避免 OOM。

## 导入与绑定

- `*.comfy.json` 是 ComfyUI 原生画布文件，可拖入 ComfyUI 查看和编辑节点。用户服务器的「工作流」列表已保存四份同名 `codex-` 文件；列表未刷新时重新加载页面。
- `*.api.json` 是执行用 API 图；直接导入 AutoDirector 时仍需填写绑定。
- `*.profile.json` 是完整 AutoDirector 导入请求，包含 API 图、能力、角色绑定、参数 owner 与覆盖规则。通过 `POST /api/v1/workflows/import` 导入，再调用 `POST /api/v1/workflows/{id}/validate` 检查本机依赖。
- 例如：`curl -H 'Content-Type: application/json' --data-binary @workflow_examples/codex_8gb/codex_zimage_t2i.profile.json http://127.0.0.1:8000/api/v1/workflows/import`。重复导入会创建新条目，操作前检查现有名称。
- 图片素材由 AutoDirector 自动上传；独立在 ComfyUI 试跑时，将 `codex_reference.png`、`codex_start.png`、`codex_end.png` 换成实际图片。
- 所有绑定字段允许高级覆盖；视频 FPS 必须与 24 FPS 原生时钟一致。仅绑定真实存在的字段，不添加无效 negative、CFG 或批量输入。

## C48 实测优化

在 RTX 5060 8GB 上固定素材、提示词、种子、640×384、24 FPS、124 帧与 8 步，逐节点比较执行时间：

| 路径 | 原配置 → 优化配置 | 保留的修改 |
| --- | --- | --- |
| H3 首尾帧视频采样 | 73.94 → 53.14 秒，约减少 28% | `LoRA → ModelAttentionBackend → BasicGuider → SamplerCustomAdvanced`，选择 `comfy kitchen attention` |
| Z-Image 图片解码 | 热运行约 0.56 → 0.13 秒 | `VAEDecodeTiled → VAEDecode`，采样步数不变 |
| H3 图生图采样 | 基础 12 步 16.57 秒；Turbo 8 步 16.66 秒 | 保留基础 12 步，不把条件缓存命中算成 Turbo 提速 |

首帧视频的新路径也完成真实生成，采样 53.46 秒。视频继续使用 256 空间块 / 32 时间块解码；扩大块、分头注意力、分块前馈及 LoRA Bypass 没有足够稳定收益，未放入最终图。工作流没有缩短视频、减少视频步数或降低对比尺寸。

视频加速使用已安装的 ComfyUI 原生 `ModelAttentionBackend`，只作用于该图的模型副本。需要对照效果时，在 ComfyUI 的「8GB 注意力加速」节点，或 AutoDirector 参数搜索 `attention`，切回 `pytorch attention`。该后端在官方代码中标记为实验性，输出可能有数值差异，不能承诺逐像素相同；当前船只样例没有观察到黑帧或明显画面崩坏。

耗时来自单机样例的节点事件，不是通用性能保证；冷启动、加载及缓存会改变总耗时，显存采样也不是精确进程峰值。完整设置、被否决的方案、试跑记录和限制见 `docs/ACCEPTANCE.md` 的 C48 记录。

## 选择与限制

文生图作为「参考图工作流」。图生图作为「关键帧工作流」时保留输入画面结构，适合小幅改动；大幅换景或换姿态需要真实视觉验收。已安装的是 FL2VA 权重，故图生图使用匹配的 FL2VA 节点，不能把 REF2VA 节点与 FL2VA 剪枝权重混搭。取生成帧而非被锁定的第 0 帧。

两套视频从 C50 起保存原生声音：同一次 `SamplerCustomAdvanced` 的联合 latent 分别送入视频与音频解码，`VAEDecodeAudio` 使用已安装的 `minimax_h3_audio_vae_fp32.safetensors`，输出连接 `CreateVideo.audio` 后保存 MP4。仍为一次 8 步采样；增加的加载、解码和显存换入换出开销以 C50 实测为准。

视频提示词可描述环境声、动作声或对白；是否出现以及听感由模型决定，未配置独立 TTS，旁白脚本不会自动朗读。拼接沿用片段音轨，只有无音轨片段才补静音。更新工作流不会给旧 MP4 自动补声，也不会删除或重跑旧成片。没有安装新模型、自定义节点或云端 API 节点。

依据：[ComfyUI 官方 H3 节点与模型指南](https://docs.comfy.org/tutorials/video/minimax/minimax-h3-native)、[官方帧数规则](https://github.com/Comfy-Org/embedded-docs/blob/main/comfyui_embedded_docs/docs/MiniMaxH3ImageToVideo/en.md)、[原生 attention 实现](https://github.com/Comfy-Org/ComfyUI/blob/master/comfy_extras/nodes_model_advanced.py)、[Image Studio 作者说明](https://github.com/astropuzzo/ComfyUI-MiniMax-H3-Image-Studio)。初次适配验收保留在 C47，本次计算路径优化记录在 C48。
