"""Context transfer under /api/v1 (FR-021–024, TR-061–065)."""

import asyncio
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from robin.context import packets
from robin.runtime.ledger import ActionConflict, ActionRejected
from robin.service.runtime import ServiceRuntime


class FindingRef(BaseModel):
    id: str
    version: int | None = None  # None: the latest version at preview time, then pinned
    claims: list[int] | None = None  # selected claim numbers (1-based); None: all of them


class ExcerptRef(BaseModel):
    session_id: str
    after: int = Field(ge=0)
    through: int | None = None


class PreviewBody(BaseModel):
    task_id: str
    target_session_id: str
    question: str = Field(min_length=1, max_length=20_000)
    findings: list[FindingRef] = []
    excerpts: list[ExcerptRef] = []
    attachments: list[str] = []
    include_objective: bool = True


class ConfirmBody(BaseModel):
    payload_hash: str


class SaveReplyBody(BaseModel):
    cursor: int | None = None  # the reply's event cursor; default: the latest reply since delivery


class DeliverBody(BaseModel):
    action_id: str = Field(min_length=8, max_length=128)


def register(app: FastAPI, runtime: ServiceRuntime, authed: Any, api: str, error: Any) -> None:
    store = runtime.store

    @app.exception_handler(packets.ContextError)
    async def _context_error(_: Request, exc: packets.ContextError) -> JSONResponse:
        resp: JSONResponse = error(
            exc.status, exc.code, str(exc), "fix_request" if exc.status != 409 else "reload_state"
        )
        if exc.detail:
            import json

            body = json.loads(bytes(resp.body))
            body["error"]["detail"] = exc.detail
            resp = JSONResponse(body, status_code=exc.status)
        return resp

    @app.post(f"{api}/context/preview", status_code=201)
    async def preview(body: PreviewBody, _: str = authed) -> dict[str, Any]:
        sel = packets.Selection(
            task_id=body.task_id,
            target_session_id=body.target_session_id,
            question=body.question,
            findings=tuple(
                (f.id, f.version, tuple(f.claims) if f.claims is not None else None) for f in body.findings
            ),
            excerpts=tuple((e.session_id, e.after, e.through) for e in body.excerpts),
            attachments=tuple(body.attachments),
            include_objective=body.include_objective,
        )
        return dict(await asyncio.to_thread(packets.preview, store, sel))

    @app.get(f"{api}/context")
    async def list_(_: str = authed, task_id: str | None = None) -> dict[str, Any]:
        return {"packets": await asyncio.to_thread(packets.list_packets, store, task_id)}

    @app.get(f"{api}/context/{{packet_id}}")
    async def get(packet_id: str, _: str = authed) -> dict[str, Any]:
        return dict(await asyncio.to_thread(packets.get, store, packet_id))

    @app.post(f"{api}/context/{{packet_id}}/confirm")
    async def confirm(packet_id: str, body: ConfirmBody, _: str = authed) -> dict[str, Any]:
        return dict(await asyncio.to_thread(packets.confirm, store, packet_id, body.payload_hash))

    @app.post(f"{api}/context/{{packet_id}}/save-reply", status_code=201)
    async def save_reply(packet_id: str, body: SaveReplyBody, _: str = authed) -> dict[str, Any]:
        result = await asyncio.to_thread(packets.save_reply, store, packet_id, body.cursor)
        await runtime.notify()
        return dict(result)

    @app.post(f"{api}/context/{{packet_id}}/deliver", status_code=202)
    async def deliver(packet_id: str, body: DeliverBody, _: str = authed) -> Any:
        try:
            result = await packets.deliver(store, runtime.manager, packet_id, body.action_id)
        except (ActionRejected, ActionConflict) as exc:
            return error(409, getattr(exc, "code", "action_id_conflict"), str(exc), "reload_state")
        await runtime.notify()
        return result
