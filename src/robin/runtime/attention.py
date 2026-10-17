"""The attention queue (FR-017, FR-035, TR-045).

Items are projected from committed host events. A request expires when the runtime says so or, where it
has no expiry, when its run ends, is interrupted, or its host goes away; an expired item can't be answered.
Answering claims the item atomically, so one request gets one response. Acknowledging or dismissing changes
the notice only: it never approves anything. A new failure reopens the session's failure item.
"""

import json
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, and_, insert, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from robin.ids import new_id, utc_now
from robin.store import Store
from robin.store.models import AttentionItem, AuditEntry
from robin.store.models import Session as SessionRow

ACTIVE = ("open", "acknowledged")
TURN_ENDS = {"turn_completed", "turn_interrupted", "turn_failed", "host_exited", "session_stopping"}
FAILURES = {"turn_failed", "host_error"}
DETAIL_CHARS = 2000


class AttentionError(Exception):
    def __init__(self, code: str, message: str, status: int = 409):
        super().__init__(message)
        self.code, self.status = code, status


def _summary(value: Any) -> Any:
    text = json.dumps(value, ensure_ascii=False, default=str)
    return value if len(text) <= DETAIL_CHARS else text[:DETAIL_CHARS] + "…"


def project(conn: Connection, session_id: str, cursor: int, at: datetime, ev: dict[str, Any]) -> None:
    """Apply one committed host event to the queue. Idempotent per event."""
    kind = ev.get("type")
    if kind in ("approval_request", "question"):
        s = conn.execute(
            select(SessionRow.current_task_id, SessionRow.active_run_id).where(
                SessionRow.session_id == session_id
            )
        ).first()
        tool = ev.get("tool") or "a tool"
        conn.execute(
            sqlite_insert(AttentionItem)
            .values(
                attention_id=new_id(),
                kind="approval" if kind == "approval_request" else "question",
                session_id=session_id,
                task_id=s.current_task_id if s else None,
                run_id=s.active_run_id if s else None,
                native_request_id=ev.get("request_id"),
                state="open",
                title=f"{tool} needs approval"
                if kind == "approval_request"
                else str(ev.get("text") or "Question"),
                detail=json.dumps(
                    {"tool": tool, "input": _summary(ev.get("input"))}, ensure_ascii=False, default=str
                ),
                allowed_responses=json.dumps(["allow", "deny"] if kind == "approval_request" else []),
                originating_cursor=cursor,
                created_at=at,
                updated_at=at,
            )
            .on_conflict_do_nothing(index_elements=["session_id", "native_request_id"])
        )
    elif kind == "approval_answered":
        _set_request(
            conn,
            session_id,
            ev.get("request_id"),
            "resolved",
            at,
            resolution="allowed" if ev.get("allow") else "denied",
        )
    elif kind in ("approval_expired", "approval_rejected"):
        _set_request(
            conn, session_id, ev.get("request_id"), "expired", at, resolution=str(ev.get("reason") or kind)
        )

    if kind in TURN_ENDS:  # requests without native expiry end with their run (TR-045)
        conn.execute(
            update(AttentionItem)
            .where(
                AttentionItem.session_id == session_id,
                AttentionItem.kind.in_(("approval", "question")),
                AttentionItem.state.in_(ACTIVE),
            )
            .values(state="expired", resolution="run ended", updated_at=at)
        )
    if kind in FAILURES:
        _failure(conn, session_id, cursor, at, ev)


# Hook names that end an observed session's open questions: the turn ended, a new prompt came, it stopped.
HOOK_TURN_ENDS = {"Stop", "agent_end", "UserPromptSubmit", "turn_start", "SessionEnd", "session_shutdown"}
QUESTION_TOOLS = {"AskUserQuestion"}


