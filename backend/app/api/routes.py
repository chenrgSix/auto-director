import asyncio
import json
import mimetypes
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, File, Query, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from app.core.errors import AppError
from app.core.security import validate_comfy_url
from app.db.store import uid
from app.generation.schemas import ACTIVE, EpisodeCreate, TestRun, TimelineUpdate
from app.media.service import AUDIO_EXT, IMAGE_EXT, VIDEO_EXT
from app.workflows.schema import WorkflowImport, WorkflowPatch

router = APIRouter(prefix="/api/v1")


class SettingsPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    comfyui_url: str = Field(min_length=1, max_length=500)


class ResolutionNote(BaseModel):
    note: str = Field(min_length=10, max_length=2000)


def resources(request: Request):
    return request.app.state


@router.get("/health")
def health():
    return {"status": "ok", "version": "0.1.0"}


@router.get("/settings")
def settings(request: Request):
    state = resources(request)
    saved = state.store.get("settings", "settings")
    return {
        **saved,
        "comfyui_url": saved.get("comfyui_url") or state.config.comfyui_url,
        "llm_configured": bool(state.config.llm_model),
        "llm_model": state.config.llm_model,
        "vlm_model": state.config.vlm_model,
        "vlm_configured": bool(state.config.vlm_model),
        "allow_public_comfyui": state.config.allow_public_comfyui,
        "max_asset_mb": state.config.max_asset_mb,
    }


@router.patch("/settings")
async def update_settings(request: Request, body: SettingsPatch):
    state = resources(request)
    if any(episode["status"] in ACTIVE for episode in state.store.list("episode")) or any(
        job["status"] in {"QUEUED", "RUNNING", "UNKNOWN"} for job in state.store.list("job")
    ):
        raise AppError("CONFLICT", "存在进行中或状态不确定的作业，不能切换 ComfyUI", status=409)
    url = await validate_comfy_url(body.comfyui_url, state.config.allow_public_comfyui)
    state.store.update("settings", "settings", {"comfyui_url": url})
    return settings(request)


@router.get("/comfyui/status")
@router.get("/comfyui/system")
@router.post("/comfyui/test")
async def comfy_status(request: Request):
    state = resources(request)
    async with state.engine.client() as client:
        system, info = await asyncio.gather(client.system(), client.object_info())
    profiles = []
    for profile in state.store.list("workflow"):
        validated = state.workflows.validate(profile["id"], info)
        profiles.append(
            {"id": profile["id"], "name": profile["name"], "validation": validated["validation"]}
        )
    return {"connected": True, "system": system, "node_count": len(info), "workflows": profiles}


@router.get("/workflows")
def workflows(request: Request):
    return resources(request).store.list("workflow")


@router.post("/workflows/import", status_code=201)
def import_workflow(request: Request, body: WorkflowImport):
    return resources(request).workflows.import_workflow(body)


@router.get("/workflows/{id}")
def workflow(request: Request, id: str):
    return resources(request).store.get("workflow", id)


@router.patch("/workflows/{id}")
def edit_workflow(request: Request, id: str, body: WorkflowPatch):
    return resources(request).workflows.update(id, body)


@router.delete("/workflows/{id}", status_code=204)
def delete_workflow(request: Request, id: str):
    resources(request).workflows.delete(id)


@router.post("/workflows/{id}/validate")
async def validate_workflow(request: Request, id: str):
    state = resources(request)
    state.store.get("workflow", id)
    async with state.engine.client() as client:
        info = await client.object_info()
    return state.workflows.validate(id, info)


@router.post("/workflows/{id}/default")
def default_workflow(request: Request, id: str):
    return resources(request).workflows.set_default(id)


