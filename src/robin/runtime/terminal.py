"""Native terminal handoff (FR-037, TR-046).

Every runtime's terminal mode is handoff: Robin releases the session (its runtime host stops, the native
session stays on disk) and the user continues it with the runtime's own resume command. Commands are
built as argument arrays from validated values; a shell string is only produced, quoted, for Terminal.app.
"""

import os
import re
import shlex
import subprocess
from pathlib import Path

# The first character is alphanumeric: an id read from a transcript must never be taken as a flag
# (`claude --resume --dangerously-skip-permissions`).
NATIVE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def resume_command(runtime: str, native_id: str, skip_permissions: bool = False) -> list[str]:
    """The runtime's own resume command. A session launched with prompts skipped keeps them skipped, with the
    same setting Robin used: bypass mode for Claude, approval policy `never` (sandbox unchanged) for Codex."""
    if not NATIVE_ID.match(native_id):
        raise ValueError("native session id has unexpected characters")
    match runtime:
        case "claude":
            skip = ["--permission-mode", "bypassPermissions"] if skip_permissions else []
            return ["claude", "--resume", native_id, *skip]
        case "codex":
            return ["codex", "resume", native_id, *(["-a", "never"] if skip_permissions else [])]
        case "pi":
            return ["pi", "--session", native_id]
    raise ValueError(f"no terminal command for {runtime}")


def holders(native_id: str) -> list[int]:
    """Processes whose command line names this native session: a terminal that may still be writing it.

    A heuristic (it can't see a session reopened by other means), so reclaiming also reports what it found.
    """
    if not NATIVE_ID.match(native_id):
        return []
    out = subprocess.run(["pgrep", "-f", "--", native_id], capture_output=True, text=True)
    me = os.getpid()
    return [int(p) for p in out.stdout.split() if p.isdigit() and int(p) != me]


def open_in_terminal_app(command: list[str], cwd: str) -> None:
    """Open macOS Terminal.app in the session's folder running the resume command (explicit user action)."""
    line = f"cd {shlex.quote(cwd)} && {shlex.join(command)}"
    script = (
        f'tell application "Terminal" to do script {_applescript_string(line)}\n'
        'tell application "Terminal" to activate'
    )
    if not Path(cwd).is_dir():
        raise ValueError(f"{cwd} is not a directory")
    subprocess.run(["osascript", "-e", script], check=True, capture_output=True, timeout=15)


def _applescript_string(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
