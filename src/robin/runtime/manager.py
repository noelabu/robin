"""The runtime manager: control actions in, state out (TR-040–044).

Every control is persisted in the ledger before dispatch, serialized per session, and checked against
capability, connection and expected lease generation just before it goes out. Replies and host events
move the action forward; nothing is resent automatically. State is projected from committed events.
"""

import asyncio
import json
import logging
import os
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, delete, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from robin.events.supervisor_source import SupervisorRecord
from robin.findings import store as findings
from robin.ids import utc_now
from robin.runtime import attention, ledger, state
from robin.runtime.capabilities import capabilities_for
from robin.runtime.ledger import Action, ActionRejected
from robin.store import Store
from robin.store.models import AgentCredential, AttentionItem, ControlAction, Event, Meta, SourceCheckpoint
from robin.store.models import Session as SessionRow
from robin.supervisor.client import SupervisorClient, SupervisorUnavailable

# A transcript written this recently is taken to be open in a terminal, whatever pgrep finds.
TERMINAL_QUIET_S = 120
LAUNCH_TYPES = ("launch", "reclaim", "resume", "takeover")

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
        self,
        session_id: str,
        text: str,
        *,
        action_id: str,
        expected_generation: int | None = None,
        record: Callable[[Connection], None] | None = None,
    ) -> Action:
        """Send input. `record` runs in the transaction that creates the action, for rows that must exist
        exactly when the action does (a context delivery's manifest, TR-064)."""
        cmd = {"cmd": "send", "text": text, "action_id": action_id}
        return await self._control(
            session_id,
            "message",
            {"text": text},
            action_id,
            {"type": "command", "session": session_id, "command": cmd},
            expected_generation=expected_generation,
            record=record,
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
        self,
        session_id: str,
        request_id: str,
        allow: bool,
        *,
        action_id: str,
        message: str | None = None,
        answers: dict[str, str] | None = None,
    ) -> Action:
        """An approval's decision, or a question's answers (sent as an allow carrying them)."""
        cmd: dict[str, Any] = {
            "cmd": "approval",
            "request_id": request_id,
            "allow": allow,
            "message": message,
            "action_id": action_id,
        }
        payload: dict[str, Any] = {"request_id": request_id, "allow": allow}
        if answers:
            cmd["answers"] = payload["answers"] = answers
        return await self._control(
            session_id,
            "approval",
            payload,
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

    async def handoff(self, session_id: str, *, action_id: str, interrupt_running: bool = False) -> Action:
        """Release the session to its native terminal (FR-037, TR-046): Robin's host stops, the lease moves to
        the terminal, pending requests are handed off, and every Robin control is disabled until reclaimed."""

        def check(row: Any) -> None:
            if not row["native_session_id"]:
                raise ActionRejected("no_native_id", "the runtime hasn't reported its session id yet")
            if row["runtime_state"] in ("running", "waiting_for_input") and not interrupt_running:
                raise ActionRejected(
                    "turn_in_progress",
                    "a turn is running; handing off now ends it. Interrupt "
                    "first, or confirm handing off anyway",
                )

        async def taken() -> None:
            await self.store.writer.acall(lambda c: _move_lease(c, session_id, "terminal"))

        return await self._control(
            session_id,
            "handoff",
            {"interrupt_running": interrupt_running},
            action_id,
            {"type": "command", "session": session_id, "command": {"cmd": "stop"}},
            check=check,
            on_submitted=taken,
        )

    async def reclaim(self, session_id: str, *, action_id: str) -> Action:
        """Take control back from the terminal: only when nothing else holds the native session, by resuming
        it through a new runtime host (TR-046)."""
        from robin.runtime.terminal import holders

        row = self._row(session_id)

        def check(r: Any) -> None:
            if r["control_owner"] != "terminal":
                raise ActionRejected("not_handed_off", "Robin already controls this session")
            busy = holders(r["native_session_id"] or "")
            if busy:
                raise ActionRejected(
                    "terminal_still_open", f"close the terminal session first (process {busy})"
                )

        async def taken() -> None:
            await self.store.writer.acall(lambda c: _move_lease(c, session_id, "robin"))

        if row is not None:
            check(row)  # before minting: a rejected request must not replace a live session's credential
        env = await self._relaunch_env(session_id, row)
        msg = {
            "type": "launch",
            "session": session_id,
            "runtime": row["runtime"] if row else "",
            "cwd": (row["execution_cwd"] if row else None),
            "action_id": action_id,
            "env": env,
        }
        return await self._control(
            session_id, "reclaim", {}, action_id, msg, needs_connection=False, check=check, on_submitted=taken
        )

    async def resume(self, session_id: str, *, action_id: str) -> Action:
        """Start a new runtime host on a session whose host ended (after a supervisor restart, say), resuming
        the same native session. It starts no turn (TR-072)."""
        row = self._row(session_id)

        def check(r: Any) -> None:
            if r["control_owner"] == "terminal":
                raise ActionRejected("handed_off", "handed off to a terminal; take control back instead")
            if r["connection_state"] == "connected":
                raise ActionRejected("already_running", "the session is already running")
            if not r["native_session_id"]:
                raise ActionRejected("no_native_id", "the runtime never reported a session to resume")

        if row is not None:
            check(row)  # before minting: a rejected request must not replace a live session's credential
        env = await self._relaunch_env(session_id, row)
        msg = {
            "type": "launch",
            "session": session_id,
            "runtime": row["runtime"] if row else "",
            "cwd": (row["execution_cwd"] if row else None),
            "action_id": action_id,
            "env": env,
        }
        return await self._control(
            session_id, "resume", {}, action_id, msg, needs_connection=False, check=check
        )

    async def takeover(self, session_id: str, text: str, *, action_id: str) -> Action:
        """Continue a session started outside Robin: the same session becomes managed and a new runtime host
        resumes its native session with `text` as the first turn. Refused, changing nothing, while a
        terminal may still be writing it."""
        from robin.runtime.terminal import holders

        row = self._row(session_id)

        def check(r: Any) -> None:
            if r["mode"] != "observed":
                raise ActionRejected("already_managed", "Robin already manages this session")
            if not r["native_session_id"]:
                raise ActionRejected("no_native_id", "the runtime never reported a session to resume")
            if not r["execution_cwd"] or not os.path.isdir(r["execution_cwd"]):
                raise ActionRejected("no_cwd", "the session's folder isn't available")
            busy = holders(r["native_session_id"])
            if busy:
                raise ActionRejected(
                    "terminal_still_open", f"close it in your terminal first (process {busy[0]})"
                )
            if _recently_written(self.store, session_id):
                raise ActionRejected(
                    "terminal_still_open",
                    "it looks like it's still running in a terminal; close it there first",
                )

        async def flip() -> None:
            await asyncio.to_thread(_catch_up, self.store, session_id, row["runtime"])
            await self.store.writer.acall(lambda c: _take_over(c, session_id))

        async def undo() -> None:
            await self.store.writer.acall(lambda c: _give_back(c, session_id))

        if await self.get_action(action_id) is not None:
            # A repeat reports the stored outcome (or a conflict) without checking, minting or sending again.
            return await self._control(session_id, "takeover", {"text": text}, action_id, {})
        if row is not None:
            check(row)  # before minting: a rejected request must not replace a live session's credential
        env = await self._relaunch_env(session_id, row)
        msg = {
            "type": "launch",
            "session": session_id,
            "runtime": row["runtime"] if row else "",
            "cwd": (row["execution_cwd"] if row else None),
            "prompt": text,
            "action_id": action_id,
            "env": env,
        }
        return await self._control(
            session_id,
            "takeover",
            {"text": text},
            action_id,
            msg,
            needs_connection=False,
            check=check,
            before_dispatch=flip,
            on_rejected=undo,
        )

    async def _relaunch_env(self, session_id: str, row: Any) -> dict[str, str]:
        """What a new host for an existing session needs: native ID, finding tools, credential, isolation,
        permission mode."""
        env = {"ROBIN_RESUME_NATIVE_ID": (row["native_session_id"] or "") if row else ""}
        if row and row["current_task_id"]:
            env["ROBIN_FINDINGS"] = "tool,message"
            url = _service_url(self.store)
            if url:  # a new host gets a new credential; the old one stops working
                from robin import retrieval

                env["ROBIN_AGENT_TOKEN"] = await asyncio.to_thread(retrieval.mint, self.store, session_id)
                env["ROBIN_SERVICE_URL"] = url
        if row and row["independence"] == "enforced":  # a reviewer stays a reviewer
            self.independence_env(row["runtime"], env)
        if row and row["skip_permissions"]:  # the opt-in made at launch outlives the host
            env["ROBIN_SKIP_PERMISSIONS"] = "1"
        return env

    def _row(self, session_id: str) -> Any:
        with self.store.reader.connect() as conn:
            return (
                conn.execute(select(SessionRow).where(SessionRow.session_id == session_id)).mappings().first()
            )

    def independence_env(self, runtime: str, env: dict[str, str]) -> str:
        """Add the reviewer's deny rule for Robin's data directory where the runtime can enforce one (TR-064).

        Returns "enforced", or "not_isolated" when the runtime has no sandbox to do it with.
        """
        if capabilities_for(runtime).review_independence != "enforced_with_deny_rules":
            return "not_isolated"
        env["ROBIN_DENY_READ"] = os.path.realpath(self.store.data_dir)  # findings, database, CLI token
        return "enforced"

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
        check: Callable[[Any], None] | None = None,
        on_submitted: Callable[[], Awaitable[None]] | None = None,
        record: Callable[[Connection], None] | None = None,
        before_dispatch: Callable[[], Awaitable[None]] | None = None,
        on_rejected: Callable[[], Awaitable[None]] | None = None,
    ) -> Action:
        with self.store.reader.connect() as conn:
            known = conn.execute(
                select(SessionRow.session_id).where(SessionRow.session_id == session_id)
            ).first()
        if known is None:  # nothing to record an action against
            raise ActionRejected("unknown_session", f"no session {session_id}")
        async with self._locks[session_id]:

            def create(c: Connection) -> tuple[Action, bool]:
                result = ledger.create(
                    c,
                    action_id=action_id,
                    session_id=session_id,
                    action_type=action_type,
                    payload=payload,
                    expected_generation=expected_generation,
                )
                if result[1] and record is not None:
                    record(c)
                return result

            action, created = await self.store.writer.acall(create)
            if not created:
                return action  # identical repeat: report the stored outcome, never resend
            try:
                self._preconditions(session_id, action_type, needs_connection, expected_generation, check)
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
            if before_dispatch is not None:
                try:
                    await before_dispatch()
                except ActionRejected as exc:
                    await self._move(action_id, "failed", error_code=exc.code, error_message=str(exc))
                    raise
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
                if on_submitted is not None:
                    await on_submitted()  # still under the session lock: queued controls see the new owner
                return await self._move(action_id, "submitted")
            if on_rejected is not None:
                await on_rejected()
            return await self._move(
                action_id, "failed", error_code=str(reply.get("error", "rejected")), error_message=str(reply)
            )

    def _preconditions(
        self,
        session_id: str,
        action_type: str,
        needs_connection: bool,
        expected_generation: int | None,
        check: Callable[[Any], None] | None = None,
    ) -> None:
        with self.store.reader.connect() as conn:
            row = (
                conn.execute(select(SessionRow).where(SessionRow.session_id == session_id)).mappings().first()
            )
        if row is None:
            raise ActionRejected("unknown_session", f"no session {session_id}")
        if row["mode"] != "managed" and action_type != "takeover":
            raise ActionRejected("observed_session", "observed sessions are read-only in Robin")
        ok, reason = capabilities_for(row["runtime"]).allows(action_type)
        if not ok:
            raise ActionRejected("unsupported", reason or "unsupported")
        if row["control_owner"] == "terminal" and action_type != "reclaim":
            raise ActionRejected(
                "handed_off", "this session is handed off to a terminal; take control back first"
            )
        if needs_connection and row["connection_state"] != "connected":
            raise ActionRejected("not_connected", f"session is {row['connection_state']}")
        if expected_generation is not None and expected_generation != row["lease_generation"]:
            raise ActionRejected(
                "stale_generation",
                f"expected lease generation {expected_generation}, session is at {row['lease_generation']}",
            )
        if check is not None:
            check(row)

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
log = logging.getLogger(__name__)


