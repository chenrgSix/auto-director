# H3 Ref2VA 多参考视频

在项目工作流页导入 `h3_ref2va.profile.json`，作为可选的 IMAGE_TO_VIDEO 工作流。`h3_ref2va.api.json` 可供 ComfyUI API 使用。生成器：`backend/.venv/bin/python scripts/build_ref2va_workflow.py`。

模型配套：`minimax_h3_ref2va_pruned_int8_convrot.safetensors` + `minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors`；4 步 Euler/simple，video/audio shift 为 12/3。复用 Qwen3-VL NVFP4、视频 VAE 与音频 VAE，视频分块解码；24 FPS，单次规划上限 5 秒。

首帧通过 `MiniMaxH3AddGuide` 在第 0 帧锚定，独立于 R2V 参考图；因此保持项目 IMAGE_TO_VIDEO 的首帧契约。`reference_image` 必需，`reference_image_2`～`reference_image_9` 可选，按连续顺序映射 `<Picture 1>`～`<Picture 9>`。不用的加载器在实际提交前移除。视频和原生音频来自同一次采样。

原生 R2V 的提示词不追加 FL2VA 的首尾图片时间头，避免把 `<Picture 1>` 的人物参考误说成视频第 0 帧；首帧位置由独立 Guide 控制。已有 FL2VA 工作流仍使用原来的首尾时间说明。

镜头参考顺序沿用 `visual_continuity.reference_roles`。建议先选人物、环境和道具三张，搭配支持同样参考数量的图像工作流。画面起点沿用已确认的关键帧，人物/场景/道具素材参与视频采样；不将仅提供人物参考等同于严格锁定身份。

与用户 `60秒.json` 的区别：本配置使用 ComfyUI 原生节点执行单镜，避免导演台内部接口调用的版本兼容问题；不把多段时间轴装作单镜时长输入。多段运动/音频接续属于独立实测，整组任务、创作契约和组级重跑尚未接入 AutoDirector。

来源：[ComfyUI 官方 H3 多帧示例](https://github.com/Comfy-Org/workflow_templates/blob/main/templates/video_minimax_h3_multiframe_reference.json)、[Turbo 作者参数](https://github.com/ModelTC/Minimax-H3-Turbo#1-model-specs)。真实验收见项目 [C60](../../docs/DEVELOPMENT_TASKS.md)。

## 两段运动上下文示例

`motion_context_2segment.api.json` 是独立 ComfyUI API 工作流，依赖已安装的 `ComfyUI-H3-Motion-Context`。四个 LoadImage 分别选择首帧、人物、环境和道具，替换 `C60_*.png` 占位名；本例固定使用三张参考图。它不作为 AutoDirector 的单镜 profile 导入。

第一段生成 124 帧；第二段用第一段采样输出的 AV latent 继承末尾 22 帧画面与 24 帧时间长度的音频上下文。第二段生成 158 帧，裁掉 22 帧上下文前缀，保留 136 帧新画面。两段音轨分别对齐实际帧数，最终拼接 260 帧，24 FPS 下约 10.833 秒。导出包含第一段、第二段和完整拼接三个 MP4。前缀裁剪只针对重复上下文，不按剧本计划时长截掉有效内容。

节点各自管理条件、采样、解码和音视频拼接，避免调用导演台内置的旧版 conditioning 接口。该示例用于验证真实接续能力；自动镜头组编排、缓存持久化和组级局部重跑仍未实现。
