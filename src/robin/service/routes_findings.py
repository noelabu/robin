"""Findings under /api/v1 (FR-020, TR-060, TR-067)."""

import asyncio
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from robin.findings import store as findings
from robin.service.runtime import ServiceRuntime


class FindingBody(BaseModel):
    """What a user writes. Identity fields (author, ID, version, time) are Robin's and can't be supplied."""

    model_config = ConfigDict(extra="ignore")
    task_id: str
    submission_id: str | None = Field(default=None, max_length=128)
    summary: str
    claims: list[dict[str, Any]]
    recommendations: list[str] = []
    questions: list[str] = []
    parents: list[str] = []


class RevisionBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    expected_version: int
    summary: str
    claims: list[dict[str, Any]]
    recommendations: list[str] = []
    questions: list[str] = []
    parents: list[str] = []


def register(app: FastAPI, runtime: ServiceRuntime, authed: Any, api: str, error: Any) -> None:
    store = runtime.store

    @app.exception_handler(findings.FindingError)
    async def _finding_error(_: Request, exc: findings.FindingError) -> JSONResponse:
        resp: JSONResponse = error(
            exc.status, exc.code, str(exc), "fix_request" if exc.status == 422 else "reload_state"
        )
        return resp

    @app.get(f"{api}/findings")
    async def list_(_: str = authed, task_id: str | None = None) -> dict[str, Any]:
        return {"findings": await asyncio.to_thread(findings.list_findings, store, task_id)}

    @app.get(f"{api}/findings/rejections")
    async def rejections(_: str = authed, task_id: str | None = None) -> dict[str, Any]:
        return {"rejections": await asyncio.to_thread(findings.list_rejections, store, task_id)}

    @app.get(f"{api}/artifacts")
    async def artifacts(task_id: str, _: str = authed, kind: str | None = None) -> dict[str, Any]:
        return {"artifacts": await asyncio.to_thread(findings.list_artifacts, store, task_id, kind)}

    @app.get(f"{api}/artifacts/{{artifact_id}}")
    async def artifact(artifact_id: str, _: str = authed, version: int | None = None) -> dict[str, Any]:
        return dict(await asyncio.to_thread(findings.get_artifact, store, artifact_id, version))

    @app.get(f"{api}/findings/{{finding_id}}")
    async def get(finding_id: str, _: str = authed, version: int | None = None) -> dict[str, Any]:
        return dict(await asyncio.to_thread(findings.get, store, finding_id, version))

    @app.post(f"{api}/findings", status_code=201)
    async def publish(body: FindingBody, _: str = authed) -> dict[str, Any]:
        payload = body.model_dump(exclude={"task_id"}, exclude_none=True)
        result = await asyncio.to_thread(findings.publish_user, store, body.task_id, payload)
        await runtime.notify()
        return dict(result)

    @app.post(f"{api}/findings/{{finding_id}}/versions", status_code=201)
    async def revise(finding_id: str, body: RevisionBody, _: str = authed) -> dict[str, Any]:
        payload = body.model_dump(exclude={"expected_version"})
        result = await asyncio.to_thread(findings.revise, store, finding_id, payload, body.expected_version)
        await runtime.notify()
        return dict(result)
