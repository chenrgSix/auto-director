from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.api.routes import router
from app.core.config import ROOT, Settings
from app.core.errors import AppError
from app.core.runtime_settings import RuntimeSettings
from app.creation.mcp import create_mcp
from app.creation.routes import router as creation_router
from app.creation.service import CreationService
from app.db.store import Store
from app.generation.engine import RenderEngine
from app.generation.pipeline import GenerationService
from app.media.service import Assets
from app.workflows.manager import WorkflowManager


def create_app(
    config: Settings | None = None, *, client_factory=None, provider_factory=None
) -> FastAPI:
    config = (config or Settings()).model_copy(deep=True)

    @asynccontextmanager
    async def lifespan(app):
        state = app.state
        state.config = config
        state.store = Store(config.storage_root)
        state.workflows = WorkflowManager(state.store)
        state.workflows.bootstrap()
        state.runtime_settings = RuntimeSettings(config, state.store.get("settings", "settings"))
        state.assets = Assets(state.store)
        state.engine = RenderEngine(config, state.store, state.assets, client_factory)
        state.generation = GenerationService(
            config, state.store, state.engine, state.assets, provider_factory
        )
        state.creation = CreationService(state.generation)
        await state.generation.start()
        try:
            async with app.state.mcp.session_manager.run():
                yield
        finally:
            await state.generation.stop()
            state.store.close()

    app = FastAPI(title="AutoDirector", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "[::1]", "testserver"]
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.allowed_origins,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["Content-Type"],
    )

    @app.middleware("http")
    async def local_boundary(request: Request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            own = f"{request.url.scheme}://{request.url.netloc}"
            if origin and origin not in {*config.allowed_origins, own}:
                return JSONResponse(
                    {
                        "error": {
                            "code": "FORBIDDEN_ORIGIN",
                            "message": "不允许此网页来源修改本地项目",
                        }
                    },
                    status_code=403,
                )
            length = request.headers.get("content-length")
            limit = (
                config.max_asset_mb * 1024 * 1024 + 65536
                if "multipart/form-data" in request.headers.get("content-type", "")
                else 2 * 1024 * 1024
            )
            if length and (not length.isdecimal() or int(length) > limit):
                return JSONResponse(
                    {"error": {"code": "PAYLOAD_TOO_LARGE", "message": "请求体超出大小限制"}},
                    status_code=413,
                )
        return await call_next(request)

    @app.exception_handler(AppError)
    async def app_error(_, exc):
        return JSONResponse({"error": exc.as_dict()}, status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_, exc):
        return JSONResponse(
            {
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": "请求字段不符合约定",
                    "details": [
                        {"loc": list(error["loc"]), "message": error["msg"]}
                        for error in exc.errors()
                    ],
                }
            },
            status_code=422,
        )

    app.include_router(router)
    app.include_router(creation_router)
    app.state.mcp = create_mcp(app, config)
    app.mount("/mcp", app.state.mcp.streamable_http_app())
    frontend = ROOT / "frontend" / "dist"
    if frontend.is_dir():
        app.mount("/", StaticFiles(directory=frontend, html=True), name="frontend")
    return app


app = create_app()
