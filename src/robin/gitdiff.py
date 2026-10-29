"""Read-only diffs: one file a run changed, from the blobs kept at its start and end (gitctx), and a session
folder's working tree against HEAD.

Only a path the caller already has on record is read: the run's changed files, or what `git status` lists
right now. Secrets are redacted on each side of a diff before it's cut, so a key split across lines or
across the cut is still caught. Git diff drivers configured in the repository are never run.
"""

import os
import re
from bisect import bisect_right
from pathlib import Path
from typing import Any

from robin import gitctx
from robin.gitctx import GitFailed
from robin.redact.scanner import RuleSet

FILE_MAX = 5_000_000  # bytes: a larger file isn't diffed
MAX_LINES = 5000  # diff lines shown
MAX_CHARS = 1_000_000  # characters of diff lines shown
SLACK_LINES, SLACK_CHARS = 50, 4096  # read past the cut before redacting
SNIFF = 8000  # bytes checked for a NUL, as Git does, to call a file binary
COUNT_BUDGET = 20_000_000  # bytes of untracked files read to count their lines, per listing
DIFF = ("diff", "--no-color", "--no-ext-diff", "--no-textconv", "--no-renames", "-U3")

HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@ ?(.*)$")


class NotListed(Exception):
    """The path isn't among the files on record: nothing is read."""


def _out(cwd: str, *args: str) -> bytes:
    out = gitctx.run(cwd, *args)
    if out is None:
        raise GitFailed(f"git {args[0]} failed in {cwd}")
    return out


def outcome(state: str) -> dict[str, Any]:
    return {"status": state, "added": 0, "removed": 0, "truncated": False, "hunks": []}


def _parse(raw: bytes) -> dict[str, Any]:
    hunks: list[dict[str, Any]] = []
    for line in raw.decode(errors="replace").split("\n"):
        if m := HUNK.match(line):
            hunks.append(
                {
                    "old_start": int(m[1]),
                    "old_lines": int(m[2] if m[2] is not None else 1),
                    "new_start": int(m[3]),
                    "new_lines": int(m[4] if m[4] is not None else 1),
                    "header": m[5],
                    "lines": [],
                }
            )
        elif not hunks:
            if line.startswith("Binary files "):
                return outcome("binary")
        elif line[:1] in (" ", "+", "-", "\\"):
            hunks[-1]["lines"].append(line)
    return {**outcome("ok"), "hunks": hunks}


def _whole(data: bytes, sign: str) -> dict[str, Any]:
    """A file that exists on one side only, as one hunk of added or removed lines."""
    if b"\0" in data[:SNIFF]:
        return outcome("binary")
    if not data:
        return outcome("ok")
    text = data.decode(errors="replace")
    lines = [sign + line for line in text.removesuffix("\n").split("\n")]
    if not text.endswith("\n"):
        lines.append("\\ No newline at end of file")
    n = len(text.removesuffix("\n").split("\n"))
    old, new = (0, n) if sign == "+" else (n, 0)
    hunk = {"old_start": int(old > 0), "old_lines": old, "new_start": int(new > 0), "new_lines": new}
    return {**outcome("ok"), "hunks": [{**hunk, "header": "", "lines": lines}]}


def _cap(hunks: list[dict[str, Any]], lines: int, chars: int) -> tuple[list[dict[str, Any]], bool]:
    out: list[dict[str, Any]] = []
    for h in hunks:
        kept = []
        for line in h["lines"]:
            if lines <= 0 or chars < len(line):
                return [*out, {**h, "lines": kept}] if kept else out, True
            kept.append(line)
            lines, chars = lines - 1, chars - len(line)
        out.append({**h, "lines": kept})
    return out, False


