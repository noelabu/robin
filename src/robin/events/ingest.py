"""Committing events, quarantines and the source checkpoint in one transaction (TR-004, TR-031–033).

Payloads over INLINE_LIMIT are written to the blob store before the transaction; the writer only runs SQL.
Dedup is the UNIQUE(source_id, dedup_key) constraint: a repeat returns Duplicate, not an error.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Connection, func, select, update
from sqlalchemy.dialects.sqlite import insert

from robin.events.envelope import EventEnvelope, EventFamily, EvidenceKind, NewEvent, content_hash
from robin.ids import new_id, utc_now
from robin.store import Store
from robin.store.models import Blob, Event, Quarantine, SourceCheckpoint
from robin.store.models import Session as SessionRow

INLINE_LIMIT = 64 * 1024


class CheckpointConflict(RuntimeError):
    """Another reader advanced this source since it was read; re-read and retry."""


@dataclass(frozen=True)
class Checkpoint:
    source_id: str
    generation: int
    kind: str
    offset: int
    file_dev: int | None
    file_ino: int | None
    anchor_hash: str | None
    generation_label: str | None


@dataclass(frozen=True)
class CheckpointAdvance:
    source_id: str
    generation: int
    new_offset: int
    # Byte sources pass the offset they read from (optimistic check);
    # sequence sources pass None and the offset becomes max(offset, new_offset).
    expected_offset: int | None
    anchor_hash: str | None = None


@dataclass(frozen=True)
class PreparedQuarantine:
    source_id: str
    generation: int
    offset_start: int
    offset_end: int
    reason: str
    diagnostic: str  # never contains record content (TR-083)
    byte_length: int
    raw: bytes | None = None  # kept locally in the 0600 blob store for reprocessing


@dataclass(frozen=True)
class Inserted:
    event_id: str
    global_cursor: int
    duplicate_of: int | None


@dataclass(frozen=True)
class Duplicate:
    dedup_key: str


def ensure_session(
    store: Store,
    *,
    runtime: str,
    mode: str,
    native_session_id: str | None = None,
    execution_cwd: str | None = None,
    session_id: str | None = None,
) -> str:
    """Return the Robin session for this native identity, creating it if needed."""

    def work(conn: Connection) -> str:
        if native_session_id is not None:
            existing = conn.execute(
                select(SessionRow.session_id).where(
                    SessionRow.host_id == store.host_id,
                    SessionRow.runtime == runtime,
                    SessionRow.native_session_id == native_session_id,
                )
            ).scalar_one_or_none()
            if existing:
                return existing
        sid = session_id or new_id()
        now = utc_now()
        conn.execute(
            insert(SessionRow).values(
                session_id=sid,
                host_id=store.host_id,
                runtime=runtime,
                native_session_id=native_session_id,
                mode=mode,
                execution_cwd=execution_cwd,
                created_at=now,
                updated_at=now,
            )
        )
        return sid

    return store.writer.call(work)


def open_generation(
    conn: Connection,
    *,
    source_id: str,
    kind: str,
    runtime: str,
    reason: str,
    session_id: str | None = None,
    path: str | None = None,
    file_dev: int | None = None,
    file_ino: int | None = None,
    label: str | None = None,
) -> int:
    """Supersede the source's active generation (if any) and start the next one at offset 0."""
    now = utc_now()
    last = conn.execute(
        select(func.max(SourceCheckpoint.generation)).where(SourceCheckpoint.source_id == source_id)
    ).scalar()
    generation = 0 if last is None else last + 1
    conn.execute(
        update(SourceCheckpoint)
        .where(SourceCheckpoint.source_id == source_id, SourceCheckpoint.state == "active")
        .values(state="superseded", updated_at=now)
    )
    conn.execute(
        insert(SourceCheckpoint).values(
            source_id=source_id,
            generation=generation,
            kind=kind,
            generation_label=label,
            session_id=session_id,
            runtime=runtime,
            path=path,
            file_dev=file_dev,
            file_ino=file_ino,
            offset=0,
            state="active",
            reason=reason,
            created_at=now,
            updated_at=now,
        )
    )
    return generation