@router.post("/workflows/{id}/test-run", status_code=202)
async def test_workflow(request: Request, id: str, body: TestRun):
    state = resources(request)
    profile = state.store.get("workflow", id)
    for role in {"start_frame", "end_frame"} & profile["bindings"].keys():
        if role not in body.asset_bindings:
            raise AppError("INVALID_MEDIA", f"请上传并选择 {role} 图片后试跑")
    for asset_id in body.asset_bindings.values():
        state.assets.path(asset_id)
    job = state.engine.create_job(
        profile,
        body.values,
        body.asset_bindings,
        "workflow-tests",
        None,
        "WORKFLOW_TEST",
        body.parameter_values,
        "test:" + uid(),
    )
    state.store.update("workflow", id, {"last_test_job_id": job["id"]})
    state.generation.queue.put_nowait(("job", job["id"]))
    return public_job(job)


def public_job(job: dict) -> dict:
    return {
        key: value
        for key, value in job.items()
        if key not in {"profile_snapshot", "patched_workflow"}
    }


@router.get("/jobs")
def jobs(request: Request, episode_id: str | None = None):
    return [public_job(job) for job in resources(request).store.list("job", episode_id)]


@router.get("/jobs/{id}")
def job(request: Request, id: str):
    return public_job(resources(request).store.get("job", id))


@router.post("/jobs/{id}/reconcile")
async def reconcile(request: Request, id: str):
    return public_job(await resources(request).engine.reconcile(id))


@router.post("/jobs/{id}/resolve")
def resolve(request: Request, id: str, body: ResolutionNote):
    return public_job(resources(request).engine.acknowledge_absent(id, body.note))


@router.post("/jobs/{id}/resume", status_code=202)
async def resume_job(request: Request, id: str):
    state = resources(request)
    record = state.store.get("job", id)
    if (
        record["type"] != "WORKFLOW_TEST"
        or record["status"] != "UNKNOWN"
        or not record["comfy_prompt_id"]
    ):
        raise AppError(
            "CONFLICT",
            "仅已核对 prompt_id 的不确定试跑作业可在此恢复；单集作业请继续生成单集",
            status=409,
        )
    state.generation.queue.put_nowait(("job", id))
    return public_job(record)


@router.post("/jobs/{id}/cancel")
async def cancel_job(request: Request, id: str):
    state = resources(request)
    record = state.store.get("job", id)
    if record["type"] != "WORKFLOW_TEST":
        raise AppError("CONFLICT", "请通过单集取消接口取消此作业", status=409)
    if record["status"] == "QUEUED":
        return public_job(state.store.update("job", id, {"status": "CANCELLED"}))
    if record["status"] in {"RUNNING", "UNKNOWN"}:
        if not record["comfy_prompt_id"]:
            raise AppError("SUBMISSION_UNKNOWN", "提交状态不确定，请先核对历史", status=409)
        async with state.engine.client() as client:
            await client.cancel(record["comfy_prompt_id"])
        return public_job(state.store.update("job", id, {"status": "CANCELLED"}))
    return public_job(record)


@router.post("/episodes", status_code=201)
def create_episode(request: Request, body: EpisodeCreate):
    return resources(request).generation.create(body)


@router.get("/episodes")
def episodes(request: Request):
    return resources(request).store.list("episode")


@router.get("/episodes/{id}")
def episode(request: Request, id: str):
    return resources(request).store.get("episode", id)


@router.delete("/episodes/{id}", status_code=204)
async def delete_episode(request: Request, id: str, delete_assets: bool = False):
    state = resources(request)
    item = state.store.get("episode", id)
    if (
        id in state.generation.busy
        or item["status"] in ACTIVE
        or any(
            job["status"] in {"QUEUED", "RUNNING", "UNKNOWN"} for job in state.store.list("job", id)
        )
    ):
        raise AppError("CONFLICT", "请先停止并核对该单集的未完成作业", status=409)
    if delete_assets:
        await state.assets.delete_episode(id)
    for job in state.store.list("job", id):
        state.store.delete("job", job["id"])
    for result in state.store.list("qa", id):
        state.store.delete("qa", result["id"])
    state.store.delete("episode", id)


@router.post("/episodes/{id}/generate", status_code=202)
async def generate(request: Request, id: str):
    return resources(request).generation.enqueue(id)


@router.post("/episodes/{id}/compose", status_code=202)
async def compose_episode(request: Request, id: str):
    return resources(request).generation.enqueue(id, "compose")


