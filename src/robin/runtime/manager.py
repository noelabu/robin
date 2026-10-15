"""The runtime manager: control actions in, state out (TR-040–044).

Every control is persisted in the ledger before dispatch, serialized per session, and checked against
capability, connection and expected lease generation just before it goes out. Replies and host events
move the action forward; nothing is resent automatically. State is projected from committed events.
"""

import asyncio
import json
from collections import defaultdict
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from robin.events.supervisor_source import SupervisorRecord
from robin.ids import utc_now
from robin.runtime import attention, ledger, state
from robin.runtime.capabilities import capabilities_for
from robin.runtime.ledger import Action, ActionRejected
from robin.store import Store
from robin.store.models import ControlAction, Event, Meta
from robin.store.models import Session as SessionRow
from robin.supervisor.client import SupervisorClient, SupervisorUnavailable

# Host events that acknowledge or reject the action whose id they carry.
ACK_EVENTS = {"input_submitted", "interrupt_acknowledged", "approval_answered"}
REJECT_EVENTS = {"approval_rejected", "command_rejected", "command_failed"}


class RuntimeManager:
    def __init__(self, store: Store, client: SupervisorClient | None = None):
        self.store = store
        self.client = client
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    # -- lifecycle ---------------------------------------------------------

    async def recover(self) -> list[str]:
        """On service start: catch up projection first (late acknowledgements settle their actions), then
        anything still in flight becomes outcome_unknown and is never resent (TR-043)."""
        await self.project_pending()
        return await self.store.writer.acall(ledger.recover_in_flight)

    async def attach(self, client: SupervisorClient) -> None:
        """Use a (re)connected supervisor and reconcile connection state from what it reports."""
        self.client = client
        alive = {sid for sid, s in client.hello.get("sessions", {}).items() if s.get("alive")}
        now = utc_now()

        def reconcile(conn: Connection) -> None:
            managed = conn.execute(
                select(SessionRow.session_id, SessionRow.connection_state).where(SessionRow.mode == "managed")
            ).all()
            for sid, current in managed:
                target = "connected" if sid in alive else "disconnected"
                if current != target:
                    state.mark_connection(conn, sid, target, now)

        await self.store.writer.acall(reconcile)

    async def connection_lost(self) -> None:
        """The supervisor link dropped: connected sessions are stale until reconciled (TR-040)."""
        now = utc_now()

        def mark(conn: Connection) -> None:
            for sid in conn.execute(
                select(SessionRow.session_id).where(SessionRow.connection_state == "connected")
            ).scalars():
                state.mark_connection(conn, sid, "stale", now)

        await self.store.writer.acall(mark)

    # -- controls ----------------------------------------------------------

    async def launch(
        self,
        session_id: str,
        *,
        runtime: str,
        cwd: str,
        action_id: str,
        prompt: str | None = None,
        env: dict[str, str] | None = None,
    ) -> Action:
        payload = {"runtime": runtime, "cwd": cwd, "prompt": prompt}
        msg: dict[str, Any] = {
            "type": "launch",
            "session": session_id,
            "runtime": runtime,
            "cwd": cwd,
            "prompt": prompt,
            "action_id": action_id,
            "env": env or {},
        }
        return await self._control(session_id, "launch", payload, action_id, msg, needs_connection=False)

    async def message(
        self, session_id: str, text: str, *, action_id: str, expected_generation: int | None = None
    ) -> Action:
        cmd = {"cmd": "send", "text": text, "action_id": action_id}
        return await self._control(
            session_id,
            "message",
            {"text": text},
            action_id,
            {"type": "command", "session": session_id, "command": cmd},
            expected_generation=expected_generation,
        )

    async def interrupt(
        self, session_id: str, *, action_id: str, expected_generation: int | None = None
    ) -> Action:
        cmd = {"cmd": "interrupt", "action_id": action_id}
        return await self._control(
            session_id,
            "interrupt",
            {},
            action_id,
            {"type": "command", "session": session_id, "command": cmd},
            expected_generation=expected_generation,
        )

    async def respond(
        self, session_id: str, request_id: str, allow: bool, *, action_id: str, message: str | None = None
    ) -> Action:
        cmd = {
            "cmd": "approval",
            "request_id": request_id,
            "allow": allow,
            "message": message,
            "action_id": action_id,
        }
        return await self._control(
            session_id,
            "approval",
            {"request_id": request_id, "allow": allow},
            action_id,
            {"type": "command", "session": session_id, "command": cmd},
        )

    async def stop(self, session_id: str, *, action_id: str) -> Action:
        return await self._control(
            session_id,
            "stop",
            {},
            action_id,
            {"type": "command", "session": session_id, "command": {"cmd": "stop"}},
        )

    async def get_action(self, action_id: str) -> Action | None:
        with self.store.reader.connect() as conn:
            return ledger.get(conn, action_id)

    async def _control(
        self,
        session_id: str,
        action_type: str,
        payload: dict[str, Any],
        action_id: str,
        msg: dict[str, Any],
        *,
        needs_connection: bool = True,
        expected_generation: int | None = None,
    ) -> Action:
        with self.store.reader.connect() as conn:
            known = conn.execute(
                select(SessionRow.session_id).where(SessionRow.session_id == session_id)
            ).first()
        if known is None:  # nothing to record an action against
            raise ActionRejected("unknown_session", f"no session {session_id}")
        async with self._locks[session_id]:
            action, created = await self.store.writer.acall(
                lambda c: ledger.create(
                    c,
                    action_id=action_id,
                    session_id=session_id,
                    action_type=action_type,
                    payload=payload,
                    expected_generation=expected_generation,
                )
            )
            if not created:
                return action  # identical repeat: report the stored outcome, never resend
            try:
                self._preconditions(session_id, action_type, needs_connection, expected_generation)
            except ActionRejected as exc:
                await self._move(action_id, "failed", error_code=exc.code, error_message=str(exc))
                raise
            if self.client is None:
                return await self._move(
                    action_id,
                    "failed",
                    error_code="supervisor_unavailable",
                    error_message="no supervisor connection",
                )
            try:
                reply = await self.client.request(msg)
            except (SupervisorUnavailable, TimeoutError, ConnectionError) as exc:
                # It may or may not have reached the runtime: say so instead of guessing (TR-043).
                return await self._move(
                    action_id,
                    "outcome_unknown",
                    error_code="delivery_unknown",
                    error_message=str(exc) or type(exc).__name__,
                )
            if reply.get("ok"):
                return await self._move(action_id, "submitted")
            return await self._move(
                action_id, "failed", error_code=str(reply.get("error", "rejected")), error_message=str(reply)
            )

    def _preconditions(
        self, session_id: str, action_type: str, needs_connection: bool, expected_generation: int | None
    ) -> None:
        with self.store.reader.connect() as conn:
            row = (
                conn.execute(select(SessionRow).where(SessionRow.session_id == session_id)).mappings().first()
            )
        if row is None:
            raise ActionRejected("unknown_session", f"no session {session_id}")
        if row["mode"] != "managed":
            raise ActionRejected("observed_session", "observed sessions are read-only in Robin")
        ok, reason = capabilities_for(row["runtime"]).allows(action_type)
        if not ok:
            raise ActionRejected("unsupported", reason or "unsupported")
        if needs_connection and row["connection_state"] != "connected":
            raise ActionRejected("not_connected", f"session is {row['connection_state']}")
        if expected_generation is not None and expected_generation != row["lease_generation"]:
            raise ActionRejected(
                "stale_generation",
                f"expected lease generation {expected_generation}, session is at {row['lease_generation']}",
            )

    async def _move(self, action_id: str, to: str, **kw: Any) -> Action:
        action = await self.store.writer.acall(lambda c: ledger.transition(c, action_id, to, **kw))
        assert action is not None
        return action

    # -- after commit ------------------------------------------------------

    async def on_committed(self, records: list[SupervisorRecord]) -> None:
        """Called after the bridge commits a batch; projection reads from the event table, not the batch."""
        await self.project_pending()

    async def project_pending(self, batch: int = 500) -> int:
        """Apply every committed supervisor event past the projection watermark (TR-004).

        The watermark advances in the same transaction as the projection, so a crash or error between
        ingest and projection is caught up on the next call, including at service start.
        """
        total = 0
        while True:
            applied = await self.store.writer.acall(lambda c: _project_batch(c, self.store, batch))
            total += applied
            if applied < batch:
                return total


