"""SQLite engines: one writer connection, read-only readers (TR-004)."""

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, String, create_engine, event
from sqlalchemy.engine import Dialect
from sqlalchemy.pool import NullPool, StaticPool
from sqlalchemy.types import TypeDecorator

from robin.ids import from_utc_text, to_utc_text

MIN_SQLITE = (3, 35, 0)  # INSERT ... RETURNING


class UTCDateTime(TypeDecorator[datetime]):
    """UTC-aware datetimes stored as fixed-width text, which sorts chronologically."""

    impl = String(27)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> str | None:
        return None if value is None else to_utc_text(value)

    def process_result_value(self, value: str | None, dialect: Dialect) -> datetime | None:
        return None if value is None else from_utc_text(value)


def check_sqlite_version() -> None:
    if sqlite3.sqlite_version_info < MIN_SQLITE:
        raise RuntimeError(f"SQLite {sqlite3.sqlite_version} is too old; Robin needs 3.35 or later")


def _pragmas(dbapi_conn: Any, *, readonly: bool) -> None:
    # Take transaction control away from the sqlite3 module so BEGIN is ours.
    dbapi_conn.isolation_level = None
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA busy_timeout=5000")
    if readonly:
        cur.execute("PRAGMA query_only=ON")
    else:
        cur.execute("PRAGMA journal_mode=WAL")
        # FULL: a committed event survives power loss before its source is acknowledged.
        cur.execute("PRAGMA synchronous=FULL")
    cur.close()


def writer_engine(db_path: Path) -> Engine:
    """The single write connection. Every transaction starts with BEGIN IMMEDIATE."""
    engine = create_engine(
        f"sqlite:///{db_path}", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )

    @event.listens_for(engine, "connect")
    def _connect(dbapi_conn: Any, _record: Any) -> None:
        _pragmas(dbapi_conn, readonly=False)

    @event.listens_for(engine, "begin")
    def _begin(conn: Any) -> None:
        conn.exec_driver_sql("BEGIN IMMEDIATE")

    return engine


def reader_engine(db_path: Path) -> Engine:
    """Read-only connections. Open the writer first so the WAL files exist."""
    engine = create_engine(f"sqlite:///file:{db_path}?mode=ro&uri=true", poolclass=NullPool)

    @event.listens_for(engine, "connect")
    def _connect(dbapi_conn: Any, _record: Any) -> None:
        _pragmas(dbapi_conn, readonly=True)

    @event.listens_for(engine, "begin")
    def _begin(conn: Any) -> None:
        conn.exec_driver_sql("BEGIN")

    return engine
