"""Agent tools' API (/api/v1/agent/*) and the user's retrieval grants (/api/v1/grants) (TR-066, TR-068).

Agent routes take only a session's agent credential. It names the session; nothing in a request can name
another one, and the credential never works on the user's routes (it isn't the CLI token or a browser
session).
"""

import asyncio
from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from robin import retrieval
from robin.service.runtime import ServiceRuntime


class ArtifactRef(BaseModel):
    id: str
    version: int | None = None


class ExcerptRange(BaseModel):
    session_id: str
    after: int = Field(ge=0)
    through: int = Field(ge=1)


class GrantBody(BaseModel):
    session_id: str
    artifacts: list[ArtifactRef] = []
    excerpts: list[ExcerptRange] = []


class SearchBody(BaseModel):
    query: str = Field(min_length=1, max_length=500)


class ExcerptBody(BaseModel):
    session_id: str
    after: int = Field(ge=0)
    through: int = Field(ge=1)


class PublishBody(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=256 * 1024)
    inputs: list[str] = []


def register(app: FastAPI, runtime: ServiceRuntime, authed: Any, api: str, error: Any) -> None:
    store = runtime.store

    @app.exception_handler(retrieval.RetrievalError)
    async def _retrieval_error(_: Request, exc: retrieval.RetrievalError) -> JSONResponse:
        resp: JSONResponse = error(exc.status, exc.code, str(exc), "fix_request")
        return resp

    async def agent_session(request: Request) -> str:
        header = request.headers.get("authorization", "")
        token = header[7:].strip() if header.lower().startswith("bearer ") else ""
        sid = await asyncio.to_thread(retrieval.session_for, store, token)
        if sid is None:
            raise retrieval.RetrievalError("unauthenticated", "agent credential required", 401)
        return sid

    Agent = Depends(agent_session)

    # -- the agent's tools -----------------------------------------------------------------------------

    @app.get(f"{api}/agent/task")
    async def task_get(sid: str = Agent) -> dict[str, Any]:
        return dict(await asyncio.to_thread(retrieval.task_get, store, sid))

    @app.get(f"{api}/agent/findings")
    async def finding_list(sid: str = Agent) -> dict[str, Any]:
        return {"findings": await asyncio.to_thread(retrieval.finding_list, store, sid)}

    @app.get(f"{api}/agent/findings/{{finding_id}}")
    async def finding_read(finding_id: str, sid: str = Agent) -> dict[str, Any]:
        return dict(await asyncio.to_thread(retrieval.finding_read, store, sid, finding_id))

    @app.post(f"{api}/agent/search")
    async def search(body: SearchBody, sid: str = Agent) -> dict[str, Any]:
        return dict(await asyncio.to_thread(retrieval.search, store, sid, body.query))

    @app.post(f"{api}/agent/excerpt")
    async def excerpt(body: ExcerptBody, sid: str = Agent) -> dict[str, Any]:
        return dict(
            await asyncio.to_thread(
                retrieval.read_excerpt, store, sid, body.session_id, body.after, body.through
            )
        )

    @app.post(f"{api}/agent/artifacts", status_code=201)
    async def publish(body: PublishBody, sid: str = Agent) -> dict[str, Any]:
        result = await asyncio.to_thread(
            retrieval.artifact_publish, store, sid, body.title, body.text, body.inputs
        )
        await runtime.notify()
        return dict(result)

    # -- the user's grants -----------------------------------------------------------------------------

    @app.post(f"{api}/grants", status_code=201)
    async def grant(body: GrantBody, _: str = authed) -> dict[str, Any]:
        grants = await asyncio.to_thread(
            lambda: retrieval.grant(
                store,
                body.session_id,
                artifacts=tuple((a.id, a.version) for a in body.artifacts),
                excerpts=tuple((e.session_id, e.after, e.through) for e in body.excerpts),
            )
        )
        return {"grants": grants}

    @app.get(f"{api}/grants")
    async def grants(session_id: str, _: str = authed, include_revoked: bool = False) -> dict[str, Any]:
        return {
            "grants": await asyncio.to_thread(retrieval.list_grants, store, session_id, include_revoked),
            "reads": await asyncio.to_thread(retrieval.reads, store, session_id),
        }

    @app.delete(f"{api}/grants/{{grant_id}}")
    async def revoke(grant_id: str, _: str = authed) -> dict[str, Any]:
        await asyncio.to_thread(retrieval.revoke, store, grant_id)
        return {"revoked": grant_id}
