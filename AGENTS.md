# Repository Guidelines

## Project Structure & Module Organization

AutoDirector is a local ComfyUI video directing studio. `backend/app/` contains FastAPI routes, directing agents, workflow analysis, ComfyUI transport, generation orchestration, SQLite storage, and media processing. Backend tests and isolated dependency fixtures live in `backend/tests/`. `frontend/src/` contains React/TypeScript pages, shared UI, and styles. Replaceable API JSON templates live in `bundled_workflows/`; their generator is `scripts/build_bundled_workflows.py`. Runtime data belongs in ignored `data/`.

## Build, Test, and Development Commands

Use Python 3.12, uv, Node 22, npm, and FFmpeg/ffprobe. Run commands from the repository root:

- `make install`: install locked backend and frontend dependencies.
- `make dev-api` and `make dev-web`: start separate development servers on 8000 and 5173.
- `make build && make run`: build the UI and serve the complete app on 8000.
- `make check`: run Ruff, formatting checks, ESLint, TypeScript, build, and pytest.
- `make test`: run backend contracts and real FFmpeg tests.
- `make browser-fixture`: start an isolated UI acceptance server with synthetic media on 8011.

See `docs/DEVELOPMENT.md` for configuration and recovery procedures. Run one API worker. Do not install ComfyUI or download model weights unless requested.

## Coding Style & Naming Conventions

Python uses four spaces, `snake_case`, type hints, Pydantic schemas, and Ruff with a 100-character formatting target. TypeScript uses two spaces, `PascalCase` components, strict types, and ESLint. Preserve semantic workflow roles; never rely on fixed node IDs. Use backend-only runtime configuration for secrets; never return saved keys to the browser.

## Testing Guidelines

Use descriptive `test_*.py` names with pytest/pytest-asyncio. Cover changed state transitions, workflow bindings, uncertain submissions, and media behavior. No coverage percentage is mandated. Report fixture integration, browser checks, real model acceptance, and CI separately; synthetic output does not demonstrate visual quality.

## Documentation & Architecture

Start from `docs/INDEX.md` and the v1.0 source design. Update affected contracts, tasks, decisions, and acceptance evidence with each change. If `.codegraph/` exists, use available CodeGraph exploration before locating indexed code; fall back to `rg` when unavailable or stale.

## Commit & Pull Request Guidelines

History uses concise `docs:`, `feat:`, `fix:`, and `chore:` subjects. Commit each independent change, stage only related files, preserve existing user changes, and never bypass hooks. PRs should explain behavior, reference design sections/issues, list actual validation and remaining limits, and include screenshots for UI changes.
