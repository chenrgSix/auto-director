"""Continuous group identity and production; keyframe production remains a separate route."""

from copy import deepcopy

from app.agents.directing import anchored_prompt
from app.core.errors import AppError
from app.generation.async_reviews import visual_qa_enabled
from app.generation.continuity import CONTINUOUS, reference_roles
from app.generation.parameters import parameter_overrides, role_overrides, usable_ai_values
from app.generation.recovery import replay_job
from app.media.service import media_duration
from app.workflows.analyzer import workflow_hash
from app.workflows.sequence import MAX_REFERENCES, MAX_SEGMENTS


def sequence_mode(episode):
    return episode.get("production_mode") == "reference_sequence"


def shot_groups(episode):
    groups = []
    for shot in episode.get("shots", []):
        if not shot.get("enabled", True):
            continue
        if shot["transition_from_previous"] in CONTINUOUS:
            if not groups:
                raise AppError("SEQUENCE_INVALID", "首个启用片段不能延续前段", status=422)
            groups[-1].append(shot)
        else:
            groups.append([shot])
        if len(groups[-1]) > MAX_SEGMENTS:
            raise AppError(
                "SEQUENCE_INVALID",
                f"一个连续镜头组最多 {MAX_SEGMENTS} 段，请在分镜中设置合理切镜",
                status=422,
            )
    return groups


def validate_groups(episode, profile=None):
    groups = shot_groups(episode)
    for group in groups:
        generated = (
            [
                usable_ai_values(
                    profile,
                    s["prompts"].get("ai_parameters", {}).get(profile["id"], {}),
                    stage_prompt=True,
                )
                for s in group
            ]
            if profile
            else []
        )
        if generated and any(value != generated[0] for value in generated[1:]):
            raise AppError(
                "SEQUENCE_INVALID", "同一连续组的采样参数必须一致，请统一参数或设置切镜", status=422
            )
        for shot in group:
            if not (shot.get("prompts") or {}).get("visual_continuity"):
                raise AppError(
                    "SHOT_REFERENCE_REQUIRED",
                    "连续片段需要明确的参考与动作状态",
                    {"shot_id": shot["id"]},
                    422,
                )
            roles = reference_roles(episode.get("bible"), shot)
            if not 1 <= len(roles) <= MAX_REFERENCES:
                raise AppError(
                    "SHOT_REFERENCE_INVALID",
                    f"每段需要 1–{MAX_REFERENCES} 张参考图",
                    {"shot_id": shot["id"]},
                    422,
                )
    return groups


def group_views(episode):
    return [
        {
            "id": group[0]["id"],
            "shot_ids": [s["id"] for s in group],
            "title": group[0]["title"],
            "planned_duration": sum(s["duration"] for s in group),
            "status": "PASSED"
            if all(s["status"] == "PASSED" for s in group)
            else "FAILED"
            if any(s["status"] == "FAILED" for s in group)
            else "RENDERING_VIDEO"
            if any(s["status"] in {"RENDERING_VIDEO", "VIDEO_READY", "QA"} for s in group)
            else "PENDING",
        }
        for group in shot_groups(episode)
    ]


def expand_groups(episode, selected):
    return {
        s["id"]
        for group in shot_groups(episode)
        if any(s["id"] in selected for s in group)
        for s in group
    }


def group_request(episode, group, profile, budget):
    references, keys, segments = {}, {}, []
    overrides = parameter_overrides(episode, profile)
    role_values = role_overrides(profile, overrides)
    ai_values = None
    for shot in group:
        prompts = shot["prompts"]
        generated = usable_ai_values(
            profile, prompts.get("ai_parameters", {}).get(profile["id"], {}), stage_prompt=True
        )
        if ai_values is not None and generated != ai_values:
            raise AppError("SEQUENCE_INVALID", "同一连续组的采样参数必须一致，请统一参数或设置切镜")
        ai_values = generated
        selected, hints = [], []
        for index, role in enumerate(reference_roles(episode["bible"], shot), 1):
            asset = episode["references"].get(role)
            if not asset:
                raise AppError(
                    "ASSET_REQUIRED",
                    "连续片段的参考素材尚未准备",
                    {"shot_id": shot["id"], "role": role},
                )
            if asset not in keys:
                keys[asset] = f"reference_image_{len(keys) + 1}"
                references[keys[asset]] = asset
            selected.append(keys[asset])
            hints.append(
                f"<Picture {index}> supplies {role}; follow its appearance while performing the requested action."
            )
        prompt = role_values.get("prompt") or anchored_prompt(
            episode["bible"],
            prompts["video_prompt"],
            prompts["continuity_state"],
            stage="video",
            visual_continuity=prompts.get("visual_continuity"),
        )
        # Put reference semantics before native H3's first section; no timed first-frame header.
        if "prompt" not in role_values:
            prompt = "\n".join(hints) + "\n\n" + prompt
        segments.append(
            {
                "shot_id": shot["id"],
                "duration": shot["duration"],
                "prompt": prompt,
                "references": selected,
            }
        )
    versions = {shot["id"]: shot["retry_version"] for shot in group}
    values = {
        **budget,
        "duration": group[0]["duration"],
        "prompt": segments[0]["prompt"],
        "seed": (episode["seed"] + group[0]["index"] + group[0].get("seed_offset", 0)) % 2147483648,
        "_sequence": {"segments": segments},
        "_sequence_versions": versions,
    }
    step = (
        "sequence:"
        + group[0]["id"]
        + ":"
        + workflow_hash(
            {
                "members": versions,
                "values": values,
                "references": references,
                "workflow": profile["id"],
                "binding": episode.get("workflow_binding_revision", 0),
                "overrides": overrides,
                "ai": ai_values,
            }
        )
    )
    return values, references, overrides, ai_values, step