@router.post("/episodes/{id}/cancel")
async def cancel(request: Request, id: str):
    return await resources(request).generation.cancel(id)


@router.get("/episodes/{id}/shots")
def shots(request: Request, id: str):
    return resources(request).store.get("episode", id)["shots"]


@router.get("/episodes/{id}/qa")
def qa_history(request: Request, id: str):
    resources(request).store.get("episode", id)
    return resources(request).store.list("qa", id)


@router.patch("/episodes/{id}/timeline")
def timeline(request: Request, id: str, body: TimelineUpdate):
    return resources(request).generation.timeline(id, body)


@router.post("/shots/{id}/retry", status_code=202)
@router.post("/shots/{id}/retry-keyframes", status_code=202)
@router.post("/shots/{id}/retry-video", status_code=202)
async def retry(request: Request, id: str):
    return resources(request).generation.retry(
        id, "video" if request.url.path.endswith("retry-video") else "keyframes"
    )


@router.get("/episodes/{id}/progress")
def progress(request: Request, id: str):
    state = resources(request)
    item = state.store.get("episode", id)
    enabled = [shot for shot in item["shots"] if shot["enabled"]]
    passed = sum(shot["status"] == "PASSED" for shot in enabled)
    return {
        "episode_id": id,
        "status": item["status"],
        "version": item["version"],
        "shots_passed": passed,
        "shots_total": len(enabled),
        "percent": 100
        if item["status"] == "COMPLETED"
        else round(passed / max(len(enabled), 1) * 90),
        "error": item["error"],
        "jobs": [public_job(job) for job in state.store.list("job", id)[:10]],
    }


@router.get("/episodes/{id}/events")
async def events(request: Request, id: str):
    resources(request).store.get("episode", id)

    async def stream():
        while not await request.is_disconnected():
            data = progress(request, id)
            yield "event: progress\ndata: " + json.dumps(data, ensure_ascii=False) + "\n\n"
            if data["status"] in {"COMPLETED", "FAILED", "CANCELLED"}:
                break
            await asyncio.sleep(1)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def save_upload(request, upload, owner):
    state = resources(request)
    extension = Path(upload.filename or "").suffix.lower()
    if extension not in IMAGE_EXT | VIDEO_EXT | AUDIO_EXT:
        raise AppError("INVALID_MEDIA", "不支持此媒体格式")
    expected = (
        "image/" if extension in IMAGE_EXT else "video/" if extension in VIDEO_EXT else "audio/"
    )
    if not (upload.content_type or "").startswith(expected):
        raise AppError("INVALID_MEDIA", "上传 MIME 与扩展名不匹配")
    target = state.assets.allocate(owner, extension)
    size = 0
    try:
        with target.open("wb") as output:
            while chunk := await upload.read(1024 * 1024):
                size += len(chunk)
                if size > state.config.max_asset_mb * 1024 * 1024:
                    raise AppError("INVALID_MEDIA", "上传超出大小限制", status=413)
                output.write(chunk)
        return await state.assets.register(target, owner, "USER_UPLOAD")
    except BaseException:
        await asyncio.to_thread(target.unlink, missing_ok=True)
        raise
    finally:
        await upload.close()


@router.post("/assets", status_code=201)
async def upload_test_asset(request: Request, file: Annotated[UploadFile, File()]):
    return await save_upload(request, file, "workflow-tests")


@router.post("/episodes/{id}/assets", status_code=201)
async def upload_episode_asset(request: Request, id: str, file: Annotated[UploadFile, File()]):
    resources(request).store.get("episode", id)
    return await save_upload(request, file, id)


@router.get("/assets")
def assets(request: Request, episode_id: str | None = None):
    return resources(request).store.list("asset", episode_id)


@router.get("/assets/{id}/file")
def asset_file(request: Request, id: str, download: bool = Query(False)):
    state = resources(request)
    path = state.assets.path(id)
    return FileResponse(
        path,
        media_type=mimetypes.guess_type(path.name)[0],
        filename=path.name if download else None,
    )
