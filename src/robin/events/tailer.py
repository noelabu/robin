"""Incremental, crash-safe ingestion of an append-only JSONL file (TR-033).

The checkpoint is a byte offset into one generation of the file. Records are split on LF only (a JSON
string can't contain a raw LF, and 0x0A never appears inside a multi-byte UTF-8 sequence), so U+2028 and
friends stay inside their record. An incomplete last line is never emitted and the checkpoint never moves
past it. Events, quarantines and the checkpoint advance commit together, and re-reading from an old offset
yields the same dedup keys, so a crash at any point neither loses nor duplicates events.
"""

import hashlib
import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from robin.events.envelope import EvidenceKind, NewEvent
from robin.events.ingest import (
    Checkpoint,
    CheckpointAdvance,
    Duplicate,
    Inserted,
    PreparedQuarantine,
    ingest_batch,
    open_generation,
    read_checkpoint,
)
from robin.store import Store

ANCHOR_BYTES = 4096


@dataclass(frozen=True)
class MapperContext:
    source_id: str
    generation: int
    offset_start: int
    offset_end: int
    runtime: str


@dataclass(frozen=True)
class MappedRecord:
    event_type: str = "unknown.raw"
    evidence_kind: EvidenceKind = EvidenceKind.UNKNOWN
    source_time: datetime | None = None
    native_event_id: str | None = None
    native_session_id: str | None = None
    run_id: str | None = None


type RecordMapper = Callable[[Any, MapperContext], MappedRecord | None]


def default_mapper(record: Any, ctx: MapperContext) -> MappedRecord | None:
    """Stores every record as unknown.raw. Runtime adapters supply real mappers; None skips a record."""
    return MappedRecord()


@dataclass
class PollResult:
    inserted: int = 0
    duplicates: int = 0
    quarantined: int = 0
    generation: int = 0
    offset: int = 0
    pending_tail_bytes: int = 0
    new_generations: list[str] = field(default_factory=list)