PROJECTION_KEY = "projection_cursor"


def _project_batch(conn: Connection, store: Store, batch: int) -> int:
    watermark = int(conn.execute(select(Meta.value).where(Meta.key == PROJECTION_KEY)).scalar() or 0)
    rows = conn.execute(
        select(
            Event.global_cursor,
            Event.session_id,
            Event.source_event_id,
            Event.source_time,
            Event.ingested_at,
            Event.payload_inline,
            Event.payload_blob,
        )
        .where(Event.global_cursor > watermark, Event.source_id.like("supervisor:%"))
        .order_by(Event.global_cursor)
        .limit(batch)
    ).all()
    for r in rows:
        raw = r.payload_inline if r.payload_inline is not None else store.blobs.read(r.payload_blob).decode()
        try:
            ev = json.loads(raw)
        except ValueError:
            continue
        if isinstance(ev, dict):
            _project_one(
                conn,
                r.session_id,
                r.global_cursor,
                r.source_time or r.ingested_at,
                r.source_event_id or "",
                ev,
            )
            # Attribute the event to the session's task as of this point in commit order.
            task = conn.execute(
                select(SessionRow.current_task_id).where(SessionRow.session_id == r.session_id)
            ).scalar()
            if task:
                conn.execute(update(Event).where(Event.global_cursor == r.global_cursor).values(task_id=task))
    if rows:
        conn.execute(
            sqlite_insert(Meta)
            .values(key=PROJECTION_KEY, value=str(rows[-1].global_cursor))
            .on_conflict_do_update(index_elements=["key"], set_={"value": str(rows[-1].global_cursor)})
        )
    return len(rows)


