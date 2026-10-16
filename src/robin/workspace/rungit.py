"""Recording Git context for runs as they start and end (FR-050, TR-075), off the request path.

The service calls `capture` after events commit. A run gets its start snapshot as soon as Robin sees it
running; a snapshot taken a while after the run began is marked late, because the run may already have
changed files. A run that started and ended before Robin could look gets its repository and end state only,
and its changed files are unknown rather than guessed.
"""

import json
import logging
from datetime import timedelta
from typing import Any

from sqlalchemy import insert, select, update

from robin import gitctx
from robin.ids import utc_now
from robin.store import Store
from robin.store.models import Run, RunGit
from robin.store.models import Session as SessionRow
from robin.workspace import folders

log = logging.getLogger(__name__)

LATE = timedelta(seconds=10)
RECENT = timedelta(minutes=1)


def capture(store: Store) -> int:
    """Take any pending start and end snapshots. Returns how many runs were updated."""
    with store.reader.connect() as conn:
        base = (
            select(Run.run_id, Run.session_id, Run.started_at, Run.ended_at, SessionRow.execution_cwd)
            .join(SessionRow, SessionRow.session_id == Run.session_id)
            .where(SessionRow.mode == "managed", SessionRow.execution_cwd.is_not(None))
        )
        new = list(
            conn.execute(
                base.outerjoin(RunGit, RunGit.run_id == Run.run_id).where(
                    RunGit.run_id.is_(None),
                    # runs from before Robin read Git, or long finished, are left alone
                    Run.ended_at.is_(None) | (Run.ended_at > utc_now() - RECENT),
                )
            ).mappings()
        )
        ending = list(
            conn.execute(
                base.add_columns(RunGit.repo_root, RunGit.branch, RunGit.base_commit, RunGit.start_snapshot)
                .join(RunGit, RunGit.run_id == Run.run_id)
                .where(Run.ended_at.is_not(None), RunGit.end_captured_at.is_(None))
            ).mappings()
        )
    for r in new:
        _start(store, r)
    for e in ending:
        _end(store, e)
    return len(new) + len(ending)


def _start(store: Store, r: Any) -> None:
    now = utc_now()
    values: dict[str, Any] = {"run_id": r["run_id"], "start_captured_at": now}
    try:
        snap = gitctx.snapshot(r["execution_cwd"])
    except gitctx.GitFailed as exc:  # a repository whose state couldn't be read: unknown, not clean
        snap = None
        values |= {"repo_root": exc.root, "start_late": 1}
    if snap is not None:
        values |= {"repo_root": snap.repo_root, "branch": snap.branch}
        if r["ended_at"] is None:
            values |= {
                "base_commit": snap.head,
                "start_dirty": len(snap.dirty),
                "start_snapshot": gitctx.to_json(snap),
                "start_late": int(now - r["started_at"] > LATE),
            }
        else:  # it ended before Robin looked: no start state to compare against
            values |= {
                "start_late": 1,
                "end_commit": snap.head,
                "end_dirty": len(snap.dirty),
                "end_captured_at": now,
            }
    elif r["ended_at"] is not None:
        values["end_captured_at"] = now
    store.writer.call(lambda c: c.execute(insert(RunGit).values(**values)))


def _end(store: Store, r: Any) -> None:
    now = utc_now()
    values: dict[str, Any] = {"end_captured_at": now}
    if r["repo_root"] is not None and r["start_snapshot"] is not None:
        try:
            end = gitctx.snapshot(r["execution_cwd"])
            if end is None or end.repo_root != r["repo_root"]:
                raise gitctx.GitFailed("the repository is gone or moved")
            files, truncated = gitctx.changes(gitctx.from_row(r), end)
        except gitctx.GitFailed:
            log.warning("couldn't read the run's Git state; its changed files stay unknown")
        else:
            values |= {
                "end_commit": end.head,
                "end_dirty": len(end.dirty),
                "changed_files": json.dumps(files),
                "truncated": int(truncated),
                "shared_folder": int(_shared(store, r)),
            }
    store.writer.call(
        lambda c: c.execute(update(RunGit).where(RunGit.run_id == r["run_id"]).values(**values))
    )


def _shared(store: Store, r: Any) -> bool:
    """Did another Robin session run in an overlapping folder while this run was going?"""
    with store.reader.connect() as conn:
        others = conn.execute(
            select(SessionRow.execution_cwd)
            .join(Run, Run.session_id == SessionRow.session_id)
            .where(
                SessionRow.session_id != r["session_id"],
                SessionRow.mode == "managed",
                SessionRow.execution_cwd.is_not(None),
                Run.started_at <= r["ended_at"],
                (Run.ended_at.is_(None)) | (Run.ended_at >= r["started_at"]),
            )
        ).scalars()
        return any(cwd and folders.overlaps(cwd, r["execution_cwd"]) for cwd in others)


def for_session(store: Store, session_id: str, limit: int = 20) -> list[dict[str, Any]]:
    """The session's latest runs with their Git context, newest first."""
    with store.reader.connect() as conn:
        rows = conn.execute(
            select(
                Run.run_id,
                Run.started_at,
                Run.ended_at,
                Run.outcome,
                Run.task_id,
                *(c for c in RunGit.__table__.c if c.name not in ("run_id",)),
            )
            .outerjoin(RunGit, RunGit.run_id == Run.run_id)
            .where(Run.session_id == session_id)
            .order_by(Run.started_at.desc())
            .limit(limit)
        ).mappings()
        return [_view(r) for r in rows]


def _view(r: Any) -> dict[str, Any]:
    git = None
    if r["repo_root"] is not None:
        captured = r["changed_files"] is not None
        git = {
            "repo_root": r["repo_root"],
            "branch": r["branch"],
            "base_commit": r["base_commit"],
            "dirty_at_start": r["start_dirty"],
            "dirty_at_end": r["end_dirty"],
            "end_commit": r["end_commit"],
            "changed_files": json.loads(r["changed_files"]) if captured else None,
            "truncated": bool(r["truncated"]),
            # labels: what this can't vouch for
            "start_late": bool(r["start_late"]),
            "shared_folder": bool(r["shared_folder"]),
        }
    return {
        "run_id": r["run_id"],
        "started_at": r["started_at"].isoformat(),
        "ended_at": r["ended_at"].isoformat() if r["ended_at"] else None,
        "outcome": r["outcome"],
        "task_id": r["task_id"],
        "git": git,
    }
