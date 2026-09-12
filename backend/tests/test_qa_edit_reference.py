"""An image edit must repair the rejected picture that visual QA actually inspected."""

from copy import deepcopy

import httpx
import pytest

from tests.test_api_capabilities import install_i2i
from tests.test_api_pipeline import wait_episode
from tests.test_episode_workflows import imported
from tests.test_qa_frame_retries import (
    CORRECTION,
    approve,
    install_qa,
    positive,
    prepared,
    shot_jobs,
)

EDIT_REFERENCE = "The supplied reference image is the rejected frame to repair."


def chronological_jobs(app, episode, kind):
    return sorted(shot_jobs(app, episode, kind=kind), key=lambda job: job["created_at"])


def test_i2i_tail_repairs_latest_rejected_picture_and_keeps_good_start_and_history(system):
    client, app, comfy = system
    image = install_i2i(client, comfy)
    install_qa(system, failures=2)
    reviewed = prepared(system, image_workflow_id=image["id"], max_retries=2)
    completed = approve(client, reviewed)
    assert completed["status"] == "COMPLETED", completed.get("error")
    starts = chronological_jobs(app, completed, "SHOT_START_FRAME")
    ends = chronological_jobs(app, completed, "SHOT_END_FRAME")
    assert len(starts) == 1 and len(ends) == 3
    start_id = completed["shots"][0]["start_frame_asset_id"]
    assert ends[0]["asset_bindings"]["reference_image"] == start_id
    assert CORRECTION not in positive(ends[0])
    assert EDIT_REFERENCE not in positive(ends[0])
    for previous, corrected in zip(ends[:-1], ends[1:], strict=True):
        rejected_id = previous["output_asset_ids"][0]
        assert corrected["asset_bindings"]["reference_image"] == rejected_id
        assert corrected["asset_bindings"]["reference_image"] != start_id
        assert CORRECTION in positive(corrected)
        assert EDIT_REFERENCE in positive(corrected)
        assert corrected["parameter_sources"]["reference.image"] == "asset_resolver"
        assert corrected["patched_workflow"]["reference"]["inputs"]["image"] == (
            "autodirector/fixture.png"
        )
        assert client.get(f"/api/v1/assets/{rejected_id}/file").status_code == 200
        assert app.state.store.get("job", previous["id"]) == previous
    assert completed["shots"][0]["start_frame_asset_id"] in starts[0]["output_asset_ids"]
    assert completed["shots"][0]["end_frame_asset_id"] in ends[-1]["output_asset_ids"]
    assert len(shot_jobs(app, completed, kind="SHOT_VIDEO")) == 1
    assert completed["plan"] == reviewed["plan"]
    assert completed["shots"][0]["prompts"] == reviewed["shots"][0]["prompts"]


def test_i2i_start_repair_with_i2v_references_rejected_start_without_rendering_end(system):
    client, app, comfy = system
    image = install_i2i(client, comfy)
    video = imported(app, "IMAGE_TO_VIDEO")
    install_qa(system, failed_frame="start_frame")
    reviewed = prepared(system, image_workflow_id=image["id"], video_workflow_id=video["id"])
    completed = approve(client, reviewed)
    assert completed["status"] == "COMPLETED", completed.get("error")
    starts = chronological_jobs(app, completed, "SHOT_START_FRAME")
    assert len(starts) == 2
    assert starts[1]["asset_bindings"]["reference_image"] == starts[0]["output_asset_ids"][0]
    assert CORRECTION in positive(starts[1])
    assert EDIT_REFERENCE in positive(starts[1])
    assert not shot_jobs(app, completed, kind="SHOT_END_FRAME")
    assert completed["shots"][0]["end_frame_asset_id"] is None
    assert completed["shots"][0]["prompts"] == reviewed["shots"][0]["prompts"]


def test_t2i_qa_correction_does_not_upload_a_reference_picture(system):
    client, app, comfy = system
    install_qa(system)
    reviewed = prepared(system)
    completed = approve(client, reviewed)
    assert completed["status"] == "COMPLETED", completed.get("error")
    ends = chronological_jobs(app, completed, "SHOT_END_FRAME")
    assert len(ends) == 2 and CORRECTION in positive(ends[1])
    assert all(EDIT_REFERENCE not in positive(job) for job in ends)
    assert all("reference" not in job["patched_workflow"] for job in ends)
    # Only the video's start and end conditioning images need uploading for T2I.
    assert comfy.calls.count(("POST", "/upload/image")) == 2


