.PHONY: install dev-api dev-web build run check test browser-fixture service-install service-start service-stop service-status

install:
	uv sync --project backend --frozen --python 3.12
	npm --prefix frontend ci

dev-api:
	cd backend && .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload

dev-web:
	npm --prefix frontend run dev

build:
	npm --prefix frontend run build

run:
	cd backend && .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000

service-install service-start service-stop service-status:
	cd backend && .venv/bin/python -m app.service $(patsubst service-%,%,$@)

check:
	cd backend && .venv/bin/ruff check app tests
	cd backend && .venv/bin/ruff format --check app tests
	npm --prefix frontend run lint
	npm --prefix frontend run typecheck
	$(MAKE) build
	$(MAKE) test

test:
	command -v ffmpeg >/dev/null
	command -v ffprobe >/dev/null
	cd backend && .venv/bin/pytest -q

browser-fixture: build
	cd backend && .venv/bin/python -m tests.browser_server
