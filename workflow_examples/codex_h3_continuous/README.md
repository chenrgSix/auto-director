# codex-H3-Ref2VA-连续镜头

基于用户指定的 `60秒.json`（画布 ID `d0d1f1a2-4b1f-4d98-ab7a-ed3c4bfd549a`）修改，保留原有 **10 个节点、MiniMaxH3Director 导演台、分段编辑和 VHS 统一音视频导出**。这是一个完整工作流，片段在同一个导演台内管理。

保存文件：`codex-H3-Ref2VA-连续镜头.json`。ComfyUI 工作流列表中的同名文件可直接打开，也可把仓库这份 JSON 拖入 ComfyUI。新画布 ID 为 `20484113-0eed-52ac-9901-ab786732ebaf`；原 `60秒.json` 保留。

## 本次调整

- R2V 模式配套 `minimax_h3_ref2va_pruned_int8_convrot.safetensors` 与 `minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors`，强度 1，4 步 Euler/simple，CFG 1，video/audio shift 为 12/3。
- 模型连接为 UNET → LoRA → Comfy Kitchen attention → Director；保留原 Qwen3-VL NVFP4、视频 VAE 和音频 VAE。
- 768×448、24 FPS，启用段间引导、22 帧上下文，以及后续片段的「引用上段」。声音使用原生生成，导出接收 Director 的实际 FPS；开启段间显存清理，关闭原片额外导出与即时 TAE 预览。
- 导演台内保存《雨夜留一碗》的六段提示词和公共素材：人物、场景、面碗、构图。第 4 张只作构图参考，不代表严格的第 0 帧 Guide；因此不能保证逐帧复现此前的独立 Motion Context 样片。
- 清理原示例遗留的旧预览、关键帧及其它题材草稿，公共参数与 R2V 工作区保持一致。示例每段规划 124 帧，实际时长以导演台生成/裁剪后的导出为准；可在导演台增加、删除片段或修改时长，名称不限定总秒数。
- 输出前缀为 `codex/continuous/take01`。换作品时修改公共素材、各段提示词和输出前缀即可。

## 当前依赖与验证边界

当前服务器已安装的 Director 在 R2V 条件编码时存在参数位置兼容错误，之前真实调用报 `unsupported operand type(s) for //: 'str' and 'int'`。作者主分支的 [conditioning.py](https://github.com/AIMixer/ComfyUI_MiniMaxH3_Director/blob/main/nodes/conditioning.py) 已改为按关键字传递官方节点参数，修复该问题。**使用此保存版前，需要更新 `ComfyUI_MiniMaxH3_Director` 并重启 ComfyUI。** 本次没有远程改写插件或重启服务。

保存验证覆盖节点与连线、模型选择、R2V 提示词、公共素材与工作区的一致性，以及 ComfyUI 服务端保存后读回。先前 29.96 秒样片由独立原生节点和 Motion Context 生成，不能算作这个 Director 版本的真实运行验收。浏览器连接超时，未宣称画布 UI 实测通过。

六份误拆分的远端工作流已撤回，本仓库仅保存这一个完整画布。详细检查结果保存在本地忽略目录 `data/repairs/c61-saved-workflows/`。