def project_hook(
    conn: Connection, session_id: str, cursor: int, at: datetime, payload: dict[str, Any]
) -> None:
    """One hook from a session started outside Robin. Its questions show here, answered in its terminal."""
    name = payload.get("hook_event_name")
    tool_use = payload.get("tool_use_id")
    if name == "PreToolUse" and payload.get("tool_name") in QUESTION_TOOLS and tool_use:
        questions = _questions(payload.get("tool_input"))
        task = conn.execute(
            select(SessionRow.current_task_id).where(SessionRow.session_id == session_id)
        ).scalar()
        conn.execute(
            sqlite_insert(AttentionItem)
            .values(
                attention_id=new_id(),
                kind="question",
                session_id=session_id,
                task_id=task,
                native_request_id=str(tool_use),
                state="open",
                title=questions[0]["question"] if questions else "Question",
                detail=json.dumps(
                    {"tool": payload.get("tool_name"), "questions": questions, "answer_in": "terminal"},
                    ensure_ascii=False,
                ),
                allowed_responses=json.dumps([]),
                originating_cursor=cursor,
                created_at=at,
                updated_at=at,
            )
            .on_conflict_do_nothing(index_elements=["session_id", "native_request_id"])
        )
    elif name == "PostToolUse" and tool_use:
        _set_request(conn, session_id, tool_use, "resolved", at, resolution="answered")
    elif name in HOOK_TURN_ENDS:
        conn.execute(
            update(AttentionItem)
            .where(
                AttentionItem.session_id == session_id,
                AttentionItem.kind == "question",
                AttentionItem.state.in_(ACTIVE),
            )
            .values(state="expired", resolution="turn ended", updated_at=at)
        )


def _questions(tool_input: Any) -> list[dict[str, Any]]:
    raw = tool_input.get("questions") if isinstance(tool_input, dict) else None
    out = []
    for q in raw if isinstance(raw, list) else []:
        if not isinstance(q, dict) or not q.get("question"):
            continue
        options = q.get("options") if isinstance(q.get("options"), list) else []
        out.append(
            {
                "question": str(q["question"]),
                "header": str(q.get("header") or ""),
                "multi": bool(q.get("multiSelect")),
                "options": [
                    {"label": str(o.get("label") or ""), "description": str(o.get("description") or "")}
                    for o in options
                    if isinstance(o, dict)
                ],
            }
        )
    return out


def _set_request(
    conn: Connection, session_id: str, request_id: Any, state: str, at: datetime, resolution: str
) -> None:
    if not request_id:
        return
    conn.execute(
        update(AttentionItem)
        .where(
            AttentionItem.session_id == session_id,
            AttentionItem.native_request_id == str(request_id),
            AttentionItem.state.in_(ACTIVE),
        )
        .values(state=state, resolution=resolution, updated_at=at)
    )


def _failure(conn: Connection, session_id: str, cursor: int, at: datetime, ev: dict[str, Any]) -> None:
    detail = json.dumps({"event": ev.get("type"), "reason": _summary(ev.get("reason"))}, ensure_ascii=False)
    existing = conn.execute(
        select(AttentionItem.attention_id).where(
            AttentionItem.session_id == session_id,
            AttentionItem.kind == "failure",
            AttentionItem.state.in_(("open", "acknowledged", "dismissed")),
        )
    ).scalar()
    if existing:  # a new failure reopens the item
        conn.execute(
            update(AttentionItem)
            .where(AttentionItem.attention_id == existing)
            .values(state="open", detail=detail, originating_cursor=cursor, updated_at=at)
        )
        return
    task = conn.execute(
        select(SessionRow.current_task_id).where(SessionRow.session_id == session_id)
    ).scalar()
    conn.execute(
        insert(AttentionItem).values(
            attention_id=new_id(),
            kind="failure",
            session_id=session_id,
            task_id=task,
            state="open",
            title="The session reported a failure",
            detail=detail,
            allowed_responses=json.dumps([]),
            originating_cursor=cursor,
            created_at=at,
            updated_at=at,
        )
    )


# -- reads and user actions ----------------------------------------------------


