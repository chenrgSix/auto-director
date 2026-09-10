# 内置工作流

仅包含 API JSON，不包含权重、ComfyUI 或 Custom Nodes。默认图像为 SD1.5 txt2img，默认视频为 Wan2.1 FLF2V FP8。用户可在工作流页面更换模型参数，或导入兼容替代项；缺模型/节点时校验会报告具体字段。

图像需要 `models/checkpoints/v1-5-pruned-emaonly.safetensors`（或更换为本机兼容 checkpoint）。视频需要：

- `diffusion_models/wan2.1_flf2v_720p_14B_fp8_e4m3fn.safetensors`
- `text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors`
- `vae/wan_2.1_vae.safetensors`
- `clip_vision/clip_vision_h.safetensors`

节点和模型依据：[官方 Wan FLF 教程](https://docs.comfy.org/tutorials/video/wan/wan-flf)、[官方 Wan 节点](https://github.com/Comfy-Org/ComfyUI/blob/master/comfy_extras/nodes_wan.py)、[官方视频节点](https://github.com/Comfy-Org/ComfyUI/blob/master/comfy_extras/nodes_video.py)。视频模板采用 2026-09-10 官方源码的 SaveVideo DynamicCombo 格式，需要对应的新版本 ComfyUI；运行前通过 object_info 验证，再执行真实试跑。

Wan 的 length 按 `ceil((duration × fps - 1) / 4) × 4 + 1` 写入，合成时裁切到时间线。默认 720×1280，低分辨率可能降低此模型画质；低显存策略不保证 14B 模型在任意 GPU 上可运行，可设置另一个低显存 profile。

默认 SD1.5 工作流没有图像 reference 输入，参考资产仍会生成并传入支持该角色的替换工作流；该默认项依靠 Bible 文本维持一致性，不能声称具备视觉身份锁定。需要更强一致性时导入支持 `reference_image` / `style_reference` 的工作流。

重建模板：`python3 scripts/build_bundled_workflows.py`。本仓库测试只校验结构、绑定与客户端协议，真实模型效果在外部验收阶段确认。
