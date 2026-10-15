"""Working folders: the branch a session writes on, and sessions that share a folder (FR-031, TR-070).

With worktrees deferred, the shared-folder warning is the protection against concurrent edits. Branches are
read from the repository's HEAD file, not by running git, so they are cheap enough for every session view.
"""

from pathlib import Path
from typing import Any

ENDED = ("stopped", "failed")


def branch_of(cwd: str | None) -> str | None:
    """The checked-out branch for a folder, or `detached@<sha>`; None outside a repository."""
    if not cwd:
        return None
    try:
        here = Path(cwd).resolve()
        for folder in (here, *here.parents):
            dot = folder / ".git"
            if dot.is_dir():
                head = dot / "HEAD"
            elif dot.is_file():  # a worktree or submodule: `gitdir: <path>`
                text = dot.read_text(errors="replace").strip()
                if not text.startswith("gitdir:"):
                    return None
                gitdir = Path(text[len("gitdir:") :].strip())
                head = (gitdir if gitdir.is_absolute() else folder / gitdir) / "HEAD"
            else:
                continue
            ref = head.read_text(errors="replace").strip()
            if ref.startswith("ref: refs/heads/"):
                return ref[len("ref: refs/heads/") :]
            return f"detached@{ref[:12]}" if ref else None
    except OSError:
        return None
    return None


def is_writer(s: Any) -> bool:
    """Sessions that may be editing their folder: live managed sessions (including handed-off ones) and
    observed sessions seen running. Every session can write until review isolation lands (Phase 3)."""
    if s["archived"] or s["runtime_state"] in ENDED or not s["execution_cwd"]:
        return False
    return bool(s["mode"] == "managed" or s["runtime_state"] == "running")


def overlaps(a: str, b: str) -> bool:
    pa, pb = Path(a), Path(b)
    return pa == pb or pa.is_relative_to(pb) or pb.is_relative_to(pa)


def shared_with(sessions: list[Any], cwd: str, exclude: str | None = None) -> list[str]:
    """Writer sessions whose folder is the same as, inside, or contains `cwd`."""
    return [
        s["session_id"]
        for s in sessions
        if s["session_id"] != exclude and is_writer(s) and overlaps(s["execution_cwd"], cwd)
    ]
