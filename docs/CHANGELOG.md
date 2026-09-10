# 迭代记录

## 2026-09-10 · D01 文档基线

- 固定 P0 + Phase 1/2 的整体 MVP，分离 Phase 3 与真实视觉验收。
- 建立任务、模块、API、开发运行、验收、决策和索引文档。
- 实测环境：Python 3.14.6、Node 22.23.1、FFmpeg 8.1.1；默认 ComfyUI 尚未取得有效响应。
- 本迭代仅完成文档基线，尚无代码实现验收。

## 2026-09-10 · B01 存储与配置基础

- 建立 Python 3.12 uv 锁定环境、配置/结构化异常、SQLite WAL versioned 聚合仓储和资产路径保护。
- ComfyUI 地址只允许配置来源；默认 loopback/LAN，阻止 metadata/link-local，公网需显式开关。
- 验证：`backend/.venv/bin/pytest -q`（backend 目录）3 passed；Ruff 检查通过。
- 用户明确先完成项目，不在本机部署 ComfyUI；真实 API 接入待整体交付后。
