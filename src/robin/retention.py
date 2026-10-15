"""Storage cap on copied activity (TR-081).

What counts is what Robin copied from sessions: event payloads (inline and in blobs) and their search text.
Findings, syntheses, approved packets, delivery manifests, agent read records and audit entries never count
and are never pruned. The default cap is 20 GB (`storage_cap_gb` in `<data dir>/settings.toml`); at 80%
Robin warns, and above the cap the oldest copied activity is pruned first, down to PRUNE_TO of the cap.
A pruned event keeps its identity (source, sequence, content hash) and reads as unavailable; blobs nothing
references any more are removed.
"""

import json
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, delete, func, insert, select, text, update

from robin.ids import utc_now
from robin.store import Store
from robin.store.models import (
    ArtifactVersion,
    Blob,
    ContextPacket,
    ControlAction,
    Event,
    PrunedEvent,
    Quarantine,
    RetrievalRead,
    TaskAttachment,
)

DEFAULT_CAP_GB = 20.0
WARN_AT = 0.8
PRUNE_TO = 0.9  # prune below the cap, so it isn't hit again at once
TOMBSTONE = json.dumps({"_robin_pruned": True})
BATCH = 1000


def cap_bytes(data_dir: Path) -> int:
    path = data_dir / "settings.toml"
    gb = DEFAULT_CAP_GB
    if path.is_file():
        try:
            gb = float(tomllib.loads(path.read_text()).get("storage_cap_gb", DEFAULT_CAP_GB))
        except (ValueError, tomllib.TOMLDecodeError):
            gb = DEFAULT_CAP_GB
    return int(gb * 1024**3)


@dataclass
class Usage:
    events_inline: int
    events_blobs: int
    search_text: int
    protected: int  # findings, packets, manifests, reads: shown, never counted
    cap: int

    @property
    def counted(self) -> int:
        return self.events_inline + self.events_blobs + self.search_text

    @property
    def state(self) -> str:
        if self.counted > self.cap:
            return "over"
        return "warning" if self.counted >= WARN_AT * self.cap else "ok"

    def view(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "counted": self.counted,
            "state": self.state,
            "ratio": round(self.counted / self.cap, 4) if self.cap else 0.0,
        }


def usage(store: Store) -> Usage:
    with store.reader.connect() as conn:
        pruned = select(PrunedEvent.global_cursor)
        inline = conn.execute(
            select(func.coalesce(func.sum(func.length(Event.payload_inline)), 0)).where(
                Event.payload_inline.is_not(None), Event.global_cursor.not_in(pruned)
            )
        ).scalar_one()
        blobs = conn.execute(
            select(func.coalesce(func.sum(Blob.size), 0)).where(
                Blob.sha256.in_(select(Event.payload_blob).where(Event.payload_blob.is_not(None)))
            )
        ).scalar_one()
        search_text = conn.execute(
            text("SELECT coalesce(sum(length(body)), 0) FROM search_index WHERE kind = 'event'")
        ).scalar_one()
        protected = (
            conn.execute(select(func.coalesce(func.sum(ArtifactVersion.size), 0))).scalar_one()
            + conn.execute(select(func.coalesce(func.sum(ContextPacket.size), 0))).scalar_one()
        )
    return Usage(int(inline), int(blobs), int(search_text), int(protected), cap_bytes(store.data_dir))


def plan(store: Store, target: int | None = None) -> dict[str, Any]:
    """What pruning would remove to get under `target` bytes (default PRUNE_TO of the cap): oldest first."""
    u = usage(store)
    goal = int(PRUNE_TO * u.cap) if target is None else target
    need = u.counted - goal
    cursors: list[int] = []
    freed = 0
    sessions: set[str] = set()
    oldest = newest = None
    if need > 0:
        after = 0
        with store.reader.connect() as conn:
            while freed < need:
                rows = conn.execute(
                    select(Event.global_cursor, Event.session_id, Event.payload_size, Event.ingested_at)
                    .where(
                        Event.global_cursor > after,
                        Event.global_cursor.not_in(select(PrunedEvent.global_cursor)),
                    )
                    .order_by(Event.global_cursor)
                    .limit(BATCH)
                ).all()
                if not rows:
                    break
                for r in rows:
                    cursors.append(r.global_cursor)
                    freed += 2 * int(r.payload_size)  # the payload and (about) its search text
                    sessions.add(r.session_id)
                    oldest = oldest or r.ingested_at
                    newest = r.ingested_at
                    if freed >= need:
                        break
                after = rows[-1].global_cursor
    return {
        "usage": u.view(),
        "target": goal,
        "events": len(cursors),
        "approx_bytes": freed,
        "sessions": len(sessions),
        "oldest": oldest.isoformat() if oldest else None,
        "newest": newest.isoformat() if newest else None,
        "through_cursor": cursors[-1] if cursors else None,
        "_cursors": cursors,
    }


def prune(store: Store, target: int | None = None) -> dict[str, Any]:
    """Prune per plan(). Event rows stay; payloads become tombstones; orphaned blobs are removed."""
    p = plan(store, target)
    cursors: list[int] = p.pop("_cursors")
    now = utc_now()
    blobs: set[str] = set()
    for i in range(0, len(cursors), BATCH):
        chunk = cursors[i : i + BATCH]

        def work(conn: Connection, chunk: list[int] = chunk) -> None:
            rows = conn.execute(
                select(Event.global_cursor, Event.payload_size, Event.payload_blob).where(
                    Event.global_cursor.in_(chunk)
                )
            ).all()
            for r in rows:
                if r.payload_blob:
                    blobs.add(r.payload_blob)
                conn.execute(
                    insert(PrunedEvent).values(
                        global_cursor=r.global_cursor, original_size=r.payload_size, pruned_at=now
                    )
                )
            conn.execute(
                update(Event)
                .where(Event.global_cursor.in_(chunk))
                .values(payload_inline=TOMBSTONE, payload_blob=None)
            )
            for c in chunk:
                conn.execute(text("DELETE FROM search_index WHERE rowid = :r"), {"r": c})

        store.writer.call(work)
    removed = _drop_orphans(store, blobs)
    return {**p, "pruned_events": len(cursors), "removed_blobs": removed, "after": usage(store).view()}


def _drop_orphans(store: Store, candidates: set[str]) -> int:
    """Remove blobs that nothing references any more (rows first, then files)."""
    if not candidates:
        return 0
    refs = (
        (Event.payload_blob,),
        (Quarantine.payload_blob,),
        (ControlAction.payload_blob,),
        (ArtifactVersion.content_blob,),
        (ContextPacket.payload_blob,),
        (TaskAttachment.snapshot_blob,),
        (RetrievalRead.content_blob,),
    )

    def work(conn: Connection) -> list[str]:
        gone = []
        for sha in candidates:
            if any(conn.execute(select(col).where(col == sha).limit(1)).first() for (col,) in refs):
                continue
            conn.execute(delete(Blob).where(Blob.sha256 == sha))
            gone.append(sha)
        return gone

    gone = store.writer.call(work)
    for sha in gone:
        store.blobs.path_for(sha).unlink(missing_ok=True)
    return len(gone)


def is_pruned_payload(raw: str | bytes | None) -> bool:
    return raw is not None and (raw if isinstance(raw, str) else raw.decode(errors="replace")) == TOMBSTONE


def enforce(store: Store) -> dict[str, Any] | None:
    """Automatic pruning when over the cap. Returns what was done, or None when under it."""
    if usage(store).state != "over":
        return None
    return prune(store)
