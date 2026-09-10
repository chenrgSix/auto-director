# Repository Guidelines

## Project Structure & Module Organization

AutoDirector is currently a design-stage repository for a ComfyUI-native short-video directing system.

- `README.md` introduces the product and intended generation pipeline.
- `docs/AutoDirector_完整产品与技术设计文档_v1.0.md` defines the proposed architecture, schemas, roadmap, and acceptance criteria.
- No application source, tests, or asset directories exist yet. The design proposes `backend/app/`, `backend/tests/`, `frontend/`, and `bundled_workflows/{text_to_image,first_last_video}/`; treat these as planned locations.

## Build, Test, and Development Commands

No dependency manifests, build scripts, development server, or automated test runner are configured.

- `git status --short`: inspect changed and untracked files before editing or committing.
- `git diff --check`: check tracked changes for whitespace errors; use `git diff --cached --check` after staging new files.

When introducing runnable code, document installation, local startup, build, and test commands in `README.md` alongside the relevant manifests.

## Coding Style & Naming Conventions

Keep Markdown sections focused, use fenced blocks with language labels, and preserve existing schema names such as `Episode`, `Shot`, `RenderJob`, and `WorkflowProfile`. Distinguish proposed behavior from implemented functionality.

No formatter or linter is configured. For the proposed Python backend, use four-space indentation and `snake_case` modules/functions. For the proposed React/TypeScript frontend, use two-space indentation and `PascalCase` components. Add shared tool configuration when scaffolding each stack.

## Architecture Guidelines

Import ComfyUI workflows in API JSON format. Bind inputs through semantic role tags rather than fixed node IDs. Keep workflow profiles replaceable and separate generation orchestration from ComfyUI execution.

## Testing Guidelines

No test framework or coverage threshold is established. For documentation changes, check referenced paths, examples, and consistency with the design.

When implementing the backend, place tests in `backend/tests/` with descriptive `test_*.py` names. Follow the design's test plan: parsing, role detection, JSON patching, duration splitting, state transitions, and prompt schemas. Verify upload, execution, progress, history, and download against real ComfyUI for integration acceptance. Report unit, integration, and end-to-end results separately.

## Commit & Pull Request Guidelines

History contains only `Initial commit`, so no established message convention exists. Use concise imperative subjects, for example `docs: add contributor guidelines`.

Commit each independent code change separately, stage only task-related files, preserve existing user changes, and never use `git commit --no-verify`.

Pull requests should describe the change, reference relevant design sections or issues, list actual validation and remaining limits, and include screenshots when UI behavior changes.
