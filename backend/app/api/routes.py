import asyncio
import json
import mimetypes
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, File, Query, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from app.agents.diagnostics import ModelTestRequest, check_model
from app.core.errors import AppError
from app.core.limits import DURATION_POLICY, LIMITS
from app.core.runtime_settings import SettingsPatch
from app.db.store import uid
from app.generation.preproduction import ImageReviewDecision
from app.generation.preproduction import context as image_review_context
from app.generation.preproduction import decide as decide_image_review
from app.generation.prompt_optimization import apply_proposal, find_shot, propose
from app.generation.prompt_preparation import preparation_progress
from app.generation.qa_policy import change_qa_policy
from app.generation.quality import change_quality
from app.generation.schemas import (
    ACTIVE,
    EpisodeCreate,
    EpisodeQAPolicyUpdate,
    EpisodeQualityUpdate,
    EpisodeRerun,
    EpisodeWorkflowRestore,
    EpisodeWorkflowsUpdate,
    PreviewApproval,
    PreviewUpdate,
    PromptOptimizationRequest,
    ReviewedPreviewApproval,
    TestRun,
    TimelineUpdate,
)
from app.media.service import AUDIO_EXT, IMAGE_EXT, VIDEO_EXT
from app.workflows.ownership import required_asset_roles
from app.workflows.recognition import recognize_workflow
from app.workflows.schema import WorkflowAnalyze, WorkflowImport, WorkflowPatch

router = APIRouter(prefix="/api/v1")


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
        "default_image": saved["default_image"],
        "default_video": saved["default_video"],
        "duration_policy": DURATION_POLICY,
        "limits": LIMITS,
        "default_capabilities": saved.get("default_capabilities", {}),
        **state.runtime_settings.public(),
    }


@router.patch("/settings")
async def update_settings(request: Request, body: SettingsPatch):
    state = resources(request)

    def busy():
        return (
            bool(state.generation.busy)
            or state.engine.lock.locked()
            or any(episode["status"] in ACTIVE for episode in state.store.list("episode"))
            or any(
                job["status"] in {"QUEUED", "RUNNING", "UNKNOWN"} for job in state.store.list("job")
            )
        )

    await state.runtime_settings.update(body, busy)
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


@router.post("/models/test")
async def test_model(request: Request, body: ModelTestRequest):
    async def disconnected():
        while True:
            if (await request.receive())["type"] == "http.disconnect":
                return

    state = resources(request)
    probe = asyncio.create_task(
        check_model(
            state.config.model_copy(deep=True),
            state.generation.provider_factory(),
            body.kind,
        )
    )
    disconnect = asyncio.create_task(disconnected())
    try:
        done, _ = await asyncio.wait({probe, disconnect}, return_when=asyncio.FIRST_COMPLETED)
        if probe in done:
            return probe.result()
        raise AppError("REQUEST_CANCELLED", "已取消模型测试", status=499)
    finally:
        for task in (probe, disconnect):
            task.cancel()
        await asyncio.gather(probe, disconnect, return_exceptions=True)


@router.get("/workflows")
def workflows(request: Request):
    state = resources(request)
    return [state.workflows.describe(p) for p in state.store.list("workflow")]


@router.post("/workflows/analyze")
async def analyze_workflow(request: Request, body: WorkflowAnalyze):
    async def disconnected():
        while True:
            message = await request.receive()
            if message["type"] == "http.disconnect":
                return

    state = resources(request)
    recognition = asyncio.create_task(
        recognize_workflow(
            body.workflow,
            state.generation.provider_factory(),
            body.capability,
            timeout_seconds=state.config.llm_timeout,
        )
    )
    disconnect = asyncio.create_task(disconnected())
    try:
        done, _ = await asyncio.wait({recognition, disconnect}, return_when=asyncio.FIRST_COMPLETED)
        if recognition in done:
            return recognition.result()
        raise AppError("REQUEST_CANCELLED", "已取消 AI 识别", status=499)
    finally:
        for task in (recognition, disconnect):
            task.cancel()
        await asyncio.gather(recognition, disconnect, return_exceptions=True)


@router.post("/workflows/import", status_code=201)
def import_workflow(request: Request, body: WorkflowImport):
    return resources(request).workflows.import_workflow(body)


@router.get("/workflows/{id}")
def workflow(request: Request, id: str):
    state = resources(request)
    return state.workflows.describe(state.store.get("workflow", id))


@router.post("/workflows/{id}/auto-bind")
def auto_bind_workflow(request: Request, id: str):
    resources(request).store.get("workflow", id)
    raise AppError(
        "WORKFLOW_RULES_REMOVED", "规则自动绑定已移除，请选择 AI 识别或手动绑定", status=410
    )


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
    asset_parameters = {
        item["role"]
        for item in profile["parameters"]
        if item["key"] in body.parameter_values and item["owner"] == "asset_resolver"
    }
    for role in required_asset_roles(profile):
        if role not in body.asset_bindings and role not in asset_parameters:
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
    state.store.update(
        "workflow",
        id,
        {
            "last_test_job_id": job["id"],
            "configuration_version": profile.get("configuration_version", profile["version"]),
        },
    )
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
    return resources(request).generation.detail(id)


