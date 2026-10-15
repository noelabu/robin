"""Ingesting the session supervisor's spooled event stream (TR-006, TR-032).

Each supervisor generation is its own source, "supervisor:<gen>", and records are identified by
"<gen>:<seq>". Replays return Duplicate. Acknowledge a cursor to the supervisor only after ingest()
returns: by then the events are committed.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel
from sqlalchemy import Connection, select

from robin.events.envelope import EvidenceKind, NewEvent, canonical_json
from robin.events.ingest import (
    CheckpointAdvance,
    Duplicate,
    Inserted,
    PreparedQuarantine,
    ingest_batch,
    open_generation,
    read_checkpoint,
)
from robin.store import Store
from robin.store.models import Session as SessionRow


class SupervisorRecord(BaseModel):
    """One spooled record as the supervisor sends it."""

    seq: int
    gen: str
    session: str  # the Robin session ID the runtime host was launched for
    at: float
    event: dict[str, Any]


@dataclass(frozen=True)
class MappedSupervisorEvent:
    event_type: str = "unknown.raw"
    evidence_kind: EvidenceKind = EvidenceKind.UNKNOWN
    native_session_id: str | None = None
    native_event_id: str | None = None


type SupervisorMapper = Callable[[SupervisorRecord], MappedSupervisorEvent]


def default_supervisor_mapper(record: SupervisorRecord) -> MappedSupervisorEvent:
    """Runtime-specific mappers replace this in the adapters; the native type stays in the payload."""
    return MappedSupervisorEvent()


class SupervisorIngestor:
    def __init__(self, store: Store, mapper: SupervisorMapper = default_supervisor_mapper):
        self.store = store
        self.mapper = mapper

    @staticmethod
    def source_id(gen: str) -> str:
        return f"supervisor:{gen}"

    def checkpoint(self, gen: str) -> int:
        with self.store.reader.connect() as conn:
            cp = read_checkpoint(conn, self.source_id(gen))
        return cp.offset if cp else 0

    def ingest(self, records: Sequence[SupervisorRecord]) -> list[Inserted | Duplicate]:
        results: list[Inserted | Duplicate] = []
        by_gen: dict[str, list[SupervisorRecord]] = {}
        for r in records:
            by_gen.setdefault(r.gen, []).append(r)
        for gen, recs in by_gen.items():
            results += self._ingest_generation(gen, sorted(recs, key=lambda r: r.seq))
        return results

    def _ingest_generation(self, gen: str, records: list[SupervisorRecord]) -> list[Inserted | Duplicate]:
        source_id = self.source_id(gen)
        runtime_of = self._sessions({r.session for r in records})

        def ensure_source(conn: Connection) -> int | None:
            cp = read_checkpoint(conn, source_id)
            if cp is not None:
                return cp.offset
            open_generation(
                conn,
                source_id=source_id,
                kind="supervisor",
                runtime="supervisor",
                reason="initial",
                label=gen,
            )
            return None  # no history yet: the first record can't be a gap

        last = self.store.writer.call(ensure_source)
        events: list[NewEvent] = []
        quarantines: list[PreparedQuarantine] = []
        for r in records:
            payload = canonical_json(r.event)
            if r.session not in runtime_of:
                quarantines.append(
                    PreparedQuarantine(
                        source_id,
                        0,
                        r.seq,
                        r.seq,
                        "unknown_session",
                        f"record {gen}:{r.seq} names a session Robin doesn't know",
                        len(payload),
                        payload,
                    )
                )
                last = r.seq if last is None else max(last, r.seq)  # quarantined, not missing
                continue
            if last is not None and r.seq > last + 1:  # sequence gap: say so instead of hiding it (TR-035)
                gap = canonical_json({"after_seq": last, "next_seq": r.seq})
                events.append(
                    NewEvent(
                        runtime=runtime_of[r.session],
                        session_id=r.session,
                        source_id=source_id,
                        source_generation=0,
                        source_event_id=f"{gen}:gap:{last}-{r.seq}",
                        source_seq=r.seq,
                        event_type="connection.gap",
                        evidence_kind=EvidenceKind.OBSERVED,
                        payload=gap,
                    )
                )
            m = self.mapper(r)
            events.append(
                NewEvent(
                    runtime=runtime_of[r.session],
                    session_id=r.session,
                    native_session_id=m.native_session_id,
                    source_id=source_id,
                    source_generation=0,
                    source_event_id=f"{gen}:{r.seq}",
                    source_seq=r.seq,
                    native_event_id=m.native_event_id,
                    source_time=datetime.fromtimestamp(r.at, UTC),
                    event_type=m.event_type,
                    evidence_kind=m.evidence_kind,
                    payload=payload,
                )
            )
            last = r.seq if last is None else max(last, r.seq)
        top = max(r.seq for r in records)
        return ingest_batch(
            self.store, events, quarantines, CheckpointAdvance(source_id, 0, top, expected_offset=None)
        )

    def _sessions(self, ids: set[str]) -> dict[str, str]:
        with self.store.reader.connect() as conn:
            rows = conn.execute(
                select(SessionRow.session_id, SessionRow.runtime).where(SessionRow.session_id.in_(ids))
            ).all()
        return {sid: runtime for sid, runtime in rows}