def _redact(rules: RuleSet, hunks: list[dict[str, Any]]) -> None:
    """Mask secrets in place, scanning the old and the new side each as one text."""
    rows = [(h, i) for h in hunks for i in range(len(h["lines"]))]
    masks: dict[int, list[tuple[int, int, str | None]]] = {}
    for side in ("-", "+"):
        idx = [k for k, (h, i) in enumerate(rows) if h["lines"][i][:1] in (" ", side)]
        texts = [rows[k][0]["lines"][rows[k][1]][1:] for k in idx]
        starts, pos = [], 0
        for t in texts:
            starts.append(pos)
            pos += len(t) + 1
        for m in rules.scan("\n".join(texts)):
            j, rule = bisect_right(starts, m.start) - 1, m.rule_id
            while j < len(idx) and starts[j] < m.end:  # a secret over several lines keeps one marker
                s, e = max(m.start - starts[j], 0), min(m.end - starts[j], len(texts[j]))
                masks.setdefault(idx[j], []).append((s, e, rule))
                rule, j = None, j + 1
    for k, spans in masks.items():
        h, i = rows[k]
        line = h["lines"][i]
        text, out, pos = line[1:], [], 0
        for s, e, rule in sorted(spans, key=lambda x: (x[0], x[2] is None)):
            if e <= pos and s < pos:  # the same secret seen from the other side of a context line
                continue
            out.append(text[pos:s] if s >= pos else "")
            out.append(f"[REDACTED:{rule}]" if rule else "")
            pos = max(pos, e)
        h["lines"][i] = line[0] + "".join(out) + text[pos:]
    for h in hunks:
        h["header"] = rules.redact(h["header"])[0]


def _finish(rules: RuleSet, diff: dict[str, Any]) -> dict[str, Any]:
    """Count, redact, then cut: a secret can't straddle the cut."""
    if diff["status"] != "ok":
        return diff
    lines = [line for h in diff["hunks"] for line in h["lines"]]
    added = sum(line.startswith("+") for line in lines)
    removed = sum(line.startswith("-") for line in lines)
    hunks, cut = _cap(diff["hunks"], MAX_LINES + SLACK_LINES, MAX_CHARS + SLACK_CHARS)
    _redact(rules, hunks)
    hunks, shown_cut = _cap(hunks, MAX_LINES, MAX_CHARS)
    return {"status": "ok", "added": added, "removed": removed, "truncated": cut or shown_cut, "hunks": hunks}


# -- a run's changed file -------------------------------------------------------


def blob_diff(rules: RuleSet, root: str, before: str | None, after: str | None) -> dict[str, Any]:
    """The diff between two kept blobs; None on a side means the file didn't exist there."""
    try:
        for oid in (before, after):
            if oid is None:
                continue
            if gitctx.run(root, "cat-file", "-e", oid) is None:  # pruned, or never kept
                return outcome("unavailable")
            if int(_out(root, "cat-file", "-s", oid)) > FILE_MAX:
                return outcome("too_large")
        if before and after:
            diff = _parse(_out(root, *DIFF, before, after))
        else:
            diff = _whole(_out(root, "cat-file", "blob", before or after or ""), "+" if after else "-")
    except (GitFailed, ValueError):
        return outcome("unknown")
    return _finish(rules, diff)


# -- a folder's working tree ----------------------------------------------------


def _repo(cwd: str) -> tuple[str, str | None, str | None]:
    root = gitctx.run(cwd, "rev-parse", "--show-toplevel")
    if root is None:
        raise NotADirectoryError(cwd)
    top = root.decode().strip()
    head = gitctx.run(top, "rev-parse", "--verify", "-q", "HEAD")
    branch = gitctx.run(top, "symbolic-ref", "-q", "--short", "HEAD")
    return top, head.decode().strip() if head else None, branch.decode().strip() if branch else None


def _base(root: str, head: str | None) -> str:
    """HEAD, or the empty tree in a repository with no commits yet."""
    return head or _out(root, "hash-object", "-t", "tree", "--stdin").decode().strip()


