"""The attention queue (FR-017, FR-035, TR-045).

Items are projected from committed host events. A request expires when the runtime says so or, where it
has no expiry, when its run ends, is interrupted, or its host goes away; an expired item can't be answered.
Answering claims the item atomically, so one request gets one response. Acknowledging or dismissing changes
the notice only: it never approves anything. A new failure reopens the session's failure item.
"""

import json
import time
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, and_, insert, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from robin.ids import new_id, utc_now
from robin.runtime import ledger
from robin.store import Store
from robin.store.models import AttentionItem, AuditEntry, Meta
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
        if kind == "approval_request":
            title, allowed = f"{tool} needs approval", ["allow", "deny"]
            detail: dict[str, Any] = {"tool": tool, "input": _summary(ev.get("input"))}
        else:  # a question asked in a session Robin runs: answered here
            questions = _questions(ev.get("input"))
            title = questions[0]["question"] if questions else str(ev.get("text") or "Question")
            allowed = ["answer", "deny"] if questions else []
            detail = {"tool": tool, "questions": questions, "answer_in": "robin" if questions else "terminal"}
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
                title=title,
                detail=json.dumps(detail, ensure_ascii=False, default=str),
                allowed_responses=json.dumps(allowed),
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
            resolution="answered" if ev.get("answered") else "allowed" if ev.get("allow") else "denied",
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
# Robin's question hook (robin.hooks.ask) registering a question it waits on, and that wait ending.
ASK_HOOK, ASK_END_HOOK = "_robin_ask", "_robin_ask_end"
HOOK_RESPONSES = ["answer", "deny", "terminal"]


def project_hook(
    conn: Connection, session_id: str, cursor: int, at: datetime, payload: dict[str, Any]
) -> None:
    """One hook from a session started outside Robin. Its questions show here, answered in its terminal,
    or here while Robin's question hook waits for an answer."""
    name = payload.get("hook_event_name")
    tool_use = payload.get("tool_use_id")
    if name == ASK_HOOK and payload.get("tool_name") in QUESTION_TOOLS and tool_use:
        questions = _questions(payload.get("tool_input"))
        if not questions:
            return
        task = conn.execute(
            select(SessionRow.current_task_id).where(SessionRow.session_id == session_id)
        ).scalar()
        detail = json.dumps(
            {
                "tool": payload.get("tool_name"),
                "questions": questions,
                "answer_in": "hook",
                "until": payload.get("robin_until"),
            },
            ensure_ascii=False,
        )
        stmt = sqlite_insert(AttentionItem).values(
            attention_id=new_id(),
            kind="question",
            session_id=session_id,
            task_id=task,
            native_request_id=str(tool_use),
            state="open",
            title=questions[0]["question"],
            detail=detail,
            allowed_responses=json.dumps(HOOK_RESPONSES),
            originating_cursor=cursor,
            created_at=at,
            updated_at=at,
        )
        # The observer's PreToolUse may have shown it first, as answered in the terminal: make it answerable.
        conn.execute(
            stmt.on_conflict_do_update(
                index_elements=["session_id", "native_request_id"],
                set_={
                    "detail": stmt.excluded.detail,
                    "allowed_responses": stmt.excluded.allowed_responses,
                    "updated_at": at,
                },
                where=and_(AttentionItem.state.in_(ACTIVE), AttentionItem.resolution_action_id.is_(None)),
            )
        )
    elif name == "PreToolUse" and payload.get("tool_name") in QUESTION_TOOLS and tool_use:
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


ANSWER_CHARS = 8192  # what Claude Code accepts per answer, and in all
ANSWERS_CHARS = 32768


def normalize_answers(questions: list[dict[str, Any]], answers: dict[str, Any]) -> dict[str, str]:
    """Answers keyed by the exact question text, as the runtime expects them. A multi-select question may
    take a list, joined the way Claude Code joins its own picks."""
    by_text = {q["question"]: q for q in questions}
    if not answers:
        raise AttentionError("invalid_answers", "give at least one answer", 422)
    out: dict[str, str] = {}
    for text, value in answers.items():
        q = by_text.get(text)
        if q is None:
            raise AttentionError("invalid_answers", f"no question {text!r} in this request", 422)
        if isinstance(value, list):
            if not q["multi"]:
                raise AttentionError("invalid_answers", f"{text!r} takes one answer", 422)
            value = ", ".join(str(v) for v in value)
        value = str(value).strip()
        if not value or len(value) > ANSWER_CHARS:
            raise AttentionError("invalid_answers", f"the answer to {text!r} is empty or too long", 422)
        out[text] = value
    if sum(len(v) for v in out.values()) > ANSWERS_CHARS:
        raise AttentionError("invalid_answers", "the answers are too long", 422)
    return out


