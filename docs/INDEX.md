# AutoDirector 文档索引

本项目按文档驱动开发。产品需求的源头是 [v1.0 产品与技术设计](AutoDirector_完整产品与技术设计文档_v1.0.md)，实现决策、进度和验证证据在下面的活文档中维护。

| 文档 | 职责 |
| --- | --- |
| [制作前画面确认](PREPRODUCTION_REVIEW.md) | C58 参考/关键帧确认、共享道具与多图输入 |
| [H3 多图工作流](../workflow_examples/h3_multi_reference/README.md) | 用户画布转换、可选辅助图片与实测边界 |
| [H3 Ref2VA 视频](../workflow_examples/h3_ref2va/README.md) | 首帧锚定、多参考视频与段间运动实测 |
| [镜头参考与连续性](SHOT_CONTINUITY.md) | C57 角色参考、空间/动作状态、制作前检查与画面复核 |
| [创作包与 MCP](CREATION_PACKAGES.md) | C53 外部持续创作、版本交付、MCP 和验收目标 |
| [开发任务](DEVELOPMENT_TASKS.md) | 冻结目标、任务、状态、验收门禁与后续范围 |
| [模块设计](MODULE_DESIGN.md) | 模块边界、数据、运行时与失败恢复 |
| [工作流能力与参数归属](WORKFLOW_CAPABILITIES.md) | C03–C06 能力分支、AI 参数、快速导入、实际字段配置与验收门禁 |
| [工作流配置指南](WORKFLOW_CONFIGURATION.md) | 在页面绑定实际字段、修改模型参数、检查与试跑 |
| [codex 8GB 工作流包](../workflow_examples/codex_8gb/README.md) | 四类能力、已安装模型适配、显式导入请求与试跑边界 |
| [接口契约](API_CONTRACT.md) | API、角色绑定、错误和状态约定 |
| [开发与运行](DEVELOPMENT.md) | 环境、启动、配置、测试与工作流准备 |
| [验收记录](ACCEPTANCE.md) | 已执行证据、未执行门禁及复现步骤 |
| [设计决策](DECISIONS.md) | 原文歧义的解释、技术选型与变更理由 |
| [迭代记录](CHANGELOG.md) | 每次独立交付及对应事实更新 |

维护规则：先改涉及的契约/决策，再实现和验证；完成任务时补证据并更新状态。`已实现`、`本地验证通过`、`真实集成通过`、`视觉验收通过`是不同事实，不互相替代。原始设计不因实现缺口被静默改写。