def _move_lease(conn: Connection, session_id: str, owner: str) -> None:
    """One owner at a time: a new lease generation invalidates controls prepared under the old one."""
    now = utc_now()
    gen = (
        conn.execute(select(SessionRow.lease_generation).where(SessionRow.session_id == session_id)).scalar()
        or 0
    )
    conn.execute(
        update(SessionRow)
        .where(SessionRow.session_id == session_id)
        .values(control_owner=owner, lease_generation=gen + 1, updated_at=now)
    )
    if owner == "terminal":
        conn.execute(
            update(AttentionItem)
            .where(AttentionItem.session_id == session_id, AttentionItem.state.in_(("open", "acknowledged")))
            .values(state="handed_off", updated_at=now)
        )


def _take_over(conn: Connection, session_id: str) -> None:
    """The observed session becomes managed. Its state is reset: hooks may have left it "running", and only
    the new host's events say what it's doing now."""
    took = conn.execute(
        update(SessionRow)
        .where(SessionRow.session_id == session_id, SessionRow.mode == "observed")
        .values(mode="managed", runtime_state="unknown", active_run_id=None, archived=0, updated_at=utc_now())
    ).rowcount
    if not took:
        raise ActionRejected("already_managed", "Robin already manages this session")
    _move_lease(conn, session_id, "robin")


