"""Projects, tasks and task-session routes under /api/v1 (FR-001–003, FR-013, FR-014, FR-042, TR-050)."""

import asyncio
from typing import Any

from fastapi import FastAPI, Query
from pydantic import BaseModel, Field

from robin.store import Store
from robin.workspace import tasks as ws


class ProjectBody(BaseModel):
    path: str
    name: str | None = None


class TaskBody(BaseModel):
    project_id: str
    title: str = Field(min_length=1, max_length=200)
    objective: str = Field(min_length=1)
    files: list[str] = []
    instructions: list[str] = []
    expected_outputs: list[str] = []


class TaskPatch(BaseModel):
    expected_revision: int
    title: str | None = None
    objective: str | None = None
    status: str | None = Field(default=None, pattern="^(open|completed|abandoned)$")
    completion_evidence: str | None = None
    archived: bool | None = None


class AttachBody(BaseModel):
    session_id: str
    role: str | None = None
    confirm_retained_context: bool = False


class SessionPatch(BaseModel):
    archived: bool


def register(app: FastAPI, store: Store, authed: Any, api: str) -> None:
    def run(fn: Any, *args: Any, **kw: Any) -> Any:
        return asyncio.to_thread(fn, store, *args, **kw)

    @app.post(f"{api}/projects", status_code=201)
    async def register_project(body: ProjectBody, _: str = authed) -> dict[str, Any]:
        return dict(await run(ws.register_project, body.path, body.name))

    @app.get(f"{api}/projects")
    async def projects(_: str = authed) -> dict[str, Any]:
        return {"projects": await run(ws.list_projects)}

    @app.get(f"{api}/projects/{{project_id}}")
    async def project(project_id: str, _: str = authed) -> dict[str, Any]:
        return dict(await run(ws.get_project, project_id))

    @app.post(f"{api}/tasks", status_code=201)
    async def create_task(body: TaskBody, _: str = authed) -> dict[str, Any]:
        return dict(
            await run(
                ws.create_task,
                project_id=body.project_id,
                title=body.title,
                objective=body.objective,
                files=body.files,
                instructions=body.instructions,
                expected_outputs=body.expected_outputs,
            )
        )

    @app.get(f"{api}/tasks")
    async def tasks(
        _: str = authed, project_id: str | None = None, include_archived: bool = Query(False)
    ) -> dict[str, Any]:
        return {"tasks": await run(ws.list_tasks, project_id=project_id, include_archived=include_archived)}

    @app.get(f"{api}/tasks/{{task_id}}")
    async def task(task_id: str, _: str = authed) -> dict[str, Any]:
        return dict(await run(ws.get_task, task_id))

    @app.patch(f"{api}/tasks/{{task_id}}")
    async def patch_task(task_id: str, body: TaskPatch, _: str = authed) -> dict[str, Any]:
        changes = body.model_dump(exclude={"expected_revision"}, exclude_none=True)
        return dict(await run(ws.update_task, task_id, expected_revision=body.expected_revision, **changes))

    @app.post(f"{api}/tasks/{{task_id}}/sessions")
    async def attach(task_id: str, body: AttachBody, _: str = authed) -> dict[str, Any]:
        return dict(
            await run(
                ws.attach_session,
                task_id,
                body.session_id,
                role=body.role,
                confirm_retained_context=body.confirm_retained_context,
            )
        )

    @app.patch(f"{api}/sessions/{{session_id}}")
    async def patch_session(session_id: str, body: SessionPatch, _: str = authed) -> dict[str, Any]:
        await run(ws.archive_session, session_id, body.archived)
        return {"session_id": session_id, "archived": body.archived}
