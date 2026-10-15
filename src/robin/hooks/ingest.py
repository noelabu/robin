"""Hook events into the event store (FR-041, TR-076).

Each relayed hook becomes one `reported` event on the session's hook source, keyed by the relay's own id,
so a repeated delivery is stored once. Hooks carry signals (a turn started, a tool ran, the session ended),
not transcript content, so they don't duplicate what tailing ingests. Sessions Robin launched are skipped:
their live stream is already the record. Dropped relays become coverage gaps.
"""

import json
import os
import threading
import uuid
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, select, update

from robin.events.envelope import EvidenceKind, NewEvent
from robin.events.ingest import (
    CheckpointAdvance,
    ensure_session,
    ingest_batch,
    open_generation,
    read_checkpoint,
)
from robin.ids import utc_now
from robin.store import Store
from robin.store.models import Session as SessionRow
from robin.workspace import tasks as ws

# Native hook names (Claude and Codex share theirs; Pi's are extension events) -> event type, runtime state.
EVENTS: dict[str, tuple[str, str | None]] = {
    "SessionStart": ("session.started", "idle"),
    "session_start": ("session.started", "idle"),
    "UserPromptSubmit": ("session.turn_started", "running"),
    "turn_start": ("session.turn_started", "running"),
    "PreToolUse": ("tool.started", "running"),
    "tool_call": ("tool.started", "running"),
    "PostToolUse": ("tool.result", "running"),
    "tool_result": ("tool.result", "running"),
    "Stop": ("session.turn_completed", "idle"),
    "agent_end": ("session.turn_completed", "idle"),
    "SubagentStart": ("session.subagent_started", None),
    "SubagentStop": ("session.subagent_stopped", None),
    "Notification": ("session.notification", None),
    "SessionEnd": ("session.stopped", "stopped"),
    "session_shutdown": ("session.stopped", "stopped"),
}

_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
_drain_lock = threading.Lock()


class HookRejected(Exception):
    pass


def record(store: Store, runtime: str, body: dict[str, Any]) -> dict[str, Any]:
    payload = body.get("payload")
    if not isinstance(payload, dict):
        raise HookRejected("payload must be an object")
    native = str(payload.get("session_id") or "").strip()
    relay_id = str(body.get("relay_id") or "")
    if not native or not relay_id:
        raise HookRejected("hook payload has no session_id")
    name = str(payload.get("hook_event_name") or "")
    event_type, state = EVENTS.get(name, ("unknown.raw", None))
    cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else None

    with store.reader.connect() as conn:
        mode = conn.execute(
            select(SessionRow.mode).where(
                SessionRow.runtime == runtime, SessionRow.native_session_id == native
            )
        ).scalar()
    if mode == "managed":
        return {"recorded": False, "reason": "managed"}  # Robin already streams this session
    sid = ensure_session(store, runtime=runtime, mode="observed", native_session_id=native, execution_cwd=cwd)
    at = _time(body.get("relayed_at"))
    source_id = f"hook:{runtime}:{native}"
    with _locks[source_id]:
        seq = _next_seq(store, source_id, runtime, sid)
        event = NewEvent(
            runtime=runtime,
            session_id=sid,
            native_session_id=native,
            source_id=source_id,
            source_generation=0,
            source_event_id=relay_id,
            source_seq=seq,
            source_time=at,
            event_type=event_type,
            evidence_kind=EvidenceKind.REPORTED,
            payload=json.dumps({"hook": name, **payload}, ensure_ascii=False).encode(),
        )
        ingest_batch(store, [event], [], CheckpointAdvance(source_id, 0, seq, None))
    projects = ws.project_paths(store)

    def observe(conn: Connection) -> None:
        values: dict[str, Any] = {"state_evidence_at": at, "updated_at": utc_now()}
        if state:
            values["runtime_state"] = state
        conn.execute(update(SessionRow).where(SessionRow.session_id == sid).values(**values))
        project = ws.project_for(cwd, projects)
        if project:
            conn.execute(
                update(SessionRow)
                .where(SessionRow.session_id == sid, SessionRow.project_id.is_(None))
                .values(project_id=project)
            )

    store.writer.call(observe)
    return {"recorded": True, "session_id": sid, "event_type": event_type}