def read_checkpoint(conn: Connection, source_id: str, generation: int | None = None) -> Checkpoint | None:
    """The active generation of a source, or a specific one."""
    q = select(SourceCheckpoint).where(SourceCheckpoint.source_id == source_id)
    q = (
        q.where(SourceCheckpoint.state == "active")
        if generation is None
        else q.where(SourceCheckpoint.generation == generation)
    )
    row = conn.execute(q).mappings().first()
    if row is None:
        return None
    return Checkpoint(
        row["source_id"],
        row["generation"],
        row["kind"],
        row["offset"],
        row["file_dev"],
        row["file_ino"],
        row["anchor_hash"],
        row["generation_label"],
    )


def ingest_batch(
    store: Store,
    events: Sequence[NewEvent],
    quarantines: Sequence[PreparedQuarantine],
    advance: CheckpointAdvance,
) -> list[Inserted | Duplicate]:
    """Commit events, quarantine rows and the checkpoint advance atomically."""
    spilled: dict[str, tuple[str, int]] = {}  # event_id -> (sha, size)
    for ev in events:
        if len(ev.payload) > INLINE_LIMIT or not _is_utf8(ev.payload):
            spilled[ev.event_id] = (store.blobs.put(ev.payload), len(ev.payload))
    q_blobs: dict[int, str] = {}
    for i, q in enumerate(quarantines):
        if q.raw is not None:
            q_blobs[i] = store.blobs.put(q.raw)

    def work(conn: Connection) -> list[Inserted | Duplicate]:
        cp = read_checkpoint(conn, advance.source_id, advance.generation)
        if cp is None:
            raise CheckpointConflict(f"no checkpoint for {advance.source_id} generation {advance.generation}")
        if advance.expected_offset is not None and cp.offset != advance.expected_offset:
            raise CheckpointConflict(
                f"{advance.source_id}: expected offset {advance.expected_offset}, found {cp.offset}"
            )
        now = utc_now()
        sizes = {sha: size for sha, size in spilled.values()}
        sizes |= {sha: len(quarantines[i].raw or b"") for i, sha in q_blobs.items()}
        for sha, size in sizes.items():
            conn.execute(
                insert(Blob)
                .values(sha256=sha, size=size, media_type="application/octet-stream", created_at=now)
                .on_conflict_do_nothing()
            )
        results: list[Inserted | Duplicate] = []
        for ev in events:
            duplicate_of = _duplicate_candidate(conn, ev) if ev.source_event_id is None else None
            blob = spilled.get(ev.event_id)
            stmt = (
                insert(Event)
                .values(
                    event_id=ev.event_id,
                    schema_version=ev.schema_version,
                    session_id=ev.session_id,
                    runtime=ev.runtime,
                    native_session_id=ev.native_session_id,
                    run_id=ev.run_id,
                    source_id=ev.source_id,
                    source_generation=ev.source_generation,
                    source_event_id=ev.source_event_id,
                    source_seq=ev.source_seq,
                    source_offset_end=ev.source_offset_end,
                    dedup_key=ev.dedup_key,
                    native_event_id=ev.native_event_id,
                    duplicate_of=duplicate_of,
                    source_time=ev.source_time,
                    ingested_at=now,
                    event_family=ev.event_family.value,
                    event_type=ev.event_type,
                    evidence_kind=ev.evidence_kind.value,
                    payload_inline=None if blob else ev.payload.decode("utf-8"),
                    payload_blob=blob[0] if blob else None,
                    payload_size=len(ev.payload),
                    content_hash=ev.content_hash,
                    task_id=ev.task_id,
                )
                .on_conflict_do_nothing(index_elements=["source_id", "dedup_key"])
                .returning(Event.global_cursor)
            )
            cursor = conn.execute(stmt).scalar_one_or_none()
            results.append(
                Duplicate(ev.dedup_key) if cursor is None else Inserted(ev.event_id, cursor, duplicate_of)
            )
        for i, q in enumerate(quarantines):
            conn.execute(
                insert(Quarantine)
                .values(
                    quarantine_id=new_id(),
                    source_id=q.source_id,
                    generation=q.generation,
                    offset_start=q.offset_start,
                    offset_end=q.offset_end,
                    reason=q.reason,
                    diagnostic=q.diagnostic,
                    byte_length=q.byte_length,
                    content_hash=content_hash(q.raw) if q.raw is not None else None,
                    payload_blob=q_blobs.get(i),
                    created_at=now,
                )
                .on_conflict_do_nothing(index_elements=["source_id", "generation", "offset_start"])
            )
        new_offset = (
            advance.new_offset if advance.expected_offset is not None else max(cp.offset, advance.new_offset)
        )
        values: dict[str, Any] = {"offset": new_offset, "updated_at": now}
        if advance.anchor_hash is not None:
            values["anchor_hash"] = advance.anchor_hash
        conn.execute(
            update(SourceCheckpoint)
            .where(
                SourceCheckpoint.source_id == advance.source_id,
                SourceCheckpoint.generation == advance.generation,
            )
            .values(**values)
        )
        return results

    return store.writer.call(work)