def _give_back(conn: Connection, session_id: str) -> None:
    """Undo a takeover whose host never started: the session is observed (and continuable) again."""
    conn.execute(
        update(SessionRow)
        .where(SessionRow.session_id == session_id, SessionRow.mode == "managed")
        .values(mode="observed", control_owner=None, connection_state="disconnected", updated_at=utc_now())
    )


def _transcripts(store: Store, session_id: str) -> list[str]:
    with store.reader.connect() as conn:
        return [
            p
            for p in conn.execute(
                select(SourceCheckpoint.path).where(
                    SourceCheckpoint.session_id == session_id,
                    SourceCheckpoint.kind == "jsonl_file",
                    SourceCheckpoint.state == "active",
                )
            ).scalars()
            if p
        ]


def _recently_written(store: Store, session_id: str) -> bool:
    now = time.time()
    for path in _transcripts(store, session_id):
        try:
            if now - os.stat(path).st_mtime < TERMINAL_QUIET_S:
                return True
        except OSError:
            continue
    return False


def _catch_up(store: Store, session_id: str, runtime: str) -> None:
    """Read what the transcripts gained since the last scan: once managed, nothing tails them again."""
    from pathlib import Path

    from robin.adapters.history import HISTORY
    from robin.events.tailer import JsonlTailer

    adapter = HISTORY.get(runtime)
    if adapter is None:
        return
    for path in _transcripts(store, session_id):
        if not os.path.exists(path):
            continue
        tailer = JsonlTailer(store, Path(path), session_id=session_id, runtime=runtime, mapper=adapter.mapper)
        try:
            tailer.poll_once()
        finally:
            tailer.close()


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
            if ev.get("type") == "finding_submission":  # attributed from this session's launch identity
                _project_finding(
                    conn, store, r.session_id, r.global_cursor, r.source_time or r.ingested_at, ev
                )
    if rows:
        conn.execute(
            sqlite_insert(Meta)
            .values(key=PROJECTION_KEY, value=str(rows[-1].global_cursor))
            .on_conflict_do_update(index_elements=["key"], set_={"value": str(rows[-1].global_cursor)})
        )
    return len(rows)