def view(r: Any) -> dict[str, Any]:
    return {
        "attention_id": r["attention_id"],
        "kind": r["kind"],
        "session_id": r["session_id"],
        "task_id": r["task_id"],
        "state": r["state"],
        "title": r["title"],
        "detail": json.loads(r["detail"]),
        "allowed_responses": json.loads(r["allowed_responses"]),
        "resolution": r["resolution"],
        "resolution_action_id": r["resolution_action_id"],
        "created_at": r["created_at"].isoformat(),
        "updated_at": r["updated_at"].isoformat(),
    }


def list_items(
    store: Store, *, states: tuple[str, ...] = ACTIVE, session_id: str | None = None
) -> list[dict[str, Any]]:
    q = select(AttentionItem).where(AttentionItem.state.in_(states)).order_by(AttentionItem.created_at.desc())
    if session_id:
        q = q.where(AttentionItem.session_id == session_id)
    with store.reader.connect() as conn:
        return [view(r) for r in conn.execute(q).mappings()]


def get(store: Store, attention_id: str) -> dict[str, Any]:
    with store.reader.connect() as conn:
        r = (
            conn.execute(select(AttentionItem).where(AttentionItem.attention_id == attention_id))
            .mappings()
            .first()
        )
    if r is None:
        raise AttentionError("not_found", f"no attention item {attention_id}", 404)
    return view(r)


def claim(store: Store, attention_id: str, action_id: str) -> dict[str, Any]:
    """Atomically take an open request for one response (TR-045). Expired or answered items refuse."""

    def work(conn: Connection) -> None:
        n = conn.execute(
            update(AttentionItem)
            .where(
                and_(
                    AttentionItem.attention_id == attention_id,
                    AttentionItem.state.in_(ACTIVE),
                    AttentionItem.kind == "approval",  # questions get a text-answer path later
                    AttentionItem.resolution_action_id.is_(None),
                )
            )
            .values(resolution_action_id=action_id, updated_at=utc_now())
        ).rowcount
        if not n:
            r = conn.execute(
                select(AttentionItem.state, AttentionItem.kind).where(
                    AttentionItem.attention_id == attention_id
                )
            ).first()
            if r is None:
                raise AttentionError("not_found", f"no attention item {attention_id}", 404)
            raise AttentionError("not_answerable", f"this {r.kind} is {r.state} and can't take a response")

    store.writer.call(work)
    return get(store, attention_id)


def release(store: Store, attention_id: str, action_id: str) -> None:
    """Undo a claim whose response never left Robin, so the request can be answered again."""
    store.writer.call(
        lambda c: c.execute(
            update(AttentionItem)
            .where(
                AttentionItem.attention_id == attention_id, AttentionItem.resolution_action_id == action_id
            )
            .values(resolution_action_id=None, updated_at=utc_now())
        )
    )


def set_notice(store: Store, attention_id: str, to: str, actor: str = "user") -> dict[str, Any]:
    """Acknowledge or dismiss: changes the notice only, with an audit record. Never sends a response."""
    assert to in ("acknowledged", "dismissed")
    now = utc_now()

    def work(conn: Connection) -> None:
        r = conn.execute(
            select(AttentionItem.state).where(AttentionItem.attention_id == attention_id)
        ).first()
        if r is None:
            raise AttentionError("not_found", f"no attention item {attention_id}", 404)
        if r.state not in ACTIVE:
            raise AttentionError("not_open", f"this item is already {r.state}")
        conn.execute(
            update(AttentionItem)
            .where(AttentionItem.attention_id == attention_id)
            .values(state=to, updated_at=now)
        )
        conn.execute(
            insert(AuditEntry).values(
                audit_id=new_id(),
                at=now,
                actor=actor,
                action=f"attention.{to}",
                affected_ids=json.dumps([attention_id]),
                outcome="ok",
            )
        )

    store.writer.call(work)
    return get(store, attention_id)
