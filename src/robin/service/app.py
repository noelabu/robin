"""The local HTTP API under /api/v1, the event stream, and the bundled web client (TR-034/035, TR-050–052).

Errors carry a code, a safe message, a correlation ID and retry guidance. Asynchronous controls return
202 with the durable action record; a lost response is recovered with GET /actions/{id}, never by
sending the request again with a new ID.
"""

import asyncio
import json
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import Connection, func, select
from starlette.exceptions import HTTPException as StarletteHTTPException

from robin.events.ingest import ensure_session, read_events, read_payload_json
from robin.paths import ExecutionCwd
from robin.runtime.capabilities import RUNTIME_CAPABILITIES, capabilities_for
from robin.runtime.ledger import Action, ActionConflict, ActionRejected
from robin.service.auth import COOKIE, CSRF_HEADER, Auth
from robin.service.runtime import ServiceRuntime
from robin.store.models import Event
from robin.store.models import Session as SessionRow
from robin.workspace import tasks as ws

API = "/api/v1"
HEARTBEAT_S = 15.0
STREAM_BATCH = 200
PREVIEW_CHARS = 500
WEB_DIST = Path(__file__).resolve().parent.parent / "web" / "dist"


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, retry: str = "do_not_retry"):
        self.status, self.code, self.message, self.retry = status, code, message, retry


def _error(
    status: int, code: str, message: str, retry: str, detail: dict[str, Any] | None = None
) -> JSONResponse:
    error: dict[str, Any] = {
        "code": code,
        "message": message,
        "correlation_id": uuid.uuid4().hex[:12],
        "retry": retry,
    }
    if detail:
        error["detail"] = detail
    return JSONResponse(status_code=status, content={"error": error})


# -- request bodies -----------------------------------------------------------


class LaunchBody(BaseModel):
    runtime: str
    cwd: str | None = None  # defaults to the task's project folder
    task_id: str | None = None
    role: str | None = None
    prompt: str | None = None
    action_id: str = Field(min_length=8, max_length=128)


class ActionBody(BaseModel):
    type: Literal["message", "interrupt", "stop", "approval"]
    action_id: str = Field(min_length=8, max_length=128)
    text: str | None = None
    request_id: str | None = None
    allow: bool | None = None
    expected_generation: int | None = None


class ExchangeBody(BaseModel):
    secret: str


# -- views --------------------------------------------------------------------

CONTROL_TYPES = ("message", "interrupt", "stop", "approval")


def session_view(r: Any) -> dict[str, Any]:
    caps = capabilities_for(r["runtime"])
    controls = {}
    for action in CONTROL_TYPES:
        ok, reason = caps.allows(action)
        if ok and r["mode"] != "managed":
            ok, reason = False, "observed sessions are read-only in Robin"
        if ok and r["connection_state"] != "connected":
            ok, reason = False, f"session is {r['connection_state']}"
        controls[action] = {"enabled": ok, "reason": reason}
    return {
        "session_id": r["session_id"],
        "runtime": r["runtime"],
        "native_session_id": r["native_session_id"],
        "mode": r["mode"],
        "runtime_state": r["runtime_state"],
        "connection_state": r["connection_state"],
        "state_evidence_at": r["state_evidence_at"].isoformat() if r["state_evidence_at"] else None,
        "active_run_id": r["active_run_id"],
        "execution_cwd": r["execution_cwd"],
        "lease_generation": r["lease_generation"],
        "project_id": r["project_id"],
        "current_task_id": r["current_task_id"],
        "pending_task_id": r["pending_task_id"],
        "archived": bool(r["archived"]),
        "created_at": r["created_at"].isoformat(),
        "updated_at": r["updated_at"].isoformat(),
        "controls": controls,
        "capabilities": caps.report(),
    }


def action_view(a: Action) -> dict[str, Any]:
    return {
        "action_id": a.action_id,
        "session_id": a.session_id,
        "type": a.action_type,
        "state": a.state,
        "error": {"code": a.error_code, "message": a.error_message} if a.error_code else None,
        "native_correlation": a.native_correlation,
        "requested_at": a.requested_at.isoformat(),
        "updated_at": a.updated_at.isoformat(),
    }


def _sessions(conn: Connection, session_id: str | None = None) -> list[dict[str, Any]]:
    q = select(SessionRow).order_by(SessionRow.updated_at.desc())
    if session_id:
        q = q.where(SessionRow.session_id == session_id)
    return [session_view(r) for r in conn.execute(q).mappings()]


