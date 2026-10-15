"""Scanning native session stores into Robin (FR-010, FR-041).

Each found session becomes an observed Robin session (identified by host, runtime and native ID), linked to
a registered project by working folder, and its transcript files are ingested incrementally through the
crash-safe tailer. Scans are idempotent. A broken file or a failing runtime is reported and skipped; it
never stops the others (NFR-06). Sessions Robin launched itself are skipped: their live stream is the
authoritative copy, and correlating it with the transcript needs native identity (TR-032).
"""

from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select, update

from robin.adapters.history import HISTORY, NativeSession
from robin.events.ingest import ensure_session
from robin.events.tailer import JsonlTailer, RecordMapper
from robin.ids import utc_now
from robin.store import Store
from robin.store.models import Project
from robin.store.models import Session as SessionRow


@dataclass
class RuntimeScan:
    runtime: str
    root: str
    found: int = 0
    new_sessions: int = 0
    skipped_managed: int = 0
    events: int = 0
    quarantined: int = 0
    errors: list[str] = field(default_factory=list)


def scan(
    store: Store, runtimes: list[str] | None = None, roots: dict[str, Path] | None = None
) -> list[RuntimeScan]:
    results = []
    for name in runtimes or list(HISTORY):
        adapter = HISTORY[name]
        root = (roots or {}).get(name) or adapter.default_root()
        report = RuntimeScan(name, str(root))
        results.append(report)
        if not root.exists():
            continue
        try:
            sessions = list(adapter.discover(root))
        except Exception as exc:  # this runtime's discoverer failed; the others carry on
            report.errors.append(f"discovery failed: {type(exc).__name__}: {exc}")
            continue
        projects = _projects(store)
        for native in sessions:
            report.found += 1
            try:
                _ingest_session(store, native, adapter.mapper, projects, report)
            except Exception as exc:
                report.errors.append(f"{native.path.name}: {type(exc).__name__}: {exc}")
    return results


def _ingest_session(
    store: Store,
    native: NativeSession,
    mapper: RecordMapper,
    projects: list[tuple[str, str]],
    report: RuntimeScan,
) -> None:
    with store.reader.connect() as conn:
        existing = conn.execute(
            select(SessionRow.session_id, SessionRow.mode).where(
                SessionRow.host_id == store.host_id,
                SessionRow.runtime == native.runtime,
                SessionRow.native_session_id == native.native_id,
            )
        ).first()
    if existing is not None and existing.mode == "managed":
        report.skipped_managed += 1
        return
    if existing is None:
        report.new_sessions += 1
    sid = ensure_session(
        store,
        runtime=native.runtime,
        mode="observed",
        native_session_id=native.native_id,
        execution_cwd=native.cwd,
    )
    project_id = _project_for(native.cwd, projects)
    if project_id:
        store.writer.call(
            lambda c: c.execute(
                update(SessionRow)
                .where(SessionRow.session_id == sid, SessionRow.project_id.is_(None))
                .values(project_id=project_id, updated_at=utc_now())
            )
        )
    for path in (native.path, *native.extra_files):
        tailer = JsonlTailer(store, path, session_id=sid, runtime=native.runtime, mapper=mapper)
        try:
            r = tailer.poll_once()
        finally:
            tailer.close()
        report.events += r.inserted
        report.quarantined += r.quarantined


def _projects(store: Store) -> list[tuple[str, str]]:
    with store.reader.connect() as conn:
        rows = conn.execute(select(Project.project_id, Project.path)).all()
    return sorted(((pid, path) for pid, path in rows), key=lambda p: -len(p[1]))  # deepest first


def _project_for(cwd: str | None, projects: list[tuple[str, str]]) -> str | None:
    if not cwd:
        return None
    here = Path(cwd)
    for pid, path in projects:
        if here == Path(path) or here.is_relative_to(path):
            return pid
    return None