def _project_finding(
    conn: Connection, store: Store, session_id: str, cursor: int, at: datetime, ev: dict[str, Any]
) -> None:
    """A submission an agent controls must never stall projection for every session: if recording it fails,
    record a rejection instead (in a savepoint, so the rest of the batch commits)."""
    try:
        with conn.begin_nested():
            findings.project(conn, store.blobs, session_id, cursor, at, ev)
    except Exception as exc:
        log.exception("couldn't record finding submission at cursor %s", cursor)
        raw = ev.get("payload")
        payload: dict[str, Any] = raw if isinstance(raw, dict) else {}
        sub_id = str(payload.get("submission_id") or f"failed-{cursor}")[:128]
        runtime = conn.execute(select(SessionRow.runtime).where(SessionRow.session_id == session_id)).scalar()
        task = conn.execute(
            select(SessionRow.current_task_id).where(SessionRow.session_id == session_id)
        ).scalar()
        path = str(ev["path"]) if ev.get("path") in ("tool", "message") else "message"
        try:
            with conn.begin_nested():
                findings.reject(
                    conn,
                    session_id,
                    str(runtime),
                    task,
                    sub_id,
                    path,
                    [f"Robin couldn't record this finding ({type(exc).__name__})"],
                    cursor,
                    at,
                )
        except Exception:
            log.exception(
                "couldn't record the rejection either; skipping the submission at cursor %s", cursor
            )