def _next_seq(store: Store, source_id: str, runtime: str, sid: str) -> int:
    def work(conn: Connection) -> int:
        cp = read_checkpoint(conn, source_id)
        if cp is None:
            open_generation(
                conn, source_id=source_id, kind="hook", runtime=runtime, reason="initial", session_id=sid
            )
            return 1
        return cp.offset + 1

    return int(store.writer.call(work))


def _time(value: Any) -> datetime:
    try:
        return datetime.fromtimestamp(float(value), UTC)
    except (TypeError, ValueError, OverflowError, OSError):
        return utc_now()


def drain_drops(store: Store) -> int:
    """Turn relays that couldn't reach Robin into coverage gaps on their sessions. Returns how many."""
    with _drain_lock:  # hook requests and startup drain concurrently; one at a time
        path = store.data_dir / "hooks" / "dropped.jsonl"
        try:
            # A unique claim, so new drops go to a fresh file while this one is read.
            os.replace(path, path.with_name(f"dropped-{uuid.uuid4().hex}.draining"))
        except FileNotFoundError:
            pass
        count = 0
        # Includes claims left by a drain that crashed; gap ids are deterministic, so redoing one is harmless.
        for claimed in sorted(path.parent.glob("dropped-*.draining")):
            try:
                lines = claimed.read_text(errors="replace").splitlines()
            except OSError:
                continue
            count += _gaps_from(store, lines)
            claimed.unlink(missing_ok=True)  # only once its gaps are recorded
        return count


def _gaps_from(store: Store, lines: list[str]) -> int:
    count = 0
    for line in lines:
        try:
            d = json.loads(line)
            runtime, native = str(d["runtime"]), str(d["session_id"])
        except (ValueError, KeyError, TypeError):
            continue
        if not native or runtime not in ("claude", "codex", "pi"):
            continue
        body = {
            "relay_id": f"gap-{native}-{d.get('at')}",
            "relayed_at": d.get("at"),
            "payload": {
                "session_id": native,
                "cwd": d.get("cwd") or None,
                "hook_event_name": "_robin_gap",
                "missed_event": d.get("event"),
            },
        }
        _gap(store, runtime, body)
        count += 1
    return count


def _gap(store: Store, runtime: str, body: dict[str, Any]) -> None:
    payload = body["payload"]
    native = payload["session_id"]
    with store.reader.connect() as conn:
        mode = conn.execute(
            select(SessionRow.mode).where(
                SessionRow.runtime == runtime, SessionRow.native_session_id == native
            )
        ).scalar()
    if mode == "managed":
        return
    sid = ensure_session(
        store, runtime=runtime, mode="observed", native_session_id=native, execution_cwd=payload.get("cwd")
    )
    source_id = f"hook:{runtime}:{native}"
    with _locks[source_id]:
        seq = _next_seq(store, source_id, runtime, sid)
        ingest_batch(
            store,
            [
                NewEvent(
                    runtime=runtime,
                    session_id=sid,
                    native_session_id=native,
                    source_id=source_id,
                    source_generation=0,
                    source_event_id=str(body["relay_id"]),
                    source_seq=seq,
                    source_time=_time(body.get("relayed_at")),
                    event_type="connection.gap",
                    evidence_kind=EvidenceKind.OBSERVED,
                    payload=json.dumps({"reason": "a hook event couldn't reach Robin", **payload}).encode(),
                )
            ],
            [],
            CheckpointAdvance(source_id, 0, seq, None),
        )


def drops_path(data_dir: Path) -> Path:
    return data_dir / "hooks" / "dropped.jsonl"
