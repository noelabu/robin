"""Git context for runs (FR-050, TR-075): repository, branch, base commit, working-tree state, files changed.

Everything here runs off the request path. Git is called with optional locks off, so Robin never takes the
index lock an agent working in the same repository may need. A folder that isn't a repository is valid
and simply has no Git context.

What a run changed is worked out per file: its content when the run started (the snapshot of files that
were already modified or untracked, otherwise the base commit) against its content at the end. Files that
were already dirty and stayed the same don't count. When another session wrote in the same folder during
the run, its edits can't be told apart, and the result says so.

The run's dirty files at its start and its changed files at its end are written to the repository's object
store (up to KEEP_MAX each), so its diff can be shown later; Git prunes them once nothing refers to them.
A symlink is hashed as its link text, as Git stores it, and never followed.
"""

import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MAX_FILES = 1000  # beyond this, the list is cut and marked truncated
KEEP_MAX = 1_000_000  # bytes: a larger file is hashed but not kept, so its diff isn't available
TIMEOUT_S = 20


class GitFailed(Exception):
    """A git call failed: the answer is unknown, never "nothing changed"."""

    def __init__(self, message: str, root: str | None = None):
        super().__init__(message)
        self.root = root


@dataclass
class Snapshot:
    repo_root: str
    branch: str | None
    head: str | None  # None in a repository with no commits yet
    dirty: dict[str, str | None] = field(default_factory=dict)  # path -> working blob hash, None if deleted
    truncated: bool = False


def run(cwd: str, *args: str, stdin: bytes | None = None) -> bytes | None:
    """A git call's output as bytes, or None when it failed. Paths are always literal, never patterns."""
    env = {
        **os.environ,
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_LITERAL_PATHSPECS": "1",
        "LC_ALL": "C",
    }
    try:
        r = subprocess.run(
            ["git", "-C", cwd, *args],
            input=stdin,
            capture_output=True,
            timeout=TIMEOUT_S,
            env=env,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 else None


def _git(cwd: str, *args: str, stdin: str | None = None) -> str | None:
    out = run(cwd, *args, stdin=stdin.encode() if stdin is not None else None)
    return out.decode(errors="replace") if out is not None else None


def _must(cwd: str, *args: str, stdin: str | None = None) -> str:
    out = _git(cwd, *args, stdin=stdin)
    if out is None:
        raise GitFailed(f"git {args[0]} failed in {cwd}")
    return out


def _hash_paths(root: str, paths: list[str], write: bool) -> list[str]:
    stdin = "\n".join(paths) + "\n"
    hashed = (_git(root, "hash-object", "-w", "--stdin-paths", stdin=stdin) if write else None) or _must(
        root, "hash-object", "--stdin-paths", stdin=stdin
    )  # a store Robin can't write to still gets hashes
    if len(hashed.split()) != len(paths):
        raise GitFailed("git hash-object returned the wrong number of hashes")
    return hashed.split()


def _hashes(root: str, paths: list[str], write: bool = False) -> dict[str, str | None]:
    """Working-tree blob hashes; None for a path that no longer exists. With `write`, the blobs are kept."""
    out: dict[str, str | None] = dict.fromkeys(paths)
    files: dict[bool, list[str]] = {True: [], False: []}
    for p in paths:
        full = Path(root) / p
        if full.is_symlink():
            link = os.fsencode(os.readlink(full))
            args = ("hash-object", *(["-w"] if write else []), "--stdin")
            hashed = run(root, *args, stdin=link) or run(root, "hash-object", "--stdin", stdin=link)
            if hashed is None:
                raise GitFailed("git hash-object failed on a symlink")
            out[p] = hashed.decode().strip()
        elif full.is_file():
            files[write and full.stat().st_size <= KEEP_MAX].append(p)
    for keep, group in files.items():
        if group:
            out.update(zip(group, _hash_paths(root, group, keep), strict=True))
    return out


def _at_commit(root: str, commit: str | None, paths: list[str]) -> dict[str, str | None]:
    """Blob hashes at a commit; None for a path the commit doesn't have."""
    out: dict[str, str | None] = dict.fromkeys(paths)
    if commit is None or not paths:
        return out
    listed = _must(root, "ls-tree", "-z", commit, "--", *paths)
    for entry in listed.split("\0"):
        if "\t" in entry:
            meta, path = entry.split("\t", 1)
            out[path] = meta.split()[2]
    return out


def snapshot(cwd: str | None, write: bool = False) -> Snapshot | None:
    """The repository state for a folder, or None when it isn't inside a Git repository. With `write`, the
    dirty files' blobs are kept.

    Raises GitFailed when it is a repository but its state couldn't be read."""
    if not cwd or not Path(cwd).is_dir():
        return None
    root = _git(cwd, "rev-parse", "--show-toplevel")
    if root is None:
        return None
    root = root.strip()
    head = _git(root, "rev-parse", "--verify", "-q", "HEAD")
    branch = _git(root, "symbolic-ref", "-q", "--short", "HEAD")
    try:
        status = _must(root, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--no-renames")
        paths = [e[3:] for e in status.split("\0") if len(e) > 3]
        truncated = len(paths) > MAX_FILES
        paths = sorted(paths)[:MAX_FILES]
        dirty = _hashes(root, paths, write)
    except GitFailed as exc:
        raise GitFailed(str(exc), root) from exc
    return Snapshot(
        repo_root=root,
        branch=branch.strip() if branch else None,
        head=head.strip() if head else None,
        dirty=dirty,
        truncated=truncated,
    )


def changes(start: Snapshot, end: Snapshot) -> tuple[list[dict[str, str | None]], bool]:
    """Files whose content differs between the run's start and end, with how they changed and the blobs
    before and after (None where the file didn't exist). The changed files' end blobs are kept."""
    candidates = set(start.dirty) | set(end.dirty)
    if start.head and end.head and start.head != end.head:  # commits made during the run
        diff = _must(end.repo_root, "diff", "--name-only", "-z", "--no-renames", start.head, end.head)
        candidates |= {p for p in diff.split("\0") if p}
    truncated = start.truncated or end.truncated or len(candidates) > MAX_FILES
    paths = sorted(candidates)[:MAX_FILES]
    clean_at_start = [p for p in paths if p not in start.dirty]
    before = {**_at_commit(end.repo_root, start.head, clean_at_start), **start.dirty}
    after = _hashes(end.repo_root, paths)
    changed = [p for p in paths if before.get(p) != after.get(p)]
    # what's kept is what's shown: a file written to again since it was hashed above takes the kept blob
    after |= _hashes(end.repo_root, [p for p in changed if after.get(p) is not None], write=True)
    out = []
    for p in changed:
        a, b = before.get(p), after.get(p)
        change = "added" if a is None else "deleted" if b is None else "modified"
        out.append({"path": p, "change": change, "before": a, "after": b})
    return out, truncated


def to_json(s: Snapshot) -> str:
    return json.dumps({"dirty": s.dirty, "truncated": s.truncated}, sort_keys=True)


def from_row(row: Any) -> Snapshot:
    data = json.loads(row["start_snapshot"] or "{}")
    return Snapshot(
        repo_root=row["repo_root"],
        branch=row["branch"],
        head=row["base_commit"],
        dirty=data.get("dirty", {}),
        truncated=bool(data.get("truncated")),
    )
