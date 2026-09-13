# H3 Ref2VA 多参考视频

在项目工作流页导入 `h3_ref2va.profile.json`，作为可选的 IMAGE_TO_VIDEO 工作流。`h3_ref2va.api.json` 可供 ComfyUI API 使用。生成器：`backend/.venv/bin/python scripts/build_ref2va_workflow.py`。

模型配套：`minimax_h3_ref2va_pruned_int8_convrot.safetensors` + `minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors`；4 步 Euler/simple，video/audio shift 为 12/3。复用 Qwen3-VL NVFP4、视频 VAE 与音频 VAE，视频分块解码；24 FPS，单次规划上限 5 秒。

首帧通过 `MiniMaxH3AddGuide` 在第 0 帧锚定，独立于 R2V 参考图；因此保持项目 IMAGE_TO_VIDEO 的首帧契约。`reference_image` 必需，`reference_image_2`～`reference_image_9` 可选，按连续顺序映射 `<Picture 1>`～`<Picture 9>`。不用的加载器在实际提交前移除。视频和原生音频来自同一次采样。

镜头参考顺序沿用 `visual_continuity.reference_roles`。建议先选人物、环境和道具三张，搭配支持同样参考数量的图像工作流。画面起点沿用已确认的关键帧，人物/场景/道具素材参与视频采样；不将仅提供人物参考等同于严格锁定身份。

与用户 `60秒.json` 的区别：本配置使用 ComfyUI 原生节点执行单镜，避免导演台内部接口调用的版本兼容问题；不把多段时间轴装作单镜时长输入。多段运动/音频接续属于独立实测，整组任务、创作契约和组级重跑尚未接入 AutoDirector。

来源：[ComfyUI 官方 H3 多帧示例](https://github.com/Comfy-Org/workflow_templates/blob/main/templates/video_minimax_h3_multiframe_reference.json)、[Turbo 作者参数](https://github.com/ModelTC/Minimax-H3-Turbo#1-model-specs)。真实验收见项目 [C60](../../docs/DEVELOPMENT_TASKS.md)。
