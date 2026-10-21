"""The attention queue under /api/v1 (FR-017, FR-035, TR-045)."""

import asyncio
import time
from typing import Any, Literal

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, model_validator

from robin.runtime import attention
from robin.runtime.ledger import ActionConflict, ActionRejected
from robin.service.runtime import ServiceRuntime


class ResponseBody(BaseModel):
    decision: Literal["allow", "deny", "answer", "terminal"]
    action_id: str = Field(min_length=8, max_length=128)
    message: str | None = None
    # "answer": {question text: answer}, a list of picks for a multi-select question
    answers: dict[str, str | list[str]] | None = None

    @model_validator(mode="after")
    def _answers_go_with_answer(self) -> "ResponseBody":
        if (self.decision == "answer") != (self.answers is not None):
            raise ValueError('answers are sent with decision "answer", and only with it')
        return self


def register(app: FastAPI, runtime: ServiceRuntime, authed: Any, api: str, error: Any) -> None:
    store, manager = runtime.store, runtime.manager

    @app.exception_handler(attention.AttentionError)
    async def _attention_error(_: Request, exc: attention.AttentionError) -> JSONResponse:
        return error(exc.status, exc.code, str(exc), "reload_state")

    @app.get(f"{api}/attention")
    async def items(_: str = authed, state: str = "active", session_id: str | None = None) -> dict[str, Any]:
        states = attention.ACTIVE if state == "active" else tuple(state.split(","))
        return {
            "items": await asyncio.to_thread(
                attention.list_items, store, states=states, session_id=session_id
            )
        }

    @app.get(f"{api}/attention/{{attention_id}}")
    async def item(attention_id: str, _: str = authed) -> dict[str, Any]:
        return dict(await asyncio.to_thread(attention.get, store, attention_id))

    @app.post(f"{api}/attention/{{attention_id}}/responses", status_code=202)
    async def respond(attention_id: str, body: ResponseBody, _: str = authed) -> Any:
        """Only an explicit response reaches the runtime, once (TR-045)."""
        existing = await manager.get_action(body.action_id)
        if existing is not None:  # a retried request: report, never answer twice
            return {"action": _action(existing)}
        current = await asyncio.to_thread(attention.get, store, attention_id)
        answers = None
        if body.answers is not None:
            questions = current["detail"].get("questions") or []
            answers = attention.normalize_answers(questions, body.answers)
        if current["detail"].get("answer_in") == "hook":  # a terminal session's hook waits for this
            try:
                hooked = await asyncio.to_thread(
                    attention.reply_hook,
                    store,
                    attention_id,
                    body.action_id,
                    body.decision,
                    answers,
                    body.message,
                )
            except ActionConflict as exc:
                return error(409, "action_id_conflict", str(exc), "reload_state")
            await _ended(attention_id)
            return {"action": _action(hooked)}
        it = await asyncio.to_thread(attention.claim, store, attention_id, body.action_id, body.decision)
        native_request = await asyncio.to_thread(_request_id, store, attention_id)
        declined = it["kind"] == "question" and body.decision == "deny"
        try:
            action = await manager.respond(
                it["session_id"],
                native_request,
                body.decision != "deny",
                action_id=body.action_id,
                message=body.message or ("The user declined to answer" if declined else None),
                answers=answers,
            )
        except (ActionRejected, ActionConflict) as exc:
            await asyncio.to_thread(attention.release, store, attention_id, body.action_id)
            return error(409, getattr(exc, "code", "action_id_conflict"), str(exc), "reload_state")
        if action.state == "failed":  # never reached the runtime: the request can be answered again
            await asyncio.to_thread(attention.release, store, attention_id, body.action_id)
        await runtime.notify()
        return {"action": _action(action)}

    @app.get(f"{api}/attention/{{attention_id}}/reply")
    async def reply(
        attention_id: str, wait: float = Query(25, ge=0, le=30), _: str = authed
    ) -> dict[str, Any]:
        """For Robin's question hook: the reply to the question it waits on, waiting up to `wait` seconds."""
        give_up = time.monotonic() + wait
        while True:
            seen = runtime.version
            out = await asyncio.to_thread(attention.hook_outcome, store, attention_id)
            if out["status"] != "pending":
                if out["status"] == "timeout":
                    await _ended(attention_id)
                return out
            left = min(give_up - time.monotonic(), float(out["until"] or 0) - time.time())
            if left <= 0:
                if time.monotonic() >= give_up:
                    return out
                continue  # the deadline passed: the next look hands it to the terminal
            await runtime.wait_change(seen, left)

    @app.post(f"{api}/attention/{{attention_id}}/release")
    async def release(attention_id: str, _: str = authed) -> dict[str, Any]:
        """Robin's question hook is going away: an unanswered question goes back to the terminal."""
        out = await asyncio.to_thread(attention.release_hook, store, attention_id)
        await _ended(attention_id)
        return out

    async def _ended(attention_id: str) -> None:
        from robin.hooks import ingest as hook_ingest

        await asyncio.to_thread(hook_ingest.ask_ended, store, attention_id)
        await runtime.notify()

    @app.post(f"{api}/attention/{{attention_id}}/acknowledge")
    async def acknowledge(attention_id: str, _: str = authed) -> dict[str, Any]:
        result = await asyncio.to_thread(attention.set_notice, store, attention_id, "acknowledged")
        await runtime.notify()
        return dict(result)

    @app.post(f"{api}/attention/{{attention_id}}/dismiss")
    async def dismiss(attention_id: str, _: str = authed) -> dict[str, Any]:
        """Hides the notice. It does not answer the request: the agent keeps waiting."""
        result = await asyncio.to_thread(attention.set_notice, store, attention_id, "dismissed")
        await runtime.notify()
        return dict(result)


def _request_id(store: Any, attention_id: str) -> str:
    from sqlalchemy import select

    from robin.store.models import AttentionItem

    with store.reader.connect() as conn:
        return str(
            conn.execute(
                select(AttentionItem.native_request_id).where(AttentionItem.attention_id == attention_id)
            ).scalar()
        )


def _action(a: Any) -> dict[str, Any]:
    return {
        "action_id": a.action_id,
        "state": a.state,
        "type": a.action_type,
        "error": {"code": a.error_code, "message": a.error_message} if a.error_code else None,
    }