def _project_one(
    conn: Connection, session_id: str, cursor: int, at: datetime, correlation: str, ev: dict[str, Any]
) -> None:
    state.apply(conn, session_id, cursor, at, ev)
    attention.project(conn, session_id, cursor, at, ev)
    if ev.get("type") == "host_exited":
        # The tools' credential dies with the host (TR-066); one minted after this exit, for a new host,
        # stays.
        conn.execute(
            delete(AgentCredential).where(
                AgentCredential.session_id == session_id, AgentCredential.created_at <= at
            )
        )
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
    elif kind == "host_exited" and _pending(conn, session_id, "handoff"):
        # The exit is the handoff's acknowledgement, whether or not the lease has moved yet.
        for handoff_id in _pending(conn, session_id, "handoff"):
            ledger.transition(conn, handoff_id, "acknowledged", native_correlation=correlation)
    elif kind == "host_exited":
        # A host that exits before its session starts means the launch failed.
        never_started = (
            conn.execute(
                select(ControlAction.action_id).where(
                    ControlAction.session_id == session_id,
                    ControlAction.action_type.in_(LAUNCH_TYPES),
                    ControlAction.state.in_(("requested", "submitted", "outcome_unknown")),
                )
            )
            .scalars()
            .all()
        )
        for launch_id in never_started:
            failed = ledger.get(conn, launch_id)
            if failed is not None and failed.action_type == "takeover":
                _give_back(conn, session_id)  # still continuable, not a managed session with no host
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
                    ControlAction.action_type.in_(LAUNCH_TYPES),
                    ControlAction.state.in_(("requested", "submitted", "outcome_unknown")),
                )
            )
            .scalars()
            .all()
        )
        for launch_id in launches:
            ledger.transition(conn, launch_id, "acknowledged", native_correlation=correlation)


def _pending(conn: Connection, session_id: str, action_type: str) -> list[str]:
    return list(
        conn.execute(
            select(ControlAction.action_id).where(
                ControlAction.session_id == session_id,
                ControlAction.action_type == action_type,
                ControlAction.state.in_(("requested", "submitted", "outcome_unknown")),
            )
        ).scalars()
    )


def _service_url(store: Store) -> str | None:
    try:
        return str(json.loads((store.data_dir / "service.json").read_text())["url"])
    except (OSError, ValueError, KeyError):
        return None
