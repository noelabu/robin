"""Projects and shared tasks (FR-001–003, FR-013, FR-014, FR-042, TR-020, TR-021).

A task is the coordination boundary across agents. Attaching a file stores an approved snapshot and its
hash; the original is never touched. A session can appear in several task histories, but each run is bound
to one task when it starts. Work for a different task starts a new native session by default; continuing
an existing session under another task needs confirmation that discloses the context it keeps.
"""

import hashlib
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, func, insert, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from robin.ids import new_id, utc_now
from robin.paths import ArtifactStoreRoot
from robin.store import Store
from robin.store.models import Blob, Project, Run, Task, TaskAttachment, TaskSession
from robin.store.models import Session as SessionRow

MAX_ATTACHMENT = 16 << 20


class WorkspaceError(Exception):
    def __init__(self, code: str, message: str, status: int = 422, detail: dict[str, Any] | None = None):
        super().__init__(message)
        self.code, self.status, self.detail = code, status, detail or {}


@dataclass(frozen=True)
class GitInfo:
    remote: str | None
    branch: str | None


def git_info(path: Path) -> GitInfo | None:
    """Repository remote and branch, or None for a plain directory (TR-075). Never raises."""

    def git(*args: str) -> str | None:
        try:
            out = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return out.stdout.strip() if out.returncode == 0 and out.stdout.strip() else None

    if git("rev-parse", "--is-inside-work-tree") != "true":
        return None
    # symbolic-ref also works on a repository with no commits yet (an "unborn" HEAD)
    branch = git("symbolic-ref", "--short", "HEAD") or git("rev-parse", "--abbrev-ref", "HEAD")
    return GitInfo(remote=git("config", "--get", "remote.origin.url"), branch=branch)


# -- projects ------------------------------------------------------------------


def register_project(store: Store, path: str, name: str | None = None) -> dict[str, Any]:
    root = Path(path).expanduser()
    if not root.is_absolute() or not root.is_dir():
        raise WorkspaceError("invalid_path", "a project must be an existing absolute directory")
    root = root.resolve()
    _not_overlapping(root, store.artifacts)
    git = git_info(root)
    now = utc_now()

    def work(conn: Connection) -> str:
        existing = conn.execute(select(Project.project_id).where(Project.path == str(root))).scalar()
        if existing:
            conn.execute(
                update(Project)
                .where(Project.project_id == existing)
                .values(
                    git_remote=git.remote if git else None,
                    git_branch=git.branch if git else None,
                    git_checked_at=now,
                    updated_at=now,
                )
            )
            return str(existing)
        pid = new_id()
        conn.execute(
            insert(Project).values(
                project_id=pid,
                path=str(root),
                name=name or root.name,
                git_remote=git.remote if git else None,
                git_branch=git.branch if git else None,
                git_checked_at=now,
                created_at=now,
                updated_at=now,
            )
        )
        return pid

    return get_project(store, store.writer.call(work))


def get_project(store: Store, project_id: str) -> dict[str, Any]:
    with store.reader.connect() as conn:
        row = conn.execute(select(Project).where(Project.project_id == project_id)).mappings().first()
        if row is None:
            raise WorkspaceError("not_found", f"no project {project_id}", 404)
        last = conn.execute(
            select(func.max(SessionRow.updated_at)).where(SessionRow.project_id == project_id)
        ).scalar()
    return _project_view(row, last)


def list_projects(store: Store) -> list[dict[str, Any]]:
    with store.reader.connect() as conn:
        rows = (
            conn.execute(select(Project).where(Project.archived == 0).order_by(Project.name)).mappings().all()
        )
        lasts = dict(
            conn.execute(
                select(SessionRow.project_id, func.max(SessionRow.updated_at)).group_by(SessionRow.project_id)
            ).all()
        )
    return [_project_view(r, lasts.get(r["project_id"])) for r in rows]


def project_paths(store: Store) -> list[tuple[str, str]]:
    with store.reader.connect() as conn:
        rows = conn.execute(select(Project.project_id, Project.path)).all()
    return sorted(((pid, path) for pid, path in rows), key=lambda p: -len(p[1]))  # deepest first


def project_for(cwd: str | None, projects: list[tuple[str, str]]) -> str | None:
    """The registered project whose folder holds `cwd` (the deepest one)."""
    if not cwd:
        return None
    here = Path(cwd)
    for pid, path in projects:
        if here == Path(path) or here.is_relative_to(path):
            return pid
    return None


def _project_view(r: Any, last_activity: Any) -> dict[str, Any]:
    return {
        "project_id": r["project_id"],
        "name": r["name"],
        "path": r["path"],
        "git": {"remote": r["git_remote"], "branch": r["git_branch"]}
        if (r["git_branch"] or r["git_remote"])
        else None,
        "last_activity_at": last_activity.isoformat() if last_activity else None,
        "created_at": r["created_at"].isoformat(),
    }