def _entries(root: str) -> dict[str, str]:
    """What `git status` lists, path -> added | modified | deleted | untracked."""
    status = _out(root, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--no-renames")
    out = {}
    for e in status.decode(errors="surrogateescape").split("\0"):
        if len(e) > 3:
            xy, path = e[:2], e[3:]
            out[path] = (
                "untracked"
                if xy == "??"
                else "deleted"
                if "D" in xy
                else "added"
                if xy[0] == "A"
                else "modified"
            )
    return out


def _count(path: Path, budget: int) -> tuple[int | None, bool, int]:
    """An untracked file's lines, whether it's binary, and what reading it cost."""
    try:
        st = os.lstat(path)
    except OSError:
        return None, False, 0
    if not path.is_file() or path.is_symlink() or st.st_size > min(FILE_MAX, budget):
        return None, False, 0
    data = path.read_bytes()
    if b"\0" in data[:SNIFF]:
        return None, True, len(data)
    return len(data.decode(errors="replace").removesuffix("\n").split("\n")) if data else 0, False, len(data)


def worktree(cwd: str | None) -> dict[str, Any]:
    """The files that differ from HEAD in the repository holding `cwd`, with their added and removed lines."""
    if not cwd:
        return {"state": "no_folder"}
    if not Path(cwd).is_dir():
        return {"state": "missing"}
    try:
        root, head, branch = _repo(cwd)
    except NotADirectoryError:
        return {"state": "not_repo"}
    try:
        entries = _entries(root)
        numstat = _out(
            root,
            "diff",
            _base(root, head),
            "--numstat",
            "-z",
            "--no-renames",
            "--no-ext-diff",
            "--no-textconv",
        )
    except GitFailed:
        return {"state": "unknown"}
    counts: dict[str, tuple[str, str]] = {}
    for e in numstat.decode(errors="surrogateescape").split("\0"):
        parts = e.split("\t", 2)
        if len(parts) == 3:
            counts[parts[2]] = (parts[0], parts[1])
    paths = sorted(entries)
    files, budget = [], COUNT_BUDGET
    for p in paths[: gitctx.MAX_FILES]:
        f: dict[str, Any] = {"path": p, "change": entries[p], "added": None, "removed": None}
        if entries[p] == "untracked":
            lines, binary, cost = _count(Path(root) / p, budget)
            budget -= cost
            f |= {"added": lines, "removed": 0 if lines is not None else None}
        elif p in counts:
            a, r = counts[p]
            binary = a == "-"
            if not binary:
                f |= {"added": int(a), "removed": int(r)}
        else:
            binary = False
        if binary:
            f["binary"] = True
        files.append(f)
    return {
        "state": "ok",
        "repo_root": root,
        "branch": branch,
        "head": head,
        "truncated": len(paths) > gitctx.MAX_FILES,
        "files": files,
    }


def worktree_diff(rules: RuleSet, cwd: str | None, path: str) -> dict[str, Any]:
    """One listed file's diff against HEAD. Raises NotListed when `git status` doesn't list it now."""
    if not cwd or not Path(cwd).is_dir():
        raise NotListed(path)
    try:
        root, head, _ = _repo(cwd)
    except NotADirectoryError:
        raise NotListed(path) from None
    try:
        entries = _entries(root)
    except GitFailed:
        return {**outcome("unknown"), "change": None}
    if path not in entries:
        raise NotListed(path)
    change, full = entries[path], Path(root) / path
    try:
        if change == "untracked":
            if full.is_symlink():
                diff = _whole(os.fsencode(os.readlink(full)), "+")
            elif not full.is_file() or not full.resolve().is_relative_to(Path(root).resolve()):
                return {**outcome("unavailable"), "change": change}
            elif full.stat().st_size > FILE_MAX:
                return {**outcome("too_large"), "change": change}
            else:
                diff = _whole(full.read_bytes(), "+")
        elif full.is_file() and not full.is_symlink() and full.stat().st_size > FILE_MAX:
            return {**outcome("too_large"), "change": change}
        else:
            diff = _parse(_out(root, *DIFF, _base(root, head), "--", path))
    except (GitFailed, OSError):
        return {**outcome("unknown"), "change": change}
    return {**_finish(rules, diff), "change": change}