async def generate_groups(service, episode, profile, budget, agents):
    id = episode["id"]
    groups = validate_groups(episode, profile)
    previous = None
    for planned in groups:
        service.check_cancel(id)
        group = [service.shot(id, s["id"]) for s in planned]
        members = [s["id"] for s in group]
        if all(
            s["status"] == "PASSED"
            and s.get("video_asset_id")
            and s.get("sequence_members") == members
            for s in group
        ):
            previous = group[-1]
            continue
        current = service.store.get("episode", id)
        values, references, overrides, generated, step = group_request(
            current, group, profile, budget
        )
        job = replay_job(service.store, id, step)
        # Recovery must use the durable request even if live hardware budgets changed.
        if job is None:
            recovery = current.get("render_recovery") or {}
            for job_id in recovery.get("jobs", {}).values():
                saved = service.store.get("job", job_id)
                if saved["type"] == "SEQUENCE_VIDEO" and saved["shot_id"] == members[0]:
                    job = saved
                    break
        if job is None:
            job = service.engine.create_job(
                profile,
                values,
                references,
                id,
                group[0]["id"],
                "SEQUENCE_VIDEO",
                overrides,
                step,
                advanced_mode=episode.get("advanced_mode", False),
                budget=budget,
                allowed_asset_ids=episode.get("allowed_asset_ids", []),
                ai_values=generated,
            )
        service.stage(id, "RENDERING_VIDEO")
        for shot in group:
            service.update_shot(
                id,
                shot["id"],
                status="RENDERING_VIDEO",
                sequence_group_id=members[0],
                sequence_members=members,
                sequence_job_id=job["id"],
                error=None,
            )
        try:
            outputs = await service.engine.run(job["id"], lambda: service.cancelled(id))
            service.check_cancel(id)
            if len(outputs) != len(group):
                raise AppError("SEQUENCE_OUTPUT_INVALID", "连续组返回片段数不一致，未绑定结果")

            def bind(current, group=group, outputs=outputs):
                if current["status"] == "CANCELLED":
                    raise AppError("CANCELLED", "连续组已取消")
                by_id = {s["id"]: s for s in current["shots"]}
                if any(
                    not by_id[s["id"]]["enabled"]
                    or by_id[s["id"]]["retry_version"] != s["retry_version"]
                    for s in group
                ):
                    raise AppError("CONFLICT", "连续组版本已变更，未绑定结果", status=409)
                for shot, output in zip(group, outputs, strict=True):
                    by_id[shot["id"]].update(
                        video_asset_id=output["id"],
                        actual_duration=media_duration(output["metadata"]),
                        status="VIDEO_READY",
                        continuity_after=deepcopy(shot["prompts"]["continuity_state"]),
                        reference_selection={
                            "source": "sequence_references",
                            "roles": reference_roles(current["bible"], shot),
                        },
                    )

            service.store.update("episode", id, bind)
            service.clear_recovery(id, group[0]["id"])
            for member in group:
                shot = await service.ensure_full_tail(id, service.shot(id, member["id"]))
                if previous and not previous.get("actual_end_frame_asset_id"):
                    previous = await service.ensure_full_tail(id, previous)
                if visual_qa_enabled(current, service.settings):
                    await service.review_video(
                        current,
                        shot,
                        previous,
                        agents,
                        advisory=current.get("qa_policy", "strict") == "advisory",
                    )
                service.update_shot(id, shot["id"], status="PASSED")
                previous = service.shot(id, shot["id"])
        except AppError as exc:
            if not service.cancelled(id):
                for shot in group:
                    service.update_shot(id, shot["id"], status="FAILED", error=exc.as_dict())
            raise