def test_user_image_reference_override_keeps_priority_during_qa_repair(system):
    client, app, comfy = system
    image = install_i2i(client, comfy)
    response = client.post(
        "/api/v1/assets", files={"file": ("user-reference.png", comfy.image_bytes, "image/png")}
    )
    assert response.status_code == 201, response.text
    user_reference = response.json()["id"]
    install_qa(system)
    reviewed = prepared(
        system,
        image_workflow_id=image["id"],
        advanced_mode=True,
        workflow_overrides={image["id"]: {"reference.image": user_reference}},
    )
    completed = approve(client, reviewed)
    assert completed["status"] == "COMPLETED", completed.get("error")
    ends = chronological_jobs(app, completed, "SHOT_END_FRAME")
    assert len(ends) == 2 and CORRECTION in positive(ends[1])
    for job in ends:
        assert job["asset_bindings"]["reference_image"] == user_reference
        assert job["parameter_sources"]["reference.image"] == "user"
        assert EDIT_REFERENCE not in positive(job)


@pytest.mark.parametrize("interrupt_corrected_tail", [False, True])
def test_continue_i2i_correction_preserves_rejected_reference_through_unknown_recovery(
    system, interrupt_corrected_tail
):
    client, app, comfy = system
    image = install_i2i(client, comfy)
    qa = install_qa(system, failures=1)
    reviewed = prepared(system, image_workflow_id=image["id"], max_retries=0)
    failed = approve(client, reviewed)
    assert failed["error"]["code"] == "QA_FAILED", failed.get("error")
    rejected_id = failed["shots"][0]["end_frame_asset_id"]
    good_start_id = failed["shots"][0]["start_frame_asset_id"]
    original_images = {
        asset_id: client.get(f"/api/v1/assets/{asset_id}/file").content
        for job in shot_jobs(app, failed)
        for asset_id in job["output_asset_ids"]
    }
    qa["failures"] = 0
    comfy.settings.render_timeout = 1
    original = comfy.handle
    state = {"offline": interrupt_corrected_tail, "job_id": None}

    def handle(request):
        if request.url.path.startswith("/history/"):
            prompt_id = request.url.path.rsplit("/", 1)[1]
            submitted = comfy.prompts.get(prompt_id)
            if submitted:
                job = app.state.store.get("job", submitted["client_id"])
                if job["type"] == "SHOT_END_FRAME" and CORRECTION in positive(job):
                    state["job_id"] = job["id"]
                    if state["offline"]:
                        raise httpx.ReadTimeout(
                            "Disconnected during image correction", request=request
                        )
        return original(request)

    comfy.handle = handle
    response = client.post(f"/api/v1/episodes/{failed['id']}/generate")
    assert response.status_code == 202, response.text
    completed = wait_episode(client, failed["id"])
    if interrupt_corrected_tail:
        assert completed["error"]["code"] == "JOB_TIMEOUT", completed.get("error")
        pending = app.state.store.get("job", state["job_id"])
        assert pending["status"] == "UNKNOWN"
        assert pending["asset_bindings"]["reference_image"] == rejected_id
        submitted = deepcopy(comfy.prompts)
        state["offline"] = False
        response = client.post(f"/api/v1/episodes/{failed['id']}/generate")
        assert response.status_code == 202, response.text
        completed = wait_episode(client, failed["id"])
        recovered = app.state.store.get("job", pending["id"])
        assert recovered["status"] == "COMPLETED"
        for key in ("comfy_prompt_id", "patched_workflow", "asset_bindings"):
            assert recovered[key] == pending[key]
        for prompt_id, body in submitted.items():
            assert comfy.prompts[prompt_id] == body
            assert (
                sum(item["client_id"] == body["client_id"] for item in comfy.prompts.values()) == 1
            )
    assert completed["status"] == "COMPLETED", completed.get("error")
    ends = chronological_jobs(app, completed, "SHOT_END_FRAME")
    assert len(ends) == 2
    assert ends[-1]["asset_bindings"]["reference_image"] == rejected_id
    assert EDIT_REFERENCE in positive(ends[-1])
    assert completed["shots"][0]["start_frame_asset_id"] == good_start_id
    assert len(shot_jobs(app, completed, kind="SHOT_START_FRAME")) == 1
    assert completed["plan"] == reviewed["plan"]
    assert completed["shots"][0]["prompts"] == reviewed["shots"][0]["prompts"]
    for asset_id, content in original_images.items():
        response = client.get(f"/api/v1/assets/{asset_id}/file")
        assert response.status_code == 200 and response.content == content