@router.delete("/episodes/{id}", status_code=204)
async def delete_episode(request: Request, id: str, delete_assets: bool = False):
    state = resources(request)
    item = state.store.get("episode", id)
    if id in state.generation.busy:
        message = (
            "正在停止后台任务，清理完成后即可删除，请稍候"
            if item["status"] == "CANCELLED"
            else "短片仍在后台执行或排队，请先取消任务再删除"
        )
        raise AppError("CONFLICT", message, status=409)
    if item["status"] in ACTIVE:
        raise AppError("CONFLICT", "短片正在生成，请先取消任务再删除", status=409)
    pending = [
        {"id": job["id"], "status": job["status"]}
        for job in state.store.list("job", id)
        if job["status"] in {"QUEUED", "RUNNING", "UNKNOWN"}
    ]
    if pending:
        raise AppError(
            "CONFLICT",
            "该短片还有未结算的 ComfyUI 作业，请在作业记录中恢复或核对后再删除",
            {"jobs": pending},
            status=409,
        )
    await state.generation.reviews.cancel_episode(id)
    if delete_assets:
        await state.assets.delete_episode(id)
    for job in state.store.list("job", id):
        state.store.delete("job", job["id"])
    for result in state.store.list("qa", id):
        state.store.delete("qa", result["id"])
    for proposal in state.store.list("prompt_optimization", id):
        state.store.delete("prompt_optimization", proposal["id"])
    state.store.delete("episode", id)


@router.post("/episodes/{id}/generate", status_code=202)
async def generate(request: Request, id: str):
    return resources(request).generation.enqueue(id)


@router.post("/episodes/{id}/rerun", status_code=202)
async def rerun_episode(request: Request, id: str, body: EpisodeRerun):
    return resources(request).generation.rerun(id, body)


@router.get("/episodes/{id}/shots/{shot_id}/prompt-optimization")
def latest_prompt_optimization(request: Request, id: str, shot_id: str):
    state = resources(request)
    current = state.store.get("episode", id)
    find_shot(current, shot_id)
    return next(
        (p for p in state.store.list("prompt_optimization", id) if p["shot_id"] == shot_id),
        None,
    )


@router.post("/episodes/{id}/shots/{shot_id}/prompt-optimization", status_code=201)
async def propose_prompt_optimization(
    request: Request, id: str, shot_id: str, body: PromptOptimizationRequest
):
    state = resources(request)

    async def disconnected():
        while True:
            if (await request.receive())["type"] == "http.disconnect":
                return

    operation = asyncio.create_task(propose(state.generation, id, shot_id, body))
    disconnect = asyncio.create_task(disconnected())
    try:
        async with asyncio.timeout(state.config.llm_timeout):
            done, _ = await asyncio.wait(
                {operation, disconnect}, return_when=asyncio.FIRST_COMPLETED
            )
            if operation in done:
                return operation.result()
            raise AppError("REQUEST_CANCELLED", "已取消提示词优化", status=499)
    except TimeoutError as exc:
        raise AppError("LLM_TIMEOUT", "提示词优化超时，原视频与提示词已保留", status=504) from exc
    finally:
        for task in (operation, disconnect):
            task.cancel()
        await asyncio.gather(operation, disconnect, return_exceptions=True)


@router.post("/episodes/{id}/prompt-optimizations/{proposal_id}/apply", status_code=202)
async def confirm_prompt_optimization(
    request: Request, id: str, proposal_id: str, body: PreviewApproval
):
    return apply_proposal(resources(request).generation, id, proposal_id, body.expected_version)


@router.post("/episodes/{id}/preview", status_code=202)
async def prepare_preview(request: Request, id: str):
    return resources(request).generation.enqueue(id, "preview")


@router.patch("/episodes/{id}/preview")
async def edit_preview(request: Request, id: str, body: PreviewUpdate):
    return resources(request).generation.edit_preview(id, body)


@router.post("/episodes/{id}/approve", status_code=202)
async def approve_preview(request: Request, id: str, body: ReviewedPreviewApproval):
    return resources(request).generation.enqueue(
        id, "approve", body.expected_version, accept_script_review=body.accept_script_review
    )


@router.patch("/episodes/{id}/quality")
async def update_episode_quality(request: Request, id: str, body: EpisodeQualityUpdate):
    state = resources(request)
    return change_quality(state.store, state.generation.busy, id, body)


@router.patch("/episodes/{id}/qa-policy")
async def update_episode_qa_policy(request: Request, id: str, body: EpisodeQAPolicyUpdate):
    state = resources(request)
    result = change_qa_policy(state.store, state.generation.busy, id, body)
    if not result.get("qa_enabled", True) or result.get("qa_policy") != "advisory":
        await state.generation.reviews.cancel_episode(id)
        result = state.store.get("episode", id)
    return result


@router.patch("/episodes/{id}/workflows")
async def update_episode_workflows(request: Request, id: str, body: EpisodeWorkflowsUpdate):
    return resources(request).generation.change_workflows(id, body)


@router.post("/episodes/{id}/workflows/restore")
async def restore_episode_workflows(request: Request, id: str, body: EpisodeWorkflowRestore):
    return resources(request).generation.restore_workflow_story(id, body)


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
    reviewing = item.get("preview_required") and not item.get("preview_approved_at")
    preparation = preparation_progress(item) if reviewing else None
    return {
        "episode_id": id,
        "status": item["status"],
        "version": item["version"],
        "shots_passed": passed,
        "shots_total": len(enabled),
        "phase": "preview" if reviewing else "generation",
        "preparation": preparation,
        "percent": 100
        if item["status"] == "COMPLETED"
        else round(preparation["completed"] / max(preparation["total"], 1) * 100)
        if reviewing
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
            if data["status"] in {
                "COMPLETED",
                "FAILED",
                "CANCELLED",
                "AWAITING_REVIEW",
                "AWAITING_IMAGE_REVIEW",
            }:
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


@router.get("/episodes/{id}/image-review")
def get_image_review(request: Request, id: str):
    pipeline = request.app.state.generation
    return image_review_context(pipeline, pipeline.store.get("episode", id))


@router.post("/episodes/{id}/image-review", status_code=202)
async def post_image_review(request: Request, id: str, body: ImageReviewDecision):
    return decide_image_review(request.app.state.generation, id, body)
