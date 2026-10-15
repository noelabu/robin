"""Projecting session state from committed host events (TR-040).

Runtime state and connection state are tracked separately. A process being alive proves only liveness;
a turn start sets running; waiting_for_input only when the runtime says the request blocks work; a turn
end sets idle without marking any task done. Projections are rebuildable from the event table (TR-004).
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, insert, select, update

from robin.ids import new_id
from robin.store.models import Run
from robin.store.models import Session as SessionRow

# Host events that block the turn until the user answers.
BLOCKING = {"approval_request", "question"}


@dataclass(frozen=True)
class StateChange:
    runtime_state: str | None = None
    connection_state: str | None = None
    run: str | None = None  # "start" | "end"
    run_outcome: str | None = None


def reduce(event: dict[str, Any]) -> StateChange | None:
    match event.get("type"):
        case "host_spawned":
            return StateChange(connection_state="connected")
        case "session_started":
            return StateChange(runtime_state="idle")
        case "turn_started":
            return StateChange(runtime_state="running", run="start")
        case "turn_completed":
            return StateChange(runtime_state="idle", run="end", run_outcome="completed")
        case "turn_interrupted":
            return StateChange(runtime_state="idle", run="end", run_outcome="interrupted")
        case "turn_failed":
            return StateChange(runtime_state="failed", run="end", run_outcome="failed")
        case t if t in BLOCKING:
            return StateChange(runtime_state="waiting_for_input")
        case "approval_answered" | "approval_expired":
            return StateChange(runtime_state="running")
        case "session_stopping":
            return StateChange(runtime_state="stopped")
        case "host_exited":
            return StateChange(
                runtime_state="stopped", connection_state="disconnected", run="end", run_outcome="stopped"
            )
    return None


def apply(conn: Connection, session_id: str, cursor: int, at: datetime, event: dict[str, Any]) -> None:
    """Apply one committed event. Ignores events at or before the session's projected cursor."""
    change = reduce(event)
    row = conn.execute(
        select(
            SessionRow.state_cursor,
            SessionRow.runtime_state,
            SessionRow.active_run_id,
            SessionRow.current_task_id,
            SessionRow.pending_task_id,
        ).where(SessionRow.session_id == session_id)
    ).first()
    if change is None or row is None or cursor <= row.state_cursor:
        return
    if event.get("type") == "session_started" and row.runtime_state != "unknown":
        change = StateChange()  # init can arrive after the first turn started; it never downgrades state
    if change.runtime_state == "waiting_for_input" and row.runtime_state not in (
        "running",
        "waiting_for_input",
    ):
        change = StateChange()  # a request outside a turn adds attention, not a blocking state
    values: dict[str, Any] = {"state_cursor": cursor, "state_evidence_at": at, "updated_at": at}
    if change.runtime_state:
        values["runtime_state"] = change.runtime_state
    if change.connection_state:
        values["connection_state"] = change.connection_state
    if change.run == "start":
        run_id = new_id()
        task_id = row.current_task_id
        if row.pending_task_id:  # a confirmed re-attach takes effect at the next run (TR-020)
            task_id = row.pending_task_id
            values["current_task_id"], values["pending_task_id"] = task_id, None
        conn.execute(
            insert(Run).values(
                run_id=run_id,
                session_id=session_id,
                started_at=at,
                native_turn_id=event.get("native_turn_id"),
                task_id=task_id,
            )
        )
        values["active_run_id"] = run_id
    elif change.run == "end" and row.active_run_id:
        conn.execute(
            update(Run)
            .where(Run.run_id == row.active_run_id, Run.ended_at.is_(None))
            .values(ended_at=at, outcome=change.run_outcome)
        )
        values["active_run_id"] = None
    native = event.get("native_session_id")
    if event.get("type") == "session_started" and native:
        _capture_native_id(conn, session_id, str(native))
    conn.execute(update(SessionRow).where(SessionRow.session_id == session_id).values(**values))


def _capture_native_id(conn: Connection, session_id: str, native: str) -> None:
    """Record the runtime's own session ID (FR-012) unless another Robin session already has it."""
    me = conn.execute(
        select(SessionRow.host_id, SessionRow.runtime, SessionRow.native_session_id).where(
            SessionRow.session_id == session_id
        )
    ).one()
    if me.native_session_id == native:
        return
    taken = conn.execute(
        select(SessionRow.session_id).where(
            SessionRow.host_id == me.host_id,
            SessionRow.runtime == me.runtime,
            SessionRow.native_session_id == native,
            SessionRow.session_id != session_id,
        )
    ).first()
    if taken is None and me.native_session_id is None:
        conn.execute(
            update(SessionRow).where(SessionRow.session_id == session_id).values(native_session_id=native)
        )


def mark_connection(conn: Connection, session_id: str, state: str, at: datetime) -> None:
    conn.execute(
        update(SessionRow)
        .where(SessionRow.session_id == session_id)
        .values(connection_state=state, state_evidence_at=at, updated_at=at)
    )
