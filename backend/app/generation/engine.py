import asyncio
from copy import deepcopy
from pathlib import Path

from app.comfyui.client import ComfyUIClient
from app.core.config import Settings
from app.core.errors import AppError
from app.db.store import Store, now
from app.generation.parameters import resolve_parameters
from app.generation.preflight import check_graph
from app.generation.resolvers import AssetResolver
from app.media.service import Assets, video_duration
from app.workflows.analyzer import patch, refresh_profile, validate_dependencies, workflow_hash
from app.workflows.optional_references import select_optional_references
from app.workflows.ownership import canonicalize


class RenderEngine:
    def __init__(self, settings: Settings, store: Store, assets: Assets, client_factory=None):
        self.settings, self.store, self.assets = settings, store, assets
        self.client_factory = client_factory
        self.lock = asyncio.Lock()

    def client(self):
        url = self.settings.comfyui_url
        return (
            self.client_factory(url) if self.client_factory else ComfyUIClient(self.settings, url)
        )

    def unresolved(self) -> list[dict]:
        return [job for job in self.store.list("job") if job["status"] == "UNKNOWN"]

    def validate_outputs(self, job, records):
        if (
            job["profile_snapshot"].get("media_type", job["profile_snapshot"].get("type"))
            == "video"
        ):
            for record in records:
                video_duration(record["metadata"])

    def create_job(
        self,
        profile: dict,
        values: dict,
        asset_bindings: dict,
        episode_id: str,
        shot_id: str | None,
        type: str,
        parameter_values: dict | None = None,
        step_key: str = "",
        *,
        advanced_mode: bool = True,
        budget: dict | None = None,
        allowed_asset_ids: list[str] | None = None,
        ai_values: dict | None = None,
    ) -> dict:
        profile = canonicalize(deepcopy(profile))
        requested = parameter_values or {}
        values, asset_bindings, parameter_values, sources = resolve_parameters(
            profile,
            values,
            asset_bindings,
            requested,
            advanced_mode,
            budget,
            recovery=bool(values.get("_oom_recovery")),
            ai_values=ai_values,
        )
        signature = workflow_hash(
            {
                "workflow": profile["workflow_hash"],
                "capability": profile["capability"],
                "parameter_rules": profile.get("parameter_rules", {}),
                "requested": requested,
                **({"ai_parameters": ai_values} if ai_values else {}),
                "bindings": profile["bindings"],
                "outputs": profile["outputs"],
                "capabilities": profile["capabilities"],
                "parameters": {**profile.get("parameter_values", {}), **(parameter_values or {})},
                "values": values,
                "assets": asset_bindings,
                "step": step_key,
            }
        )
        for old in self.store.list("job", episode_id):
            if old.get("signature") == signature and old["status"] in {"COMPLETED", "UNKNOWN"}:
                return old
        return self.store.create(
            "job",
            {
                "status": "QUEUED",
                "episode_id": episode_id,
                "shot_id": shot_id,
                "type": type,
                "workflow_id": profile["id"],
                "workflow_hash": profile["workflow_hash"],
                "profile_snapshot": profile,
                "input_values": values,
                "asset_bindings": asset_bindings,
                "parameter_values": parameter_values or {},
                "requested_parameter_values": requested,
                "ai_parameter_values": ai_values or {},
                "parameter_sources": sources,
                "advanced_mode": advanced_mode,
                "budget_snapshot": budget,
                "allowed_asset_ids": allowed_asset_ids or [],
                "signature": signature,
                "step_key": step_key,
                "comfy_prompt_id": None,
                "patched_workflow": None,
                "output_asset_ids": [],
                "error": None,
                "progress": None,
                "attempt": 1,
            },
            parent=episode_id,
        )

    async def run(self, job_id: str, cancelled=lambda: False) -> list[dict]:
        async with self.lock:
            job = self.store.get("job", job_id)
            if job["status"] == "COMPLETED":
                records = [self.store.get("asset", id) for id in job["output_asset_ids"]]
                try:
                    self.validate_outputs(job, records)
                except AppError as exc:
                    self.store.update("job", job_id, {"status": "FAILED", "error": exc.as_dict()})
                    raise
                return records
            unresolved = [item for item in self.unresolved() if item["id"] != job_id]
            if unresolved:
                raise AppError(
                    "UNRESOLVED_JOB",
                    "存在状态不确定的渲染作业，请先核对历史后继续",
                    {"job_ids": [item["id"] for item in unresolved]},
                    status=409,
                )
            if job["status"] == "UNKNOWN" and not job["comfy_prompt_id"]:
                raise AppError(
                    "SUBMISSION_UNKNOWN",
                    "此作业未取得 prompt_id，请在作业记录中核对",
                    {"job_id": job_id},
                    status=409,
                )
            if cancelled():
                self.store.update("job", job_id, {"status": "CANCELLED"})
                raise AppError("CANCELLED", "作业已取消")
            profile = job["profile_snapshot"]
            submitted = bool(job["comfy_prompt_id"])
            try:
                async with self.client() as client:
                    if not job["patched_workflow"]:
                        info = await client.object_info()
                        checked = refresh_profile(
                            profile, info, job.get("requested_parameter_values", {})
                        )
                        automatic = dict(job["input_values"])
                        if "timeline_duration" in automatic:
                            automatic["duration"] = automatic["timeline_duration"]
                        values, bound_assets, raw, sources = resolve_parameters(
                            checked,
                            automatic,
                            job["asset_bindings"],
                            job.get("requested_parameter_values", job["parameter_values"]),
                            job.get("advanced_mode", True),
                            job.get("budget_snapshot"),
                            recovery=bool(automatic.get("_oom_recovery")),
                            ai_values=job.get("ai_parameter_values", {}),
                        )
                        resolved_values = dict(values)
                        checked = select_optional_references(checked, bound_assets, info)
                        check_graph(
                            checked,
                            info,
                            values,
                            raw,
                            advanced=job.get("advanced_mode", True),
                            ai_values=job.get("ai_parameter_values", {}),
                            deferred_inputs={
                                p["key"]
                                for p in checked["parameters"]
                                if p.get("owner") == "asset_resolver"
                            },
                        )
                        values.update(
                            await AssetResolver(self.store, self.assets).resolve(
                                profile,
                                bound_assets,
                                client,
                                job["episode_id"],
                                job.get("allowed_asset_ids", []),
                                test=job["type"] == "WORKFLOW_TEST",
                            )
                        )
                        graph = patch(
                            checked,
                            values,
                            raw,
                            advanced=job.get("advanced_mode", True),
                            ai_values=job.get("ai_parameter_values", {}),
                        )
                        report = validate_dependencies(
                            {**checked, "workflow": graph, "parameter_values": {}}, info
                        )
                        if not report["valid"]:
                            raise AppError(
                                "WORKFLOW_INVALID", "工作流依赖未满足", report["issues"], status=422
                            )
                        job = self.store.update(
                            "job",
                            job_id,
                            {
                                "patched_workflow": graph,
                                "input_values": resolved_values,
                                "parameter_sources": sources,
                                "status": "RUNNING",
                                "started_at": now(),
                            },
                        )
                    else:
                        self.store.update("job", job_id, {"status": "RUNNING", "error": None})

                    async def on_submit(data):
                        nonlocal submitted
                        submitted = True
                        job.update(data)
                        self.store.update("job", job_id, data)

                    async def on_progress(data):
                        def change(current):
                            progress = current.get("progress") or {}
                            if data.get("event") in {"executing", "execution_start"}:
                                progress.pop("value", None)
                                progress.pop("max", None)
                            current["progress"] = {**progress, **data}

                        self.store.update("job", job_id, change)

                    history = await client.execute(
                        job["patched_workflow"],
                        job_id,
                        on_submit,
                        on_progress,
                        cancelled,
                        prompt_id=job["comfy_prompt_id"],
                    )
                    metadata = client.outputs(
                        history, profile["outputs"][profile["media_type"]], profile["media_type"]
                    )
                    if not metadata:
                        raise AppError(
                            "OUTPUT_NOT_FOUND",
                            "指定输出节点没有可用的图像/视频",
                            {"output_node": profile["outputs"][profile["media_type"]]},
                        )
                    records = []
                    for item in metadata[:8]:
                        target = self.assets.allocate(
                            job["episode_id"], Path(item["filename"]).suffix.lower()
                        )
                        await client.recover_read(
                            lambda metadata=item, path=target: client.download(metadata, path),
                            job["comfy_prompt_id"],
                            on_progress,
                            cancelled,
                        )
                        record = await self.assets.register(
                            target, job["episode_id"], job["type"], job["shot_id"]
                        )
                        records.append(record)
                    # Keep rejected outputs accessible in job history for inspection.
                    self.store.update(
                        "job", job_id, {"output_asset_ids": [r["id"] for r in records]}
                    )
                    self.validate_outputs(job, records)
                    self.store.update(
                        "job",
                        job_id,
                        {
                            "status": "COMPLETED",
                            "output_asset_ids": [record["id"] for record in records],
                            "completed_at": now(),
                            "error": None,
                        },
                    )
                    return records
            except AppError as exc:
                uncertain = exc.code in {"SUBMISSION_UNKNOWN", "JOB_TIMEOUT"} or (
                    submitted and exc.code == "COMFYUI_OFFLINE"
                )
                state = (
                    "UNKNOWN" if uncertain else "CANCELLED" if exc.code == "CANCELLED" else "FAILED"
                )
                self.store.update("job", job_id, {"status": state, "error": exc.as_dict()})
                raise
            except asyncio.CancelledError:
                self.store.update(
                    "job",
                    job_id,
                    {
                        "status": "UNKNOWN",
                        "error": {
                            "code": "WORKER_INTERRUPTED",
                            "message": "进程中断，渲染状态需核对",
                        },
                    },
                )
                raise
            except Exception:
                self.store.update(
                    "job",
                    job_id,
                    {
                        "status": "UNKNOWN" if submitted else "FAILED",
                        "error": {
                            "code": "INTERNAL_ERROR",
                            "message": "渲染处理异常；已保留作业信息",
                        },
                    },
                )
                raise

    async def reconcile(self, job_id: str) -> dict:
        job = self.store.get("job", job_id)
        if job["status"] != "UNKNOWN":
            return job
        async with self.client() as client:
            queue = await client.json("GET", "/queue")
            history = await client.json("GET", "/history", params={"max_items": 1000})
            entries = queue.get("queue_running", []) + queue.get("queue_pending", [])
            entries += [value.get("prompt", []) for value in history.values()]
            prompt_id = job["comfy_prompt_id"]
            for entry in entries:
                if (
                    len(entry) > 3
                    and isinstance(entry[3], dict)
                    and (
                        entry[3].get("autodirector_job_id") == job_id
                        or entry[3].get("client_id") == job_id
                    )
                ):
                    prompt_id = entry[1]
                    break
            if not prompt_id:
                raise AppError(
                    "SUBMISSION_UNKNOWN",
                    "当前队列与可用历史未找到作业，仍不能确认未受理；需核对 ComfyUI 历史后人工确认",
                    {"job_id": job_id},
                    status=409,
                )
            return self.store.update("job", job_id, {"comfy_prompt_id": prompt_id, "error": None})

    def acknowledge_absent(self, job_id: str, note: str) -> dict:
        job = self.store.get("job", job_id)
        if job["status"] != "UNKNOWN" or len(note.strip()) < 10:
            raise AppError(
                "CONFLICT", "仅可在人工核对后为 UNKNOWN 作业填写至少 10 字的结论", status=409
            )
        return self.store.update(
            "job",
            job_id,
            {
                "status": "FAILED",
                "error": {"code": "MANUALLY_RESOLVED", "message": note.strip()},
                "resolved_at": now(),
            },
        )
