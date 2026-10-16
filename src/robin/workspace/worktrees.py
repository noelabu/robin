"""Optional task worktrees (FR-051, TR-070, TR-072): a session can run in its own Git worktree.

Robin creates the worktree on a new `robin/...` branch from the repository's current commit, under
~/robin-worktrees/<repo>/ (or ROBIN_WORKTREES), never inside Robin's data directory. Removing one is its own
confirmed action: Robin shows what removal would lose (uncommitted files, and commits not on the branch the
worktree came from), refuses while the session is still connected, removes uncommitted work only when told
to, and never deletes the branch or its commits.
"""

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from sqlalchemy import insert, select, update

from robin.ids import new_id, utc_now
from robin.store import Store
from robin.store.models import AuditEntry, Worktree
from robin.store.models import Session as SessionRow


class WorktreeError(Exception):
    def __init__(self, code: str, message: str, status: int = 409, detail: dict[str, Any] | None = None):
        super().__init__(message)
        self.code, self.status, self.detail = code, status, detail or {}


def root() -> Path:
    return Path(os.environ.get("ROBIN_WORKTREES") or Path.home() / "robin-worktrees").expanduser()


def _git(cwd: str | Path, *args: str) -> str:
    r = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"},
        check=False,
    )
    if r.returncode != 0:
        raise WorktreeError("git_failed", f"git {args[0]}: {r.stderr.strip() or 'failed'}", 422)
    return r.stdout


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "session"