def create_app(runtime: ServiceRuntime, auth: Auth) -> FastAPI:
    store = runtime.store

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await runtime.start()
        try:
            yield
        finally:
            await runtime.stop()

    app = FastAPI(title="Robin", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        return _error(exc.status, exc.code, exc.message, exc.retry)

    @app.exception_handler(ws.WorkspaceError)
    async def _workspace_error(_: Request, exc: ws.WorkspaceError) -> JSONResponse:
        retry = "reload_state" if exc.status == 409 else "fix_request"
        return _error(exc.status, exc.code, str(exc), retry, exc.detail)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        fields = [".".join(str(p) for p in e["loc"][1:]) for e in exc.errors()]
        return _error(422, "invalid_request", f"invalid fields: {', '.join(fields)}", "fix_request")

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        return _error(
            exc.status_code,
            "not_found" if exc.status_code == 404 else "http_error",
            str(exc.detail),
            "do_not_retry",
        )

    @app.middleware("http")
    async def _guard(request: Request, call_next: Any) -> Response:
        """Loopback Host and same-origin only, on every request (TR-051)."""
        if not auth.host_ok(request.headers.get("host")):
            return _error(403, "bad_host", "requests must address this loopback service", "do_not_retry")
        if not auth.origin_ok(request.headers.get("origin")):
            return _error(403, "bad_origin", "cross-site requests are not allowed", "do_not_retry")
        response: Response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
        )
        return response

    def authenticated(request: Request) -> str:
        if auth.cli_ok(request.headers.get("authorization")):
            return "cli"
        s = auth.browser_session(request.cookies.get(COOKIE))
        if s is None:
            raise ApiError(
                401, "unauthenticated", "sign in with the URL printed by `robin`", "reauthenticate"
            )
        if request.method not in ("GET", "HEAD") and request.headers.get(CSRF_HEADER) != s.csrf:
            raise ApiError(403, "csrf", "missing or wrong CSRF token", "reauthenticate")
        return "browser"

    Authed = Depends(authenticated)

    # -- auth ------------------------------------------------------------------

    @app.post(f"{API}/auth/exchange")
    async def exchange(body: ExchangeBody, response: Response) -> dict[str, str]:
        result = auth.exchange(body.secret)
        if result is None:
            raise ApiError(
                401,
                "bootstrap_invalid",
                "this sign-in link was already used or has expired; run `robin open` for a new one",
                "reauthenticate",
            )
        sid, csrf = result
        response.set_cookie(COOKIE, sid, httponly=True, samesite="strict", secure=False, path="/")
        return {"csrf_token": csrf}

    @app.get(f"{API}/auth/session")
    async def current_session(request: Request) -> dict[str, str]:
        """For a page reload: the cookie is still valid, the CSRF token lived only in memory."""
        s = auth.browser_session(request.cookies.get(COOKIE))
        if s is None:
            raise ApiError(
                401, "unauthenticated", "sign in with the URL printed by `robin`", "reauthenticate"
            )
        return {"csrf_token": s.csrf}

    @app.post(f"{API}/auth/bootstrap")
    async def new_bootstrap(request: Request) -> dict[str, str]:
        """CLI only: mint a fresh single-use sign-in link for `robin open`."""
        if not auth.cli_ok(request.headers.get("authorization")):
            raise ApiError(401, "unauthenticated", "CLI token required", "reauthenticate")
        return {"url": f"http://127.0.0.1:{auth.port}/#bootstrap={auth.new_bootstrap()}"}

    # -- workspace -------------------------------------------------------------

    @app.get(f"{API}/workspace")
    async def workspace(_: str = Authed) -> dict[str, Any]:
        """Snapshot and watermark from one read transaction; subscribe to /stream after the watermark."""
        with store.reader.connect() as conn, conn.begin():
            watermark = conn.execute(select(func.coalesce(func.max(Event.global_cursor), 0))).scalar_one()
            sessions = _sessions(conn)
        return {
            "watermark": watermark,
            "sessions": sessions,
            "supervisor": {"connected": runtime.supervisor_connected},
        }

    @app.get(f"{API}/adapters")
    async def adapters(_: str = Authed) -> dict[str, Any]:
        return {"adapters": [c.report() for name, c in RUNTIME_CAPABILITIES.items() if name != "fake"]}

    @app.get(f"{API}/sessions")
    async def sessions(
        _: str = Authed,
        runtime_name: str | None = Query(None, alias="runtime"),
        state: str | None = None,
        project_id: str | None = None,
        task_id: str | None = None,
        include_archived: bool = False,
    ) -> dict[str, Any]:
        with store.reader.connect() as conn:
            rows = _sessions(conn)
        if not include_archived:
            rows = [r for r in rows if not r["archived"]]
        if project_id:
            rows = [r for r in rows if r["project_id"] == project_id]
        if task_id:
            rows = [r for r in rows if task_id in (r["current_task_id"], r["pending_task_id"])]
        if runtime_name:
            rows = [r for r in rows if r["runtime"] == runtime_name]
        if state:
            rows = [r for r in rows if r["runtime_state"] == state]
        return {"sessions": rows}

    @app.get(f"{API}/sessions/{{session_id}}")
    async def session(session_id: str, _: str = Authed) -> dict[str, Any]:
        with store.reader.connect() as conn:
            rows = _sessions(conn, session_id)
        if not rows:
            raise ApiError(404, "not_found", f"no session {session_id}")
        return rows[0]

    # -- controls --------------------------------------------------------------

    @app.post(f"{API}/sessions/launch", status_code=202)
    async def launch(body: LaunchBody, _: str = Authed) -> dict[str, Any]:
        existing = await runtime.manager.get_action(body.action_id)
        if existing is not None:  # a retried request: report, don't launch again
            return {"action": action_view(existing), "session_id": existing.session_id}
        task = project = None
        if body.task_id:
            try:
                task = await asyncio.to_thread(ws.get_task, store, body.task_id)
                project = await asyncio.to_thread(ws.get_project, store, task["project_id"])
            except ws.WorkspaceError as exc:
                raise ApiError(exc.status, exc.code, str(exc)) from exc
        raw_cwd = body.cwd or (project["path"] if project else None)
        if raw_cwd is None:
            raise ApiError(
                422, "invalid_cwd", "give cwd, or a task_id whose project folder to use", "fix_request"
            )
        cwd = Path(raw_cwd).expanduser()
        if not cwd.is_absolute() or not cwd.is_dir():
            raise ApiError(422, "invalid_cwd", "cwd must be an existing absolute directory", "fix_request")
        try:
            checked = ExecutionCwd.checked(cwd, store.artifacts)
        except ValueError as exc:
            raise ApiError(422, "invalid_cwd", str(exc), "fix_request") from exc
        caps = capabilities_for(body.runtime)
        ok, reason = caps.allows("launch")
        if not ok:
            raise ApiError(409, "unsupported", reason or "unsupported")
        sid = await asyncio.to_thread(
            ensure_session, store, runtime=body.runtime, mode="managed", execution_cwd=str(checked.path)
        )
        # Work for a task always starts a new native session bound to it (FR-013).
        await asyncio.to_thread(
            ws.bind_new_session,
            store,
            sid,
            task_id=body.task_id,
            project_id=project["project_id"] if project else None,
            role=body.role,
        )
        action = await _run(
            runtime.manager.launch(
                sid, runtime=body.runtime, cwd=str(checked.path), action_id=body.action_id, prompt=body.prompt
            )
        )
        return {"action": action_view(action), "session_id": sid}

    @app.post(f"{API}/sessions/{{session_id}}/actions", status_code=202)
    async def act(session_id: str, body: ActionBody, _: str = Authed) -> dict[str, Any]:
        m = runtime.manager
        if body.type == "message":
            if not body.text:
                raise ApiError(422, "invalid_request", "message needs text", "fix_request")
            coro = m.message(
                session_id, body.text, action_id=body.action_id, expected_generation=body.expected_generation
            )
        elif body.type == "interrupt":
            coro = m.interrupt(
                session_id, action_id=body.action_id, expected_generation=body.expected_generation
            )
        elif body.type == "stop":
            coro = m.stop(session_id, action_id=body.action_id)
        else:
            if not body.request_id or body.allow is None:
                raise ApiError(422, "invalid_request", "approval needs request_id and allow", "fix_request")
            coro = m.respond(session_id, body.request_id, body.allow, action_id=body.action_id)
        return {"action": action_view(await _run(coro))}

    async def _run(coro: Any) -> Action:
        try:
            result: Action = await coro
        except ActionConflict as exc:
            raise ApiError(409, "action_id_conflict", str(exc)) from exc
        except ActionRejected as exc:
            status = 404 if exc.code == "unknown_session" else 409
            raise ApiError(status, exc.code, str(exc), "reload_state") from exc
        return result

    class ShutdownBody(BaseModel):
        confirm_sessions: list[str]

    @app.post(f"{API}/supervisor/shutdown")
    async def supervisor_shutdown(body: ShutdownBody, _: str = Authed) -> dict[str, Any]:
        """Stopping the supervisor ends its sessions, so the request names exactly those (TR-072)."""
        client = runtime.manager.client
        if client is None:
            raise ApiError(503, "supervisor_unavailable", "no supervisor connection", "retry_later")
        reply = await client.request({"type": "shutdown", "confirm_sessions": body.confirm_sessions})
        if not reply.get("ok"):
            raise ApiError(
                409, str(reply.get("error")), f"would end: {reply.get('would_end')}", "fix_request"
            )
        return {"ending": reply.get("ending", [])}

    @app.get(f"{API}/actions/{{action_id}}")
    async def get_action(action_id: str, _: str = Authed) -> dict[str, Any]:
        action = await runtime.manager.get_action(action_id)
        if action is None:
            raise ApiError(404, "not_found", f"no action {action_id}")
        return action_view(action)

    # -- events ----------------------------------------------------------------

    @app.get(f"{API}/events")
    async def events(
        _: str = Authed,
        after: int = Query(0, ge=0),
        limit: int = Query(100, ge=1, le=500),
        session_id: str | None = None,
    ) -> dict[str, Any]:
        evs = await asyncio.to_thread(read_events, store, session_id=session_id, after=after, limit=limit)
        items = [e.model_dump(mode="json") for e in evs]
        return {"events": items, "next_after": items[-1]["global_cursor"] if items else after}

    @app.get(f"{API}/stream")
    async def stream(
        request: Request, _: str = Authed, after: int | None = Query(None, ge=0)
    ) -> StreamingResponse:
        last_id = request.headers.get("last-event-id")
        cursor = after if after is not None else int(last_id) if last_id and last_id.isdigit() else 0
        return StreamingResponse(
            _sse(request, cursor), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"}
        )

    async def _sse(request: Request, cursor: int) -> AsyncIterator[bytes]:
        """Events after the cursor, in commit order, read from the database (TR-034, TR-035).

        Reading from the database rather than an in-memory queue keeps per-client buffers bounded and
        means a slow client resumes where it was instead of losing history.
        """
        with store.reader.connect() as conn:
            head = conn.execute(select(func.coalesce(func.max(Event.global_cursor), 0))).scalar_one()
        if cursor > head:
            yield _frame("resync_required", {"reason": "cursor is ahead of this store", "head": head})
            return
        while not await request.is_disconnected():
            seen = runtime.version  # taken before the read, so a commit during the read isn't missed
            evs = await asyncio.to_thread(read_events, store, after=cursor, limit=STREAM_BATCH)
            if evs:
                touched = {e.session_id for e in evs}
                with store.reader.connect() as conn:
                    views = {s: _sessions(conn, s)[0] for s in touched}
                for e in evs:
                    data = {
                        "event": e.model_dump(mode="json"),
                        "session": views[e.session_id],
                        "preview": await asyncio.to_thread(_preview, e.global_cursor, e.payload_ref),
                    }
                    yield _frame("event", data, event_id=e.global_cursor)
                cursor = evs[-1].global_cursor
                continue
            if await runtime.wait_change(seen, HEARTBEAT_S) == seen:
                yield b": heartbeat\n\n"  # proves the Robin connection is alive, not that agents work

    def _preview(cursor: int, payload_ref: str | None) -> str | None:
        """A short plain-text summary for the activity feed. Large payloads stay behind their reference."""
        if payload_ref is not None:
            return None
        try:
            payload = read_payload_json(store, cursor)
        except ValueError:
            return None
        if not isinstance(payload, dict):
            return None
        for key in ("text", "reason", "result", "output"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:PREVIEW_CHARS]
        tool = payload.get("tool")
        if isinstance(tool, str):
            detail = payload.get("input")
            command = detail.get("command") if isinstance(detail, dict) else None
            return f"{tool}: {command}"[:PREVIEW_CHARS] if isinstance(command, str) else tool
        return None

    def _frame(kind: str, data: dict[str, Any], event_id: int | None = None) -> bytes:
        head = f"id: {event_id}\n" if event_id is not None else ""
        return f"{head}event: {kind}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()

    @app.get(f"{API}/health")
    async def health(_: str = Authed) -> dict[str, Any]:
        return {"service": "ready", "supervisor_connected": runtime.supervisor_connected, "pid": os.getpid()}

    from robin.service import routes_attention, routes_workspace

    routes_workspace.register(app, store, Authed, API)
    routes_attention.register(app, runtime, Authed, API, _error)

    # -- web client (mounted last: it matches every path) ----------------------

    if WEB_DIST.exists():
        app.mount("/", StaticFiles(directory=WEB_DIST, html=True), name="web")
    else:

        @app.get("/")
        async def no_web() -> dict[str, str]:
            return {"robin": "service is running; the web client isn't built (cd web && npm run build)"}

    return app
