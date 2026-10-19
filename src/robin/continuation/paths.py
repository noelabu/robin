"""Where each runtime keeps a session's transcript, for writing a new one and finding an existing one.

Folder names follow what the runtimes write on disk (checked 2026-10-17): Claude replaces every
non-alphanumeric character of the folder's real path with "-"; Pi replaces only "/" and wraps the result
in "--"; Codex files sessions by date. Roots come from HISTORY so ROBIN_HISTORY_<RUNTIME> points tests
elsewhere, and Pi honours ROBIN_PI_SESSION_DIR the way its host does.
"""

import os
import re
from datetime import UTC, datetime
from pathlib import Path

from robin.adapters.history import HISTORY

CLAUDE_FOLDER_MAX = 200  # Claude shortens longer folder names with a hash Robin can't reproduce
NAME_MAX = 255  # bytes in one file or folder name (macOS)


def pi_session_dir() -> Path | None:
    value = os.environ.get("ROBIN_PI_SESSION_DIR")
    return Path(value) if value else None


def claude_folder(cwd: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]", "-", cwd)


def pi_folder(cwd: str) -> str:
    return f"--{cwd.lstrip('/').replace('/', '-')}--"


def stamp(when: datetime) -> str:
    """2026-10-18T01:43:07.933Z, the form both runtimes write."""
    when = when.astimezone(UTC)
    return when.strftime("%Y-%m-%dT%H:%M:%S.") + f"{when.microsecond // 1000:03d}Z"


def target_path(runtime: str, native_id: str, cwd: str, when: datetime) -> Path:
    root = HISTORY[runtime].default_root()
    if runtime == "claude":
        return root / claude_folder(cwd) / f"{native_id}.jsonl"
    if runtime == "codex":
        when = when.astimezone(UTC)
        return (
            root
            / when.strftime("%Y/%m/%d")
            / f"rollout-{when.strftime('%Y-%m-%dT%H-%M-%S')}-{native_id}.jsonl"
        )
    name = f"{stamp(when).replace(':', '-').replace('.', '-')}_{native_id}.jsonl"
    flat = pi_session_dir()
    return flat / name if flat else root / pi_folder(cwd) / name


def unwritable(runtime: str, cwd: str) -> str | None:
    """Why a session for this folder can't be written where the runtime will look, or None."""
    if runtime == "claude" and len(claude_folder(cwd)) > CLAUDE_FOLDER_MAX:
        return "the folder's path is too long for Claude's session store"
    if runtime == "pi" and not pi_session_dir() and len(pi_folder(cwd).encode()) > NAME_MAX:
        return "the folder's path is too long for Pi's session store"
    return None


def locate(runtime: str, native_id: str) -> Path | None:
    """An existing transcript by native session ID: Robin keeps no path for sessions it launched."""
    root = HISTORY[runtime].default_root()
    if runtime == "claude":
        found = sorted(root.glob(f"*/{native_id}.jsonl"))
    elif runtime == "codex":
        found = sorted(root.rglob(f"rollout-*-{native_id}.jsonl"))
    else:
        flat = pi_session_dir()
        found = sorted(flat.glob(f"*_{native_id}.jsonl")) if flat else []
        found = found or sorted(root.glob(f"*/*_{native_id}.jsonl"))
    return found[-1] if found else None
