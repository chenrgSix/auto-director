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
- 公共图片的 `imageFile` 保存为 ComfyUI `input` 下的完整相对路径，例如 `autodirector/<图片文件名>.png`，并同步公共设置与 R2V 工作区。Director 图片加载器不拼接单独的 `subfolder` 字段；只存文件名会静默忽略素材。换素材请在导演台重新选择图片，并确认运行报告显示参考图数量。

## 当前依赖与验证边界

2026-09-14 用户更新 `ComfyUI_MiniMaxH3_Director` 并重启后，原来的 R2V 条件编码错误 `unsupported operand type(s) for //: 'str' and 'int'` 已不再出现。作者的 [conditioning.py](https://github.com/AIMixer/ComfyUI_MiniMaxH3_Director/blob/main/nodes/conditioning.py) 采用关键字传参；旧版插件仍需先更新。

本画布前两段已完成真实生成：两段运行报告均显示 4 张参考图，第二段继承上段 22 帧 AV 上下文；124 帧与 136 帧有效画面统一导出 260 帧、10.833333 秒，768×448、24 FPS。完整 FFmpeg 解码通过；音轨时长 10.816 秒，与画面相差约 17 毫秒，未完成听感验收。抽查全片及接缝帧，人物、场景和面碗保持，未见明显画面跳变；动作仍有停顿，不能推断所有题材均流畅。

保存验证覆盖节点与连线、模型选择、公共素材与工作区的一致性，以及 ComfyUI 服务端覆盖同名文件后读回。保存版保留六段配置，但本次只试跑前两段；先前 29.96 秒样片由独立原生节点和 Motion Context 生成，不能算作本画布六段运行验收。浏览器连接超时，未宣称画布 UI 实测通过。

六份误拆分的远端工作流已撤回，本仓库仅保存这一个完整画布。保存证据在本地忽略目录 `data/repairs/c61-saved-workflows/`；更新后的真实生成与修复证据在 `data/repairs/c62-director-after-update/corrected/`。刷新 ComfyUI 后，从工作流列表重新打开同名文件，以加载服务端保存的配置。