def _is_utf8(data: bytes) -> bool:
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _duplicate_candidate(conn: Connection, ev: NewEvent) -> int | None:
    """Same content in an earlier generation of the same source: an unresolved duplicate (TR-032)."""
    if ev.source_generation == 0:
        return None
    return conn.execute(
        select(Event.global_cursor)
        .where(
            Event.source_id == ev.source_id,
            Event.content_hash == ev.content_hash,
            Event.source_generation < ev.source_generation,
        )
        .order_by(Event.global_cursor)
        .limit(1)
    ).scalar()


def read_events(
    store: Store, *, session_id: str | None = None, after: int = 0, limit: int = 500
) -> list[EventEnvelope]:
    q = select(Event).where(Event.global_cursor > after).order_by(Event.global_cursor).limit(limit)
    if session_id is not None:
        q = q.where(Event.session_id == session_id)
    with store.reader.connect() as conn:
        rows = list(conn.execute(q).mappings())
    return [_envelope(r) for r in rows]


def _envelope(r: Any) -> EventEnvelope:
    return EventEnvelope(
        global_cursor=r["global_cursor"],
        event_id=r["event_id"],
        schema_version=r["schema_version"],
        runtime=r["runtime"],
        session_id=r["session_id"],
        native_session_id=r["native_session_id"],
        run_id=r["run_id"],
        source_id=r["source_id"],
        source_generation=r["source_generation"],
        source_event_id=r["source_event_id"],
        source_seq=r["source_seq"],
        source_offset_end=r["source_offset_end"],
        native_event_id=r["native_event_id"],
        duplicate_of=r["duplicate_of"],
        source_time=r["source_time"],
        ingested_at=r["ingested_at"],
        event_family=EventFamily(r["event_family"]),
        event_type=r["event_type"],
        evidence_kind=EvidenceKind(r["evidence_kind"]),
        payload_ref=f"blob:sha256:{r['payload_blob']}" if r["payload_blob"] else None,
        payload_size=r["payload_size"],
        task_id=r["task_id"],
        content_hash=r["content_hash"],
    )


def read_payload(store: Store, global_cursor: int) -> bytes:
    with store.reader.connect() as conn:
        row = conn.execute(
            select(Event.payload_inline, Event.payload_blob).where(Event.global_cursor == global_cursor)
        ).one()
    return (
        row.payload_inline.encode("utf-8")
        if row.payload_inline is not None
        else store.blobs.read(row.payload_blob)
    )


def read_payload_json(store: Store, global_cursor: int) -> Any:
    return json.loads(read_payload(store, global_cursor))