def _not_overlapping(root: Path, store: ArtifactStoreRoot) -> None:
    if root.is_relative_to(store.path) or store.path.is_relative_to(root):
        raise WorkspaceError("invalid_path", "a project can't contain, or sit inside, Robin's data directory")


# -- tasks ---------------------------------------------------------------------


def create_task(
    store: Store,
    *,
    project_id: str,
    title: str,
    objective: str,
    files: list[str] | None = None,
    instructions: list[str] | None = None,
    expected_outputs: list[str] | None = None,
) -> dict[str, Any]:
    project = get_project(store, project_id)
    snapshots = [_snapshot(store, Path(project["path"]), f) for f in files or []]  # validate before writing
    now = utc_now()
    task_id = new_id()

    def work(conn: Connection) -> None:
        conn.execute(
            insert(Task).values(
                task_id=task_id,
                project_id=project_id,
                title=title,
                objective=objective,
                created_at=now,
                updated_at=now,
            )
        )
        for source, sha, size in snapshots:
            conn.execute(
                sqlite_insert(Blob)
                .values(sha256=sha, size=size, media_type="application/octet-stream", created_at=now)
                .on_conflict_do_nothing()
            )
            conn.execute(
                insert(TaskAttachment).values(
                    attachment_id=new_id(),
                    task_id=task_id,
                    kind="file",
                    source_path=source,
                    snapshot_blob=sha,
                    content_hash=f"sha256:{sha}",
                    size=size,
                    created_at=now,
                )
            )
        for kind, values in (
            ("instruction", instructions or []),
            ("expected_output", expected_outputs or []),
        ):
            for v in values:
                conn.execute(
                    insert(TaskAttachment).values(
                        attachment_id=new_id(),
                        task_id=task_id,
                        kind=kind,
                        text_value=v,
                        content_hash="sha256:" + hashlib.sha256(v.encode()).hexdigest(),
                        created_at=now,
                    )
                )

    store.writer.call(work)
    return get_task(store, task_id)


def _snapshot(store: Store, project_root: Path, file: str) -> tuple[str, str, int]:
    """Read an attached file as an approved snapshot. It must stay inside the project (TR-070)."""
    candidate = Path(file).expanduser()
    if not candidate.is_absolute():
        candidate = project_root / candidate
    resolved = Path(os.path.realpath(candidate))  # follows symlinks: an escape is caught below
    if not resolved.is_relative_to(project_root):
        raise WorkspaceError("invalid_attachment", f"{file} is outside the project")
    if not resolved.is_file():
        raise WorkspaceError("invalid_attachment", f"{file} is not a regular file")
    size = resolved.stat().st_size
    if size > MAX_ATTACHMENT:
        raise WorkspaceError("invalid_attachment", f"{file} is larger than {MAX_ATTACHMENT >> 20} MiB")
    sha = store.blobs.put(resolved.read_bytes())
    return str(resolved), sha, size


def get_task(store: Store, task_id: str) -> dict[str, Any]:
    with store.reader.connect() as conn:
        t = conn.execute(select(Task).where(Task.task_id == task_id)).mappings().first()
        if t is None:
            raise WorkspaceError("not_found", f"no task {task_id}", 404)
        attachments = (
            conn.execute(
                select(TaskAttachment)
                .where(TaskAttachment.task_id == task_id)
                .order_by(TaskAttachment.created_at)
            )
            .mappings()
            .all()
        )
        sessions = (
            conn.execute(
                select(
                    TaskSession,
                    SessionRow.runtime,
                    SessionRow.runtime_state,
                    SessionRow.connection_state,
                    SessionRow.current_task_id,
                )
                .join(SessionRow, SessionRow.session_id == TaskSession.session_id)
                .where(TaskSession.task_id == task_id)
                .order_by(TaskSession.attached_at)
            )
            .mappings()
            .all()
        )
    return {
        **_task_view(t),
        "attachments": [
            {
                "attachment_id": a["attachment_id"],
                "kind": a["kind"],
                "source_path": a["source_path"],
                "text": a["text_value"],
                "content_hash": a["content_hash"],
                "size": a["size"],
            }
            for a in attachments
        ],
        "sessions": [
            {
                "session_id": s["session_id"],
                "runtime": s["runtime"],
                "role": s["role"],
                "runtime_state": s["runtime_state"],
                "connection_state": s["connection_state"],
                "current": s["current_task_id"] == task_id,
                "retained_context_disclosed": bool(s["retained_context_disclosed"]),
                "attached_at": s["attached_at"].isoformat(),
            }
            for s in sessions
        ],
    }


def list_tasks(
    store: Store, *, project_id: str | None = None, include_archived: bool = False
) -> list[dict[str, Any]]:
    q = select(Task).order_by(Task.updated_at.desc())
    if project_id:
        q = q.where(Task.project_id == project_id)
    if not include_archived:
        q = q.where(Task.archived == 0)
    with store.reader.connect() as conn:
        return [_task_view(t) for t in conn.execute(q).mappings()]