def claim(store: Store, attention_id: str, action_id: str, decision: str = "allow") -> dict[str, Any]:
    """Atomically take an open request for one response (TR-045). Expired or answered items refuse, and so
    does a response the item doesn't take (a question asked in a terminal is answered there)."""

    def work(conn: Connection) -> None:
        r = conn.execute(
            select(AttentionItem.kind, AttentionItem.allowed_responses).where(
                AttentionItem.attention_id == attention_id
            )
        ).first()
        if r is not None and decision not in json.loads(r.allowed_responses):
            if r.kind == "question" and not json.loads(r.allowed_responses):
                raise AttentionError("answer_in_terminal", "answer this question in the session's terminal")
            raise AttentionError("invalid_response", f"a {r.kind} can't take {decision!r}", 422)
        n = conn.execute(
            update(AttentionItem)
            .where(
                and_(
                    AttentionItem.attention_id == attention_id,
                    AttentionItem.state.in_(ACTIVE),
                    AttentionItem.kind.in_(("approval", "question")),
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


# -- questions answered through Robin's question hook --------------------------------------------------------

WAIT_SETTING = "question_wait_s"
WAIT_DEFAULT_S = 300
WAIT_MAX_S = 540  # under the hook's 600 s timeout, so Claude never kills it


def question_wait_s(store: Store) -> int:
    """How long the question hook waits for an answer here before the terminal asks. 0 turns it off."""
    with store.reader.connect() as conn:
        value = conn.execute(select(Meta.value).where(Meta.key == WAIT_SETTING)).scalar()
    try:
        return max(0, min(WAIT_MAX_S, int(value))) if value is not None else WAIT_DEFAULT_S
    except ValueError:
        return WAIT_DEFAULT_S


def set_question_wait_s(store: Store, seconds: int) -> int:
    value = str(max(0, min(WAIT_MAX_S, int(seconds))))
    store.writer.call(
        lambda c: c.execute(
            sqlite_insert(Meta)
            .values(key=WAIT_SETTING, value=value)
            .on_conflict_do_update(index_elements=["key"], set_={"value": value})
        )
    )
    return int(value)


def _hook_row(conn: Connection, attention_id: str) -> Any:
    r = (
        conn.execute(select(AttentionItem).where(AttentionItem.attention_id == attention_id))
        .mappings()
        .first()
    )
    if r is None:
        raise AttentionError("not_found", f"no attention item {attention_id}", 404)
    return r


def _to_terminal(conn: Connection, attention_id: str, detail: dict[str, Any], **extra: Any) -> None:
    """The hook stopped waiting: the question is answered in the terminal from here on."""
    conn.execute(
        update(AttentionItem)
        .where(AttentionItem.attention_id == attention_id, AttentionItem.resolution_action_id.is_(None))
        .values(
            detail=json.dumps({**detail, "answer_in": "terminal"}, ensure_ascii=False),
            allowed_responses=json.dumps([]),
            updated_at=utc_now(),
            **extra,
        )
    )


def reply_hook(
    store: Store,
    attention_id: str,
    action_id: str,
    decision: str,
    answers: dict[str, str] | None = None,
    message: str | None = None,
    now: float | None = None,
) -> ledger.Action:
    """Answer, decline or hand back a question the hook is waiting on. Once only, and never after the hook
    gave up: the deadline is checked in the same transaction that takes the item."""

    def work(conn: Connection) -> ledger.Action | str:
        at = (
            time.time() if now is None else now
        )  # in the transaction: a queued write may land past the deadline
        r = _hook_row(conn, attention_id)
        detail = json.loads(r["detail"])
        allowed = json.loads(r["allowed_responses"])
        if decision not in allowed:
            if not allowed:
                raise AttentionError("answer_in_terminal", "answer this question in the session's terminal")
            raise AttentionError("invalid_response", f"a {r['kind']} can't take {decision!r}", 422)
        if r["state"] not in ACTIVE or r["resolution_action_id"] is not None:
            raise AttentionError(
                "not_answerable", f"this {r['kind']} is {r['state']} and can't take a response"
            )
        if at >= float(detail.get("until") or 0):
            _to_terminal(conn, attention_id, detail)
            return "expired"
        if decision == "answer":
            missing = [
                q["question"] for q in detail.get("questions", []) if q["question"] not in (answers or {})
            ]
            if missing:
                raise AttentionError("invalid_answers", f"answer every question; missing {missing[0]!r}", 422)
        payload = {"request_id": r["native_request_id"], "decision": decision, "answers": answers}
        action, _ = ledger.create(
            conn, action_id=action_id, session_id=r["session_id"], action_type="approval", payload=payload
        )
        ledger.transition(conn, action_id, "submitted")
        reply = {"decision": decision, "answers": answers, "message": message, "action_id": action_id}
        if decision == "terminal":
            _to_terminal(conn, attention_id, {**detail, "reply": reply}, resolution_action_id=action_id)
        else:
            conn.execute(
                update(AttentionItem)
                .where(AttentionItem.attention_id == attention_id)
                .values(
                    state="resolved",
                    resolution="answered" if decision == "answer" else "declined",
                    resolution_action_id=action_id,
                    detail=json.dumps({**detail, "reply": reply}, ensure_ascii=False),
                    updated_at=utc_now(),
                )
            )
        got = ledger.get(conn, action_id)
        assert got is not None
        return got

    out = store.writer.call(work)
    if out == "expired":  # raised outside the transaction, so handing it to the terminal sticks
        raise AttentionError(
            "answer_in_terminal", "Robin stopped waiting; answer it in the session's terminal"
        )
    assert isinstance(out, ledger.Action)
    return out


def hook_outcome(store: Store, attention_id: str, now: float | None = None) -> dict[str, Any]:
    """What the waiting hook should do: pending, answered, declined, terminal, timeout or closed."""
    now = time.time() if now is None else now
    with store.reader.connect() as conn:
        r = _hook_row(conn, attention_id)
    detail = json.loads(r["detail"])
    if detail.get("reply"):
        return _delivered(store, detail["reply"])
    if r["state"] not in ACTIVE or detail.get("answer_in") != "hook":
        return {"status": "closed"}
    if now < float(detail.get("until") or 0):
        return {"status": "pending", "until": detail.get("until")}

    def expire(conn: Connection) -> dict[str, Any] | None:
        fresh = _hook_row(conn, attention_id)
        d = json.loads(fresh["detail"])
        if d.get("reply"):  # answered just before the deadline: that answer wins
            return dict(d["reply"])
        if fresh["state"] in ACTIVE and d.get("answer_in") == "hook":
            _to_terminal(conn, attention_id, d)
        return None

    reply = store.writer.call(expire)
    return _delivered(store, reply) if reply else {"status": "timeout"}


def _delivered(store: Store, reply: dict[str, Any]) -> dict[str, Any]:
    """The hook has the reply: its action is acknowledged."""
    store.writer.call(lambda c: ledger.transition(c, reply["action_id"], "acknowledged"))
    status = {"answer": "answered", "deny": "declined", "terminal": "terminal"}[reply["decision"]]
    out: dict[str, Any] = {"status": status}
    if reply["decision"] == "answer":
        out["answers"] = reply["answers"]
    if reply["decision"] == "deny":
        out["message"] = reply.get("message")
    return out


def release_hook(store: Store, attention_id: str) -> dict[str, Any]:
    """The hook is going away without output (Esc in the terminal, it was stopped, or it gave up): an
    unanswered question goes back to the terminal. A reply it never delivered is recorded as such, never as
    delivered."""

    def work(conn: Connection) -> dict[str, Any]:
        r = _hook_row(conn, attention_id)
        d = json.loads(r["detail"])
        if r["state"] in ACTIVE and d.get("answer_in") == "hook":
            _to_terminal(conn, attention_id, d)
            return {"status": "closed"}
        reply = d.get("reply")
        if not reply or reply["decision"] == "terminal":
            return {"status": "closed"}
        action = ledger.get(conn, reply["action_id"])
        if action is None or action.state == "acknowledged":
            return {"status": "closed"}  # the hook had it before it went away
        ledger.transition(
            conn,
            reply["action_id"],
            "failed",
            error_code="not_delivered",
            error_message="the question hook stopped before it could pass this on",
        )
        conn.execute(
            update(AttentionItem)
            .where(AttentionItem.attention_id == attention_id)
            .values(resolution="not delivered", updated_at=utc_now())
        )
        return {"status": "undelivered"}

    return dict(store.writer.call(work))
