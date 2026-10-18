"""Continuing a session in another runtime, under /api/v1."""

import asyncio
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from robin.continuation import flow
from robin.runtime.ledger import ActionConflict, ActionRejected
from robin.service.runtime import ServiceRuntime


class PreviewBody(BaseModel):
    runtime: str = Field(min_length=1, max_length=32)  # the flow refuses runtimes it can't write


class ContinueBody(BaseModel):
    runtime: str = Field(min_length=1, max_length=32)
    text: str = Field(max_length=100_000)
    preview_hash: str = Field(min_length=8, max_length=200)
    action_id: str = Field(min_length=8, max_length=128)


def register(
    app: FastAPI, runtime: ServiceRuntime, authed: Any, api: str, error: Any, action_view: Any
) -> None:
    store = runtime.store

    @app.exception_handler(flow.ContinueError)
    async def _continue_error(_: Request, exc: flow.ContinueError) -> JSONResponse:
        resp: JSONResponse = error(
            exc.status, exc.code, str(exc), "reload_state" if exc.status == 409 else "fix_request"
        )
        return resp

    @app.post(f"{api}/sessions/{{session_id}}/continue/preview")
    async def preview(session_id: str, body: PreviewBody, _: str = authed) -> dict[str, Any]:
        return await asyncio.to_thread(flow.preview, store, session_id, body.runtime)

    @app.post(f"{api}/sessions/{{session_id}}/continue", status_code=202)
    async def continue_(session_id: str, body: ContinueBody, _: str = authed) -> Any:
        try:
            result = await flow.continue_(
                store, runtime.manager, session_id, body.runtime, body.text, body.preview_hash, body.action_id
            )
        except ActionConflict as exc:
            return error(409, "action_id_conflict", str(exc), "reload_state")
        except ActionRejected as exc:
            return error(409, exc.code, str(exc), "reload_state")
        await runtime.notify()
        return {**result, "action": action_view(result["action"]) if result["action"] else None}
