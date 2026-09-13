import json

from fastapi import APIRouter, Query, Request
from fastapi.responses import Response

from app.creation.schemas import (
    ConfirmProduction,
    CreateProject,
    CreationPackage,
    SaveRevision,
    SubmitRevision,
)

router = APIRouter(prefix="/api/v1/creation", tags=["creation"])


def attachment(value, filename):
    return Response(
        json.dumps(value, ensure_ascii=False, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/schema")
def schema():
    return CreationPackage.model_json_schema()


@router.post("/normalize")
def normalize(body: CreationPackage):
    """Validate file structure and fill defaults without saving or invoking any service."""
    return body.model_dump(mode="json")


@router.get("/projects")
def projects(request: Request):
    return request.app.state.creation.projects()


@router.post("/projects", status_code=201)
def create_project(request: Request, body: CreateProject):
    return request.app.state.creation.create(body)


@router.get("/projects/{id}")
def detail(request: Request, id: str):
    return request.app.state.creation.detail(id)


@router.get("/projects/{id}/context")
def context(request: Request, id: str, download: bool = False):
    value = request.app.state.creation.context(id)
    if download:
        return attachment(value, f"creation-context-{id}-v{value['revision']}.json")
    return value


@router.get("/projects/{id}/export")
def export(
    request: Request,
    id: str,
    revision: int | None = Query(default=None, ge=1),
    download: bool = False,
):
    saved = request.app.state.creation.revision(id, revision)
    if download:
        return attachment(saved["document"], f"creation-{id}-v{saved['revision']}.json")
    return saved["document"]


@router.post("/projects/{id}/revisions")
def save(request: Request, id: str, body: SaveRevision):
    return request.app.state.creation.save(id, body)


@router.post("/projects/{id}/validate")
def validate(request: Request, id: str, revision: int | None = Query(default=None, ge=1)):
    return request.app.state.creation.validate(id, revision)


@router.post("/projects/{id}/submit", status_code=201)
def submit(request: Request, id: str, body: SubmitRevision):
    return request.app.state.creation.submit(id, body)


@router.get("/projects/{id}/productions/{episode_id}")
def feedback(request: Request, id: str, episode_id: str):
    return request.app.state.creation.feedback(id, episode_id)


@router.post("/projects/{id}/productions/{episode_id}/confirm", status_code=202)
async def confirm(request: Request, id: str, episode_id: str, body: ConfirmProduction):
    return request.app.state.creation.confirm(id, episode_id, body)


@router.post("/projects/{id}/productions/{episode_id}/cancel")
async def cancel(request: Request, id: str, episode_id: str):
    return await request.app.state.creation.cancel(id, episode_id)