class JsonlTailer:
    def __init__(
        self,
        store: Store,
        path: Path,
        *,
        session_id: str,
        runtime: str,
        mapper: RecordMapper = default_mapper,
        chunk: int = 1 << 20,
        max_record: int = 16 << 20,
        batch_records: int = 500,
        batch_bytes: int = 4 << 20,
    ):
        self.store = store
        self.path = Path(path)
        self.source_id = "file:" + os.path.realpath(self.path)
        self.session_id = session_id
        self.runtime = runtime
        self.mapper = mapper
        self.chunk = chunk
        self.max_record = max_record
        self.batch_records = batch_records
        self.batch_bytes = batch_bytes
        self._fd: int | None = None
        self._fd_ident: tuple[int, int] | None = None

    def close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd, self._fd_ident = None, None

    def poll_once(self) -> PollResult:
        result = PollResult()
        try:
            fd = os.open(self.path, os.O_RDONLY)
        except FileNotFoundError:
            self._drain_rotated(None, result)
            return result
        try:
            st = os.fstat(fd)
            ident = (st.st_dev, st.st_ino)
            if self._fd is not None and self._fd_ident == ident:
                os.close(fd)  # same file: keep the descriptor we already hold
                fd = self._fd
            with self.store.reader.connect() as conn:
                cp = read_checkpoint(conn, self.source_id)

            if cp is None:
                cp = self._new_generation(ident, "initial", result)
            elif (cp.file_dev, cp.file_ino) != ident:
                self._drain_rotated(cp, result)
                cp = self._new_generation(ident, "rotated", result)
            elif st.st_size < cp.offset:
                cp = self._new_generation(ident, "truncated", result)
            elif cp.offset > 0 and cp.anchor_hash != _anchor(fd, cp.offset):
                cp = self._new_generation(ident, "rewritten", result)

            if self._fd is not None and self._fd != fd:
                os.close(self._fd)
            self._fd, self._fd_ident = fd, ident
        except BaseException:
            if fd != self._fd:
                os.close(fd)
            raise
        self._drain(fd, cp, result, final=False)
        return result

    # -- internals ---------------------------------------------------------

    def _new_generation(self, ident: tuple[int, int], reason: str, result: PollResult) -> Checkpoint:
        def work(conn: Any) -> Checkpoint:
            gen = open_generation(
                conn,
                source_id=self.source_id,
                kind="jsonl_file",
                runtime=self.runtime,
                reason=reason,
                session_id=self.session_id,
                path=str(self.path),
                file_dev=ident[0],
                file_ino=ident[1],
            )
            cp = read_checkpoint(conn, self.source_id, gen)
            assert cp is not None
            return cp

        cp = self.store.writer.call(work)
        result.new_generations.append(reason)
        return cp

    def _drain_rotated(self, cp: Checkpoint | None, result: PollResult) -> None:
        """Read the old inode to EOF if we still hold it, so nothing appended before rotation is lost."""
        if self._fd is None:
            return
        if cp is None:
            with self.store.reader.connect() as conn:
                cp = read_checkpoint(conn, self.source_id)
        if cp is not None and (cp.file_dev, cp.file_ino) == self._fd_ident:
            self._drain(self._fd, cp, result, final=True)
        os.close(self._fd)
        self._fd, self._fd_ident = None, None

    def _drain(self, fd: int, cp: Checkpoint, result: PollResult, *, final: bool) -> None:
        committed = cp.offset  # offset the checkpoint holds; flushes advance it
        pos = cp.offset  # start of the next unread record
        buf = bytearray()
        scan = 0
        skipping_from: int | None = None  # start of an oversized record we're skipping past
        skip_pos = 0
        events: list[NewEvent] = []
        quarantines: list[PreparedQuarantine] = []
        pending_bytes = 0

        def flush(new_offset: int) -> None:
            nonlocal committed, events, quarantines, pending_bytes
            if new_offset == committed and not events and not quarantines:
                return
            res = ingest_batch(
                self.store,
                events,
                quarantines,
                CheckpointAdvance(
                    self.source_id,
                    cp.generation,
                    new_offset,
                    expected_offset=committed,
                    anchor_hash=_anchor(fd, new_offset),
                ),
            )
            result.inserted += sum(isinstance(r, Inserted) for r in res)
            result.duplicates += sum(isinstance(r, Duplicate) for r in res)
            result.quarantined += len(quarantines)
            committed = new_offset
            events, quarantines, pending_bytes = [], [], 0

        read_at = cp.offset
        while True:
            chunk = os.pread(fd, self.chunk, read_at)
            if not chunk:
                break
            read_at += len(chunk)
            if skipping_from is not None:
                i = chunk.find(b"\n")
                if i < 0:
                    skip_pos += len(chunk)
                    continue
                end = skip_pos + i + 1
                quarantines.append(self._oversized(cp, skipping_from, end))
                pos, skipping_from = end, None
                chunk = chunk[i + 1 :]
            buf += chunk
            while (i := buf.find(b"\n", scan)) >= 0:
                start, end = pos, pos + i + 1
                line = bytes(buf[:i])
                del buf[: i + 1]
                pos, scan = end, 0
                if line.endswith(b"\r"):
                    line = line[:-1]
                if len(line) > self.max_record:
                    quarantines.append(self._oversized(cp, start, end))
                elif line.strip():
                    item = self._record(cp, start, end, line)
                    if isinstance(item, PreparedQuarantine):
                        quarantines.append(item)
                    elif item is not None:
                        events.append(item)
                        pending_bytes += len(line)
                if len(events) + len(quarantines) >= self.batch_records or pending_bytes >= self.batch_bytes:
                    flush(pos)
            scan = len(buf)
            if len(buf) > self.max_record:  # an oversized record that hasn't ended yet
                skipping_from, skip_pos = pos, pos + len(buf)
                buf.clear()
                scan = 0

        tail = len(buf) if skipping_from is None else skip_pos - skipping_from
        if final and tail:
            quarantines.append(
                PreparedQuarantine(
                    self.source_id,
                    cp.generation,
                    pos,
                    pos + tail,
                    "incomplete_at_rotation",
                    f"{tail} bytes without a final newline when the file was rotated",
                    tail,
                )
            )
            pos += tail
            tail = 0
        flush(pos)
        result.generation, result.offset, result.pending_tail_bytes = cp.generation, committed, tail

    def _oversized(self, cp: Checkpoint, start: int, end: int) -> PreparedQuarantine:
        return PreparedQuarantine(
            self.source_id,
            cp.generation,
            start,
            end,
            "oversized",
            f"record of {end - start} bytes exceeds the {self.max_record}-byte limit",
            end - start,
        )

    def _record(
        self, cp: Checkpoint, start: int, end: int, line: bytes
    ) -> NewEvent | PreparedQuarantine | None:
        try:
            record = json.loads(line)
        except ValueError as exc:
            return PreparedQuarantine(
                self.source_id,
                cp.generation,
                start,
                end,
                "invalid_json",
                f"{type(exc).__name__} at byte {getattr(exc, 'pos', '?')} of a {len(line)}-byte record",
                len(line),
                line,
            )
        ctx = MapperContext(self.source_id, cp.generation, start, end, self.runtime)
        try:
            mapped = self.mapper(record, ctx)
        except Exception as exc:
            return PreparedQuarantine(
                self.source_id,
                cp.generation,
                start,
                end,
                "mapper_error",
                f"mapper raised {type(exc).__name__}",
                len(line),
                line,
            )
        if mapped is None:
            return None
        return NewEvent(
            runtime=self.runtime,
            session_id=self.session_id,
            native_session_id=mapped.native_session_id,
            run_id=mapped.run_id,
            source_id=self.source_id,
            source_generation=cp.generation,
            source_event_id=None,
            source_seq=start,
            source_offset_end=end,
            native_event_id=mapped.native_event_id,
            source_time=mapped.source_time,
            event_type=mapped.event_type,
            evidence_kind=mapped.evidence_kind,
            payload=line,
        )


def _anchor(fd: int, offset: int) -> str:
    """Hash of the bytes just before the offset: detects a file rewritten in place to the same size."""
    start = max(0, offset - ANCHOR_BYTES)
    return hashlib.sha256(os.pread(fd, offset - start, start)).hexdigest()
