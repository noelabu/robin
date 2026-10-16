"""Notifications (FR-033): tell the user when a managed session needs input, fails, or finishes a run.

Opt-in and off by default. The service reads committed events past a durable cursor, so each event notifies
at most once, a restart or replay never repeats one, and turning notifications on doesn't announce the
backlog. A notification names the runtime, the task and the session; it never carries prompt or message
text. Sessions the user is looking at in the dashboard right now are left quiet.
"""

import json
import logging
import subprocess
import sys
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from robin.events.ingest import read_payload_json
from robin.store import Store
from robin.store.models import Event, Meta, Task
from robin.store.models import Session as SessionRow

SETTING = "notifications"
CURSOR = "notifications_cursor"
KINDS = ("input", "failed", "finished")
BATCH = 500
MAX_PER_BATCH = 3  # more than this at once is summarised in one notification
log = logging.getLogger(__name__)

# envelope event type -> notification kind; turn_completed is "finished" only when the turn wasn't interrupted
EVENT_KINDS = {
    "attention.approval_requested": "input",
    "attention.question": "input",
    "session.failed": "failed",
    "session.turn_completed": "finished",
}
WORDS = {"input": "needs your input", "failed": "failed", "finished": "finished its run"}


@dataclass(frozen=True)
class Note:
    session_id: str
    kind: str
    title: str
    body: str


def settings(store: Store) -> dict[str, Any]:
    with store.reader.connect() as conn:
        raw = conn.execute(select(Meta.value).where(Meta.key == SETTING)).scalar()
    value = json.loads(raw) if raw else {}
    return {"enabled": bool(value.get("enabled")), "kinds": list(value.get("kinds", KINDS))}


def set_settings(store: Store, enabled: bool, kinds: list[str] | None = None) -> dict[str, Any]:
    kinds = list(KINDS) if kinds is None else [k for k in KINDS if k in kinds]
    value = json.dumps({"enabled": enabled, "kinds": kinds})

    def work(conn: Any) -> None:
        was = conn.execute(select(Meta.value).where(Meta.key == SETTING)).scalar()
        rows = [(SETTING, value)]
        if enabled and not (was and json.loads(was).get("enabled")):
            # start from now: turning notifications on never announces what already happened
            latest = conn.execute(select(func.max(Event.global_cursor))).scalar() or 0
            rows.append((CURSOR, str(latest)))
        for key, v in rows:
            conn.execute(
                sqlite_insert(Meta)
                .values(key=key, value=v)
                .on_conflict_do_update(index_elements=["key"], set_={"value": v})
            )

    store.writer.call(work)
    return settings(store)


def pending(store: Store) -> tuple[list[Note], int]:
    """Notifications for events past the cursor, and the cursor to store once they're handled."""
    wanted = set(settings(store)["kinds"])
    with store.reader.connect() as conn:
        cursor = int(conn.execute(select(Meta.value).where(Meta.key == CURSOR)).scalar() or 0)
        rows = conn.execute(
            select(Event.global_cursor, Event.event_type, Event.session_id, SessionRow.runtime, Task.title)
            .join(SessionRow, SessionRow.session_id == Event.session_id)
            .outerjoin(Task, Task.task_id == func.coalesce(Event.task_id, SessionRow.current_task_id))
            .where(
                Event.global_cursor > cursor,
                Event.event_type.in_(list(EVENT_KINDS)),
                SessionRow.mode == "managed",
            )
            .order_by(Event.global_cursor)
            .limit(BATCH)
        ).all()
        if len(rows) < BATCH:  # nothing relevant further on: skip past everything read so far
            last = conn.execute(select(func.max(Event.global_cursor))).scalar() or cursor
        else:
            last = rows[-1].global_cursor
    notes = []
    for r in rows:
        kind = EVENT_KINDS[r.event_type]
        if kind == "finished" and read_payload_json(store, r.global_cursor).get("type") != "turn_completed":
            continue  # an interrupted turn: the user did that themselves
        if kind not in wanted:
            continue
        where = f" on “{r.title}”" if r.title else ""
        notes.append(
            Note(
                session_id=r.session_id,
                kind=kind,
                title="Robin",
                body=f"{r.runtime} session {r.session_id[:8]} {WORDS[kind]}{where}",
            )
        )
    return notes, max(int(last), cursor)


def advance(store: Store, cursor: int) -> None:
    store.writer.call(
        lambda c: c.execute(
            sqlite_insert(Meta)
            .values(key=CURSOR, value=str(cursor))
            .on_conflict_do_update(index_elements=["key"], set_={"value": str(cursor)})
        )
    )


def condense(notes: list[Note]) -> list[Note]:
    """A burst becomes a few notifications and one summary, not a wall of them."""
    if len(notes) <= MAX_PER_BATCH:
        return notes
    keep = notes[: MAX_PER_BATCH - 1]
    rest = len(notes) - len(keep)
    return [*keep, Note("", "summary", "Robin", f"{rest} more session updates; open Robin to see them")]


def send(note: Note) -> None:
    """A macOS notification. Text goes in as arguments, never into the script, so nothing in it can run."""
    if sys.platform != "darwin":
        return
    script = ["-e", "on run argv", "-e", "display notification (item 2 of argv) with title (item 1 of argv)"]
    subprocess.run(
        ["osascript", *script, "-e", "end run", note.title, note.body],
        capture_output=True,
        timeout=10,
        check=False,
    )
