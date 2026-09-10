import asyncio
import contextlib
import json
import mimetypes
import time
from collections.abc import Awaitable, Callable
from pathlib import Path, PurePosixPath
from urllib.parse import urlencode

import httpx
from websockets.asyncio.client import connect

from app.core.config import Settings
from app.core.errors import AppError
from app.core.security import validate_comfy_url

Callback = Callable[[dict], Awaitable[None]]


class ComfyUIClient:
    def __init__(self, settings: Settings, url: str, *, transport=None, use_websocket=True):
        self.settings = settings
        self.url = url.rstrip("/")
        self.transport = transport
        self.use_websocket = use_websocket
        self.http: httpx.AsyncClient | None = None

    async def __aenter__(self):
        await validate_comfy_url(self.url, self.settings.allow_public_comfyui)
        self.http = httpx.AsyncClient(
            base_url=self.url,
            timeout=self.settings.request_timeout,
            follow_redirects=False,
            trust_env=False,
            transport=self.transport,
        )
        return self

    async def __aexit__(self, *_):
        await self.http.aclose()

    async def request(self, method: str, path: str, **kwargs) -> httpx.Response:
        for attempt in range(3 if method == "GET" else 1):
            try:
                response = await self.http.request(method, path, **kwargs)
                if response.status_code >= 500 and method == "GET" and attempt < 2:
                    await asyncio.sleep(0.1 * 2**attempt)
                    continue
                if response.status_code >= 400 or 300 <= response.status_code < 400:
                    if path == "/prompt" and response.status_code >= 500:
                        raise AppError(
                            "SUBMISSION_UNKNOWN",
                            "提交返回服务端错误，是否已受理未知，请先核对队列",
                            status=502,
                        )
                    code = "PROMPT_REJECTED" if path == "/prompt" else "COMFYUI_OFFLINE"
                    try:
                        details = response.json()
                    except ValueError:
                        details = response.text[:2000]
                    raise AppError(
                        code, f"ComfyUI {path} 返回 {response.status_code}", details, status=502
                    )
                return response
            except httpx.RequestError as exc:
                if method == "GET" and attempt < 2:
                    await asyncio.sleep(0.1 * 2**attempt)
                    continue
                if path == "/prompt":
                    raise AppError(
                        "SUBMISSION_UNKNOWN",
                        "渲染提交响应丢失，服务端可能已受理；请核对作业后恢复，不能自动重发",
                        status=502,
                    ) from exc
                raise AppError("COMFYUI_OFFLINE", f"ComfyUI {path} 无法连接", status=502) from exc
        raise AppError("COMFYUI_OFFLINE", "ComfyUI 读取失败", status=502)

    async def json(self, method: str, path: str, **kwargs) -> dict:
        response = await self.request(method, path, **kwargs)
        try:
            data = response.json()
        except ValueError as exc:
            raise AppError("COMFYUI_OFFLINE", f"ComfyUI {path} 返回无效 JSON", status=502) from exc
        if not isinstance(data, dict):
            raise AppError("COMFYUI_OFFLINE", f"ComfyUI {path} 返回格式不正确", status=502)
        return data

    async def system(self) -> dict:
        return await self.json("GET", "/system_stats")

    async def object_info(self) -> dict:
        return await self.json("GET", "/object_info")

    async def history(self, prompt_id: str) -> dict:
        data = await self.json("GET", f"/history/{prompt_id}")
        return data.get(prompt_id, {})

    async def upload(self, path: Path) -> str:
        with path.open("rb") as file:
            response = await self.json(
                "POST",
                "/upload/image",
                files={
                    "image": (
                        path.name,
                        file,
                        mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                    )
                },
                data={"type": "input", "overwrite": "false", "subfolder": "autodirector"},
            )
        name = response.get("name")
        if not name:
            raise AppError("EXECUTION_ERROR", "ComfyUI 上传未返回文件名", response)
        subfolder = response.get("subfolder", "")
        return f"{subfolder}/{name}" if subfolder else name

    async def download(self, metadata: dict, target: Path) -> None:
        name = metadata.get("filename", "")
        subfolder = metadata.get("subfolder", "")
        if (
            not name
            or PurePosixPath(name).name != name
            or "\\" in name
            or PurePosixPath(subfolder).is_absolute()
            or ".." in PurePosixPath(subfolder).parts
            or "\\" in subfolder
            or metadata.get("type", "output") not in {"input", "temp", "output"}
        ):
            raise AppError("INVALID_MEDIA", "ComfyUI 返回非法文件元数据")
        limit = self.settings.max_asset_mb * 1024 * 1024
        try:
            async with self.http.stream(
                "GET",
                "/view",
                params={
                    "filename": name,
                    "subfolder": subfolder,
                    "type": metadata.get("type", "output"),
                },
            ) as response:
                if response.status_code != 200:
                    raise AppError("OUTPUT_NOT_FOUND", "无法下载 ComfyUI 产物", status=502)
                target.parent.mkdir(parents=True, exist_ok=True)
                size = 0
                with target.open("wb") as file:
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > limit:
                            raise AppError("INVALID_MEDIA", "渲染产物超过大小限制")
                        file.write(chunk)
        except BaseException:
            await asyncio.to_thread(target.unlink, missing_ok=True)
            raise

    async def cancel(self, prompt_id: str) -> None:
        queue = await self.json("GET", "/queue")
        pending = queue.get("queue_pending", [])
        running = queue.get("queue_running", [])
        if any(item[1] == prompt_id for item in pending):
            await self.request("POST", "/queue", json={"delete": [prompt_id]})
        if any(item[1] == prompt_id for item in running):
            await self.request("POST", "/interrupt", json={"prompt_id": prompt_id})

    @staticmethod
    def outputs(history: dict, output_node: str, kind: str) -> list[dict]:
        result = history.get("outputs", {}).get(output_node, {})
        keys = ("images",) if kind == "image" else ("videos", "gifs", "images")
        extensions = (
            {".png", ".jpg", ".jpeg", ".webp"}
            if kind == "image"
            else {".mp4", ".webm", ".mkv", ".mov"}
        )
        return [
            item
            for key in keys
            for item in result.get(key, [])
            if isinstance(item, dict)
            and Path(item.get("filename", "")).suffix.lower() in extensions
        ]

    async def execute(
        self,
        graph: dict,
        job_id: str,
        on_submit: Callback,
        on_progress: Callback,
        cancelled: Callable[[], bool],
        *,
        prompt_id: str | None = None,
    ) -> dict:
        websocket = None
        try:
            if self.use_websocket:
                with contextlib.suppress(OSError, TimeoutError, Exception):
                    websocket = await connect(
                        self.url.replace("https://", "wss://").replace("http://", "ws://")
                        + "/ws?"
                        + urlencode({"clientId": job_id}),
                        open_timeout=3,
                        close_timeout=1,
                        proxy=None,
                        max_size=2 * 1024 * 1024,
                    )
            if prompt_id is None:
                if cancelled():
                    raise AppError("CANCELLED", "作业已取消")
                data = await self.json(
                    "POST",
                    "/prompt",
                    json={
                        "prompt": graph,
                        "client_id": job_id,
                        "extra_data": {"autodirector_job_id": job_id},
                    },
                )
                prompt_id = data.get("prompt_id")
                if not prompt_id:
                    raise AppError(
                        "SUBMISSION_UNKNOWN", "ComfyUI 成功响应未返回 prompt_id，需先核对队列", data
                    )
                await on_submit({"comfy_prompt_id": prompt_id})
            started = time.monotonic()
            while time.monotonic() - started < self.settings.render_timeout:
                if cancelled():
                    await self.cancel(prompt_id)
                    raise AppError("CANCELLED", "作业已取消")
                history = await self.history(prompt_id)
                status = history.get("status", {})
                if status.get("status_str") == "error":
                    messages = status.get("messages", [])
                    oom = "out of memory" in json.dumps(
                        messages
                    ).lower() or "OutOfMemoryError" in json.dumps(messages)
                    raise AppError(
                        "OUT_OF_MEMORY" if oom else "EXECUTION_ERROR",
                        "ComfyUI 执行失败",
                        messages,
                        status=502,
                    )
                if status.get("completed") and history.get("outputs"):
                    return history
                if websocket is not None:
                    try:
                        raw = await asyncio.wait_for(websocket.recv(), self.settings.poll_interval)
                        if isinstance(raw, str):
                            event = json.loads(raw)
                            data = event.get("data", {})
                            if data.get("prompt_id") == prompt_id and event.get("type") in {
                                "progress",
                                "executing",
                                "execution_start",
                            }:
                                await on_progress({"event": event["type"], **data})
                    except TimeoutError:
                        pass
                    except Exception:
                        await websocket.close()
                        websocket = None
                else:
                    await asyncio.sleep(self.settings.poll_interval)
            raise AppError(
                "JOB_TIMEOUT",
                "渲染等待超时，已保留 prompt_id；可读取历史恢复",
                {"prompt_id": prompt_id},
                status=504,
            )
        finally:
            if websocket is not None:
                await websocket.close()
