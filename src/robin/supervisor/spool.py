"""The supervisor's durable event spool (TR-006).

Records are appended to numbered segment files and fsynced before they're sent. A segment is deleted
once the service has acknowledged every record in it. Segments left by an earlier supervisor generation
are loaded on start, so events emitted before a supervisor crash still reach the service.

The spool is bounded: past max_bytes the oldest unacknowledged segment is dropped and a gap is recorded,
so the service reports lost coverage instead of the supervisor running out of disk.
"""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from robin.paths import FILE_MODE, secure_mkdir


@dataclass
class _Segment:
    path: Path
    first_seq: int
    last_seq: int
    size: int
    records: list[dict[str, Any]] = field(default_factory=list)


class Spool:
    def __init__(self, directory: Path, *, segment_bytes: int = 8 << 20, max_bytes: int = 1 << 30):
        self.dir = secure_mkdir(directory)
        self.segment_bytes = segment_bytes
        self.max_bytes = max_bytes
        self.segments: list[_Segment] = []
        self.seq = 0
        self.acked = 0
        self.dropped: list[tuple[int, int]] = []  # (first_seq, last_seq) lost to the size bound
        self._fd: int | None = None
        self._fd_path: Path | None = None
        self._load()

    def _load(self) -> None:
        for path in sorted(self.dir.glob("segment-*.jsonl")):
            records = []
            with open(path, "rb") as f:
                for line in f:
                    if not line.endswith(b"\n"):
                        break  # torn final write: never acknowledged, never sent
                    records.append(json.loads(line))
            if not records:
                path.unlink()
                continue
            self.segments.append(_Segment(path, records[0]["seq"], records[-1]["seq"], path.stat().st_size,
                                          records))
            self.seq = max(self.seq, records[-1]["seq"])
        ack_file = self.dir / "acked"
        if ack_file.exists():
            self.acked = int(ack_file.read_text() or 0)
        # Acknowledged segments get deleted, so the files alone can under-report how far seq has gone.
        self.seq = max(self.seq, self.acked)
        self._trim()

    def append(self, record: dict[str, Any]) -> dict[str, Any]:
        """Assign the next seq, write and fsync. Returns the stored record."""
        self.seq += 1
        record = {"seq": self.seq, **record}
        line = (json.dumps(record, separators=(",", ":"), ensure_ascii=False, default=str) + "\n").encode()
        seg = self._writable(len(line))
        os.write(self._fd_for(seg), line)
        os.fsync(self._fd_for(seg))
        seg.last_seq, seg.size = self.seq, seg.size + len(line)
        seg.records.append(record)
        self._enforce_bound()
        return record

    def after(self, seq: int) -> list[dict[str, Any]]:
        return [r for s in self.segments if s.last_seq > seq for r in s.records if r["seq"] > seq]

    def ack(self, seq: int) -> None:
        if seq <= self.acked:
            return
        self.acked = min(seq, self.seq)
        tmp = self.dir / "acked.tmp"
        tmp.write_text(str(self.acked))
        os.replace(tmp, self.dir / "acked")
        self._trim()

    def close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd, self._fd_path = None, None

    def total_bytes(self) -> int:
        return sum(s.size for s in self.segments)

    # -- internals ---------------------------------------------------------

    def _writable(self, needed: int) -> _Segment:
        """The segment this process is appending to; a new one when it would grow past segment_bytes."""
        current = self.segments[-1] if self.segments and self.segments[-1].path == self._fd_path else None
        if current is None or (current.size > 0 and current.size + needed > self.segment_bytes):
            self.close()
            path = self.dir / f"segment-{self.seq:020d}.jsonl"
            self._fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, FILE_MODE)
            self._fd_path = path
            current = _Segment(path, self.seq, self.seq, 0)
            self.segments.append(current)
        return current

    def _fd_for(self, seg: _Segment) -> int:
        assert self._fd is not None and seg.path == self._fd_path
        return self._fd

    def _trim(self) -> None:
        """Delete segments whose every record has been acknowledged (never the one being written)."""
        while self.segments and self.segments[0].last_seq <= self.acked and \
                self.segments[0].path != self._fd_path:
            self.segments.pop(0).path.unlink(missing_ok=True)

    def _enforce_bound(self) -> None:
        while self.total_bytes() > self.max_bytes and len(self.segments) > 1:
            lost = self.segments.pop(0)
            lost.path.unlink(missing_ok=True)
            self.dropped.append((lost.first_seq, lost.last_seq))