def _project_one(
    conn: Connection, session_id: str, cursor: int, at: datetime, correlation: str, ev: dict[str, Any]
) -> None:
    state.apply(conn, session_id, cursor, at, ev)
    attention.project(conn, session_id, cursor, at, ev)
    action_id = ev.get("action_id")
    kind = ev.get("type")
    if action_id and kind in ACK_EVENTS:
        ledger.transition(conn, action_id, "acknowledged", native_correlation=correlation)
    elif action_id and kind in REJECT_EVENTS:
        ledger.transition(
            conn,
            action_id,
            "failed",
            error_code=str(kind),
            error_message=str(ev.get("reason", "")),
            native_correlation=correlation,
        )
    elif kind == "host_exited":
        # A host that exits before its session starts means the launch failed.
        never_started = (
            conn.execute(
                select(ControlAction.action_id).where(
                    ControlAction.session_id == session_id,
                    ControlAction.action_type == "launch",
                    ControlAction.state.in_(("requested", "submitted", "outcome_unknown")),
                )
            )
            .scalars()
            .all()
        )
        for launch_id in never_started:
            ledger.transition(
                conn,
                launch_id,
                "failed",
                error_code="host_exited_before_start",
                error_message=f"runtime host exited with code {ev.get('returncode')}",
                native_correlation=correlation,
            )
    elif kind == "session_started":
        launches = (
            conn.execute(
                select(ControlAction.action_id).where(
                    ControlAction.session_id == session_id,
                    ControlAction.action_type == "launch",
                    ControlAction.state.in_(("requested", "submitted", "outcome_unknown")),
                )
            )
            .scalars()
            .all()
        )
        for launch_id in launches:
            ledger.transition(conn, launch_id, "acknowledged", native_correlation=correlation)