def _task_view(t: Any) -> dict[str, Any]:
    return {
        "task_id": t["task_id"],
        "project_id": t["project_id"],
        "title": t["title"],
        "objective": t["objective"],
        "status": t["status"],
        "completion_evidence": t["completion_evidence"],
        "archived": bool(t["archived"]),
        "revision": t["revision"],
        "created_at": t["created_at"].isoformat(),
        "updated_at": t["updated_at"].isoformat(),
    }


def update_task(store: Store, task_id: str, *, expected_revision: int, **changes: Any) -> dict[str, Any]:
    """Optimistic update. Completing needs evidence of the user's decision (FR-014)."""
    allowed = {"title", "objective", "status", "completion_evidence", "archived"}
    unknown = set(changes) - allowed
    if unknown:
        raise WorkspaceError("invalid_request", f"can't change {sorted(unknown)}")
    if changes.get("status") == "completed" and not (changes.get("completion_evidence") or "").strip():
        raise WorkspaceError("evidence_required", "completing a task needs the evidence you based it on")
    values = {k: (int(v) if k == "archived" else v) for k, v in changes.items() if v is not None}

    def work(conn: Connection) -> None:
        current = conn.execute(select(Task.revision).where(Task.task_id == task_id)).scalar()
        if current is None:
            raise WorkspaceError("not_found", f"no task {task_id}", 404)
        if current != expected_revision:
            raise WorkspaceError(
                "stale_revision", f"task is at revision {current}", 409, {"revision": current}
            )
        conn.execute(
            update(Task)
            .where(Task.task_id == task_id)
            .values(**values, revision=current + 1, updated_at=utc_now())
        )

    store.writer.call(work)
    return get_task(store, task_id)


# -- sessions and tasks --------------------------------------------------------


def attach_session(
    store: Store,
    task_id: str,
    session_id: str,
    *,
    role: str | None = None,
    confirm_retained_context: bool = False,
) -> dict[str, Any]:
    """Associate a session with a task. A session already bound to another task keeps that task's context;
    continuing it here needs explicit confirmation, and takes effect from its next run (FR-013)."""
    get_task(store, task_id)
    now = utc_now()

    def work(conn: Connection) -> None:
        s = conn.execute(
            select(SessionRow.current_task_id).where(SessionRow.session_id == session_id)
        ).first()
        if s is None:
            raise WorkspaceError("not_found", f"no session {session_id}", 404)
        existing = conn.execute(
            select(TaskSession).where(TaskSession.task_id == task_id, TaskSession.session_id == session_id)
        ).first()
        current = s.current_task_id
        if current not in (None, task_id) and not confirm_retained_context:
            runs = conn.execute(
                select(func.count())
                .select_from(Run)
                .where(Run.session_id == session_id, Run.task_id == current)
            ).scalar()
            raise WorkspaceError(
                "confirmation_required",
                f"this session already carries context from another task ({runs} run(s)); continuing it here "
                "keeps that context. Start a new session instead, or confirm to continue.",
                409,
                {"current_task_id": current, "runs": runs},
            )
        if existing is None:
            conn.execute(
                insert(TaskSession).values(
                    task_id=task_id,
                    session_id=session_id,
                    role=role,
                    retained_context_disclosed=int(current not in (None, task_id)),
                    attached_at=now,
                )
            )
        # The latest choice wins: bind now, switch at the next run, or cancel a pending switch.
        if current is None:
            values = {"current_task_id": task_id, "pending_task_id": None}
        elif current != task_id:
            values = {"pending_task_id": task_id}
        else:
            values = {"pending_task_id": None}
        conn.execute(
            update(SessionRow).where(SessionRow.session_id == session_id).values(**values, updated_at=now)
        )

    store.writer.call(work)
    return get_task(store, task_id)


def bind_new_session(
    store: Store, session_id: str, *, task_id: str | None, project_id: str | None, role: str | None = None
) -> None:
    """A session launched for a task starts bound to it."""
    now = utc_now()

    def work(conn: Connection) -> None:
        conn.execute(
            update(SessionRow)
            .where(SessionRow.session_id == session_id)
            .values(current_task_id=task_id, project_id=project_id, updated_at=now)
        )
        if task_id:
            conn.execute(
                sqlite_insert(TaskSession)
                .values(task_id=task_id, session_id=session_id, role=role, attached_at=now)
                .on_conflict_do_nothing()
            )

    store.writer.call(work)


def archive_session(store: Store, session_id: str, archived: bool) -> None:
    """Hide from default views; nothing native is deleted (FR-042)."""

    def work(conn: Connection) -> None:
        n = conn.execute(
            update(SessionRow)
            .where(SessionRow.session_id == session_id)
            .values(archived=int(archived), updated_at=utc_now())
        ).rowcount
        if not n:
            raise WorkspaceError("not_found", f"no session {session_id}", 404)

    store.writer.call(work)


def set_independence(store: Store, session_id: str, independence: str | None) -> None:
    store.writer.call(
        lambda c: c.execute(
            update(SessionRow).where(SessionRow.session_id == session_id).values(independence=independence)
        )
    )
