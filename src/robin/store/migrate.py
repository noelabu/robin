"""Schema migrations: compatibility check, pre-migration backup, one-transaction upgrade (TR-073)."""

import os
import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from alembic.util.exc import CommandError
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

from robin.ids import utc_now
from robin.paths import secure_mkdir, secure_touch

MIGRATIONS = Path(__file__).resolve().parent / "migrations"


class SchemaTooNew(RuntimeError):
    """The database was written by a newer Robin; this build refuses to touch it."""


class MigrationFailed(RuntimeError):
    """An upgrade failed and was rolled back; the database is at its previous revision."""


def alembic_config(script_location: Path = MIGRATIONS) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(script_location))
    return cfg


def current_revision(db_path: Path) -> str | None:
    if not db_path.exists():
        return None
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        has_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='alembic_version'"
        ).fetchone()
        if not has_table:
            return None
        row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
    return row[0] if row else None


def backup(db_path: Path, backups_dir: Path, label: str) -> Path:
    """Consistent copy through SQLite's online backup API, checked and fsynced before it counts."""
    secure_mkdir(backups_dir)
    stamp = utc_now().strftime("%Y%m%dT%H%M%SZ")
    final = backups_dir / f"robin-{label}-{stamp}.db"
    partial = final.with_suffix(".db.partial")
    secure_touch(partial)
    # sqlite3's `with` commits but doesn't close; both close before the rename, and the copy leaves WAL
    # mode, so every page is in the one file that gets renamed (no -wal/-shm left behind or split off).
    src, dst = sqlite3.connect(db_path), sqlite3.connect(partial)
    try:
        src.backup(dst)
        dst.execute("PRAGMA journal_mode=DELETE")
        if dst.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise MigrationFailed(f"backup of {db_path} failed its integrity check")
    finally:
        src.close()
        dst.close()
    fd = os.open(partial, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(partial, final)
    return final


def migrate(db_path: Path, backups_dir: Path, script_location: Path = MIGRATIONS) -> Path | None:
    """Bring the database to head. Returns the backup path when an existing database was upgraded."""
    cfg = alembic_config(script_location)
    script = ScriptDirectory.from_config(cfg)
    head = script.get_current_head()
    current = current_revision(db_path)
    if current is not None:
        try:
            script.get_revision(current)
        except CommandError as exc:
            raise SchemaTooNew(f"database revision {current} is newer than this build (head {head})") from exc
    if current == head:
        return None

    backup_path = backup(db_path, backups_dir, current) if current is not None else None
    engine = create_engine(f"sqlite:///{db_path}", poolclass=NullPool)
    try:
        with engine.connect() as conn:
            dbapi = conn.connection.driver_connection
            assert dbapi is not None
            dbapi.isolation_level = None  # explicit BEGIN/COMMIT below
            # Must be set outside a transaction; batch-mode table rebuilds need it off.
            conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
            conn.exec_driver_sql("BEGIN IMMEDIATE")
            try:
                cfg.attributes["connection"] = conn
                command.upgrade(cfg, "head")
                problems = conn.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
                if problems:
                    raise MigrationFailed(f"foreign key check failed after upgrade: {problems[:5]}")
                conn.exec_driver_sql("COMMIT")
            except BaseException as exc:
                conn.exec_driver_sql("ROLLBACK")
                if isinstance(exc, MigrationFailed):
                    raise
                where = f"; backup: {backup_path}" if backup_path else ""
                raise MigrationFailed(f"upgrade from {current} to {head} rolled back: {exc}{where}") from exc
    finally:
        engine.dispose()
    return backup_path