def create(cwd: Path, label: str | None, data_dir: Path) -> dict[str, Any]:
    """Make a worktree for a launch from `cwd`. Returns where to run and what to record."""
    try:
        repo = Path(_git(cwd, "rev-parse", "--show-toplevel").strip())
    except WorktreeError as exc:
        raise WorktreeError("not_a_repository", f"{cwd} isn't inside a Git repository", 422) from exc
    try:
        base = _git(repo, "rev-parse", "--verify", "HEAD").strip()
    except WorktreeError as exc:
        raise WorktreeError(
            "no_commits", "the repository has no commits to start a worktree from", 422
        ) from exc
    from_branch = subprocess.run(
        ["git", "-C", str(repo), "symbolic-ref", "-q", "--short", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    name = f"{_slug(label or 'session')}-{new_id()[:8]}"
    path = (root() / _slug(repo.name) / name).resolve()
    data = data_dir.resolve()
    if path.is_relative_to(data) or data.is_relative_to(path) or path.is_relative_to(repo):
        raise WorktreeError("invalid_location", f"worktrees can't live at {path}", 422)
    path.parent.mkdir(parents=True, exist_ok=True)
    branch = f"robin/{name}"
    _git(repo, "worktree", "add", "-q", "-b", branch, str(path), base)
    rel = Path(cwd).resolve().relative_to(repo.resolve())
    return {
        "repo_root": str(repo),
        "path": str(path),
        "run_in": str(path / rel),
        "branch": branch,
        "from_branch": from_branch or None,
        "base_commit": base,
    }


def discard(wt: dict[str, Any]) -> None:
    """Undo a worktree whose launch failed before Robin recorded it: nothing has used it yet."""
    try:
        _git(wt["repo_root"], "worktree", "remove", "--force", wt["path"])
        _git(wt["repo_root"], "branch", "-D", wt["branch"])
    except WorktreeError:
        pass  # best effort; the launch error is what the user needs to see


def record(store: Store, wt: dict[str, Any], session_id: str, task_id: str | None) -> str:
    worktree_id = new_id()
    now = utc_now()
    fields = ("repo_root", "path", "branch", "from_branch", "base_commit")

    def work(conn: Any) -> None:
        conn.execute(
            insert(Worktree).values(
                worktree_id=worktree_id,
                session_id=session_id,
                task_id=task_id,
                created_at=now,
                **{k: wt[k] for k in fields},
            )
        )
        _audit(conn, "worktree.create", [worktree_id, session_id, wt["path"]], now)

    store.writer.call(work)
    return worktree_id


def _audit(conn: Any, action: str, ids: list[str], at: Any) -> None:
    conn.execute(
        insert(AuditEntry).values(
            audit_id=new_id(), at=at, actor="user", action=action, affected_ids=json.dumps(ids), outcome="ok"
        )
    )


def _row(store: Store, worktree_id: str) -> Any:
    with store.reader.connect() as conn:
        w = conn.execute(select(Worktree).where(Worktree.worktree_id == worktree_id)).mappings().first()
    if w is None:
        raise WorktreeError("not_found", f"no worktree {worktree_id}", 404)
    return w


def _view(w: Any) -> dict[str, Any]:
    return {
        "worktree_id": w["worktree_id"],
        "session_id": w["session_id"],
        "task_id": w["task_id"],
        "repo_root": w["repo_root"],
        "path": w["path"],
        "branch": w["branch"],
        "from_branch": w["from_branch"],
        "base_commit": w["base_commit"],
        "created_at": w["created_at"].isoformat(),
        "removed_at": w["removed_at"].isoformat() if w["removed_at"] else None,
    }


def list_worktrees(
    store: Store, session_id: str | None = None, task_id: str | None = None
) -> list[dict[str, Any]]:
    q = select(Worktree).order_by(Worktree.created_at.desc())
    if session_id:
        q = q.where(Worktree.session_id == session_id)
    if task_id:
        q = q.where(Worktree.task_id == task_id)
    with store.reader.connect() as conn:
        return [_view(w) for w in conn.execute(q).mappings()]


def status(store: Store, worktree_id: str) -> dict[str, Any]:
    """The worktree and what removing it would lose."""
    w = _row(store, worktree_id)
    view = _view(w)
    if w["removed_at"] is not None or not Path(w["path"]).is_dir():
        return {**view, "exists": False, "uncommitted": [], "unmerged_commits": None}
    # without renames, a moved file is its old path deleted and its new one added: one entry each
    porcelain = _git(w["path"], "status", "--porcelain=v1", "-z", "--untracked-files=all", "--no-renames")
    uncommitted = sorted(e[3:] for e in porcelain.split("\0") if len(e) > 3)
    unmerged = None
    if w["from_branch"]:
        try:
            unmerged = int(_git(w["repo_root"], "rev-list", "--count", f"{w['from_branch']}..{w['branch']}"))
        except WorktreeError:
            unmerged = None  # the original branch is gone: say unknown, not zero
    return {**view, "exists": True, "uncommitted": uncommitted, "unmerged_commits": unmerged}


def remove(store: Store, worktree_id: str, *, discard_changes: bool = False) -> dict[str, Any]:
    """Remove the worktree folder. The branch and its commits stay."""
    s = status(store, worktree_id)
    if s["removed_at"]:
        raise WorktreeError("already_removed", "this worktree was already removed")
    with store.reader.connect() as conn:
        state = conn.execute(
            select(SessionRow.connection_state).where(SessionRow.session_id == s["session_id"])
        ).scalar()
    if state == "connected":
        raise WorktreeError("session_running", "stop the session using this worktree first")
    if s["uncommitted"] and not discard_changes:
        raise WorktreeError(
            "uncommitted_changes",
            f"{len(s['uncommitted'])} uncommitted file(s) would be lost; commit them, or confirm discarding",
            409,
            {"uncommitted": s["uncommitted"][:50], "count": len(s["uncommitted"])},
        )
    if s["exists"]:
        _git(s["repo_root"], "worktree", "remove", *(["--force"] if discard_changes else []), s["path"])
    else:
        _git(s["repo_root"], "worktree", "prune")
    now = utc_now()

    def work(conn: Any) -> None:
        conn.execute(update(Worktree).where(Worktree.worktree_id == worktree_id).values(removed_at=now))
        action = "worktree.remove_discarding" if s["uncommitted"] else "worktree.remove"
        _audit(conn, action, [worktree_id, s["session_id"], s["path"]], now)

    store.writer.call(work)
    return {**status(store, worktree_id), "branch_kept": s["branch"]}
