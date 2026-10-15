"""`robin hook-relay`: forward one hook event to Robin and get out of the harness's way (TR-076).

Runs as `python -m robin.hooks.relay --runtime <r> --data <dir>` from the harness's hook config. Standard
library only, so it starts fast. It prints nothing (some harnesses read a hook's stdout as context or as a
decision; Robin's hooks never decide anything) and always exits 0 within a short timeout. If Robin is
down the event is dropped and noted, and the service turns the note into a coverage gap.
"""

import json
import os
import sys
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Any

MAX_INPUT = 4 * 1024 * 1024
MAX_STRING = 2000
TIMEOUT_S = 1.5
DROPS_MAX = 1024 * 1024
RUNTIMES = ("claude", "codex", "pi")


def trim(value: Any, depth: int = 0) -> Any:
    """Keep what identifies the event, not tool outputs or file contents: the transcript holds those."""
    if depth > 6:
        return "…"
    if isinstance(value, str):
        return value if len(value) <= MAX_STRING else value[:MAX_STRING] + "…"
    if isinstance(value, dict):
        return {str(k): trim(v, depth + 1) for k, v in list(value.items())[:100]}
    if isinstance(value, list):
        return [trim(v, depth + 1) for v in value[:50]]
    return value


def relay(runtime: str, data_dir: Path, raw: bytes) -> bool:
    try:
        payload = json.loads(raw or b"{}")
    except ValueError:
        payload = {"_robin_invalid_json": True}
    if not isinstance(payload, dict):
        payload = {"_robin_value": payload}
    body = {
        "relay_id": uuid.uuid4().hex,
        "relayed_at": time.time(),
        "payload": trim(payload),
    }
    try:
        endpoint = json.loads((data_dir / "service.json").read_text())["url"]
        token = (data_dir / "cli.token").read_text().strip()
        req = urllib.request.Request(
            f"{endpoint}/api/v1/hooks/{runtime}",
            data=json.dumps(body).encode(),
            headers={"content-type": "application/json", "authorization": f"Bearer {token}"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:  # noqa: S310 - loopback only
            return 200 <= resp.status < 300
    except Exception:
        _note_drop(data_dir, runtime, payload)
        return False


def _note_drop(data_dir: Path, runtime: str, payload: dict[str, Any]) -> None:
    """Remember that an event was lost, so the session can show a coverage gap later. Bounded."""
    try:
        path = data_dir / "hooks" / "dropped.jsonl"
        if not path.parent.is_dir() or (path.exists() and path.stat().st_size > DROPS_MAX):
            return
        line = {
            "runtime": runtime,
            "session_id": str(payload.get("session_id") or ""),
            "event": str(payload.get("hook_event_name") or ""),
            "cwd": str(payload.get("cwd") or ""),
            "at": time.time(),
        }
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, (json.dumps(line) + "\n").encode())
        finally:
            os.close(fd)
    except Exception:
        pass


def main(argv: list[str] | None = None) -> None:
    try:
        args = list(sys.argv[1:] if argv is None else argv)
        runtime = args[args.index("--runtime") + 1]
        data_dir = Path(args[args.index("--data") + 1])
        raw = sys.stdin.buffer.read(MAX_INPUT)
        while sys.stdin.buffer.read(65536):  # drain the rest so the harness never sees a broken pipe
            pass
        # Sessions Robin launched run under its runtime host, which sets this. Their stream is the record.
        if runtime in RUNTIMES and "ROBIN_LAUNCH_TOKEN" not in os.environ:
            relay(runtime, data_dir, raw)
    except BaseException:
        pass
    os._exit(0)  # never block, never fail the harness's hook


if __name__ == "__main__":
    main()
