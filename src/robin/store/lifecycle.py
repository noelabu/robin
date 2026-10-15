"""Opening and closing Robin's store: directories, lock, migrations, reconciliation, writer."""

import fcntl
import json
import os
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import Connection, Engine, insert, select, update

from robin.ids import new_id, utc_now
from robin.paths import ArtifactStoreRoot, default_data_dir, secure_mkdir, secure_touch
from robin.store.blobs import BlobStore
from robin.store.db import check_sqlite_version, reader_engine, writer_engine
from robin.store.migrate import MIGRATIONS, migrate
from robin.store.models import AuditEntry, Blob, ControlAction, Event, Meta, Quarantine
from robin.store.writer import WriterThread


class StoreLocked(RuntimeError):
    """Another Robin process owns this data directory."""


@dataclass
class Store:
    data_dir: Path
    db_path: Path
    artifacts: ArtifactStoreRoot
    blobs: BlobStore
    writer: WriterThread
    reader: Engine
    host_id: str
    backup_path: Path | None
    _lock_fd: int

    def close(self) -> None:
        self.writer.close()
        self.reader.dispose()
        fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
        os.close(self._lock_fd)


def _acquire_lock(path: Path) -> int:
    secure_touch(path)
    fd = os.open(path, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise StoreLocked(f"another Robin process is using {path.parent}") from None
    return fd


def open_store(
    data_dir: Path | None = None, *, migrations: Path = MIGRATIONS, writer_queue: int = 256
) -> Store:
    check_sqlite_version()
    data_dir = secure_mkdir((data_dir or default_data_dir()).expanduser().resolve())
    lock_fd = _acquire_lock(data_dir / "robin.lock")
    writer: WriterThread | None = None
    reader: Engine | None = None
    try:
        db_path = data_dir / "robin.db"
        secure_touch(db_path)  # SQLite gives -wal and -shm the database file's mode
        backup_path = migrate(db_path, data_dir / "backups", migrations)

        artifacts = ArtifactStoreRoot(data_dir / "artifacts")
        blobs = BlobStore(artifacts.path)
        writer = WriterThread(writer_engine(db_path), maxsize=writer_queue)
        writer.start()
        host_id = writer.call(_ensure_host_id)
        _reconcile_blobs(writer, blobs)
        reader = reader_engine(db_path)
        return Store(data_dir, db_path, artifacts, blobs, writer, reader, host_id, backup_path, lock_fd)
    except BaseException:
        # Stop the (non-daemon) writer and close its connection before giving up the lock.
        if writer is not None:
            writer.close()
        if reader is not None:
            reader.dispose()
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
        raise


def _ensure_host_id(conn: Connection) -> str:
    row = conn.execute(select(Meta.value).where(Meta.key == "host_id")).scalar_one_or_none()
    if row:
        return row
    host_id = new_id()
    conn.execute(insert(Meta).values(key="host_id", value=host_id))
    conn.execute(insert(Meta).values(key="created_at", value=utc_now().isoformat()))
    return host_id


def _reconcile_blobs(writer: WriterThread, blobs: BlobStore) -> None:
    """Startup reconciliation (TR-022): drop orphaned files, flag rows whose file is gone."""

    def referenced(conn: Connection) -> set[str]:
        rows = set(conn.execute(select(Blob.sha256)).scalars())
        for col in (Event.payload_blob, Quarantine.payload_blob, ControlAction.payload_blob):
            rows |= {r for r in conn.execute(select(col).where(col.is_not(None))).scalars() if r is not None}
        return rows

    known = writer.call(referenced)
    _, missing = blobs.reconcile(known)
    if not missing:
        return

    def mark_missing(conn: Connection) -> None:
        conn.execute(update(Blob).where(Blob.sha256.in_(missing)).values(state="missing"))
        conn.execute(
            insert(AuditEntry).values(
                audit_id=new_id(),
                at=utc_now(),
                actor="system",
                action="blob.reconcile",
                affected_ids=json.dumps(missing),
                outcome="missing_on_disk",
            )
        )

    writer.call(mark_missing)
