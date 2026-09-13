# H3 多图参考

在工作流页导入 `h3_multi_reference.profile.json`（完整配置），作为关键帧图像工作流。原始 `source.comfy.json` 是用户提供的画布；它只连接主图，第 2～9 张接口原本为空。

转换脚本：`backend/.venv/bin/python scripts/build_multi_reference.py`。转换保留原模型、12 步采样、5 帧质量及推荐帧选择策略；将源图自动尺寸改为导演预算控制的宽高（模板 640×384），清除示例提示和文件名，新增八个可选输入。不替换现有默认配置。

在镜头 `visual_continuity.reference_roles` 中按顺序选 `character:<id>`、`environment`、`prop:<id>` 或 `style`。第一张必需，其余按需连接；不要空缺中间编号。主图提供主要身份，辅助图提供布局、道具或风格；端点编辑时第一张替换为本镜首帧。缺少主图会报错，未选的辅助 LoadImage 会在提交前移除，不能使用占位文件。

`bible.props` 的条目包含 `id`、`description` 和 `distinguishing_features`，同一 ID 在跨镜头反打中复用同一资产。建议从人物、场景、关键道具三张开始，不要把更多参考等同于更好画面。

2026-09-13：已对当前 ComfyUI 的实际 object_info 检查节点和权重依赖，并在 RTX 5060 8GB 上以 1280×704 真实运行两图/三图。三图关键帧约 102～105 秒/张；人物、空场景、面碗实际连接，未选辅助节点被裁去。送碗、接碗两镜的状态和动作通过本次复核；无人特写首尾图正确，视频仍会中途插人，不能把多图支持等同于整片稳定。

本轮未见渲染/OOM 失败，约三秒一次的 ComfyUI 可用显存采样最低约 0.24 GiB；这不是峰值显存测量，也未测试九图。详细证据见 [C59 验收](../../docs/ACCEPTANCE.md#2026-09-13--c59-三图参考真实模型验收)。`source_fidelity` 控制保留特征的提示措辞，不是采样降噪强度。
