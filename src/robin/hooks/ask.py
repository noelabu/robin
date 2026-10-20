"""Robin's question hook: answer a terminal Claude session's AskUserQuestion from Robin.

Runs as `python -m robin.hooks.ask --data <dir>` from Claude Code's PreToolUse hook, matched to
AskUserQuestion. It registers the question with Robin and waits for the reply. Answered: it allows the tool
call with the answers in its input, so the terminal doesn't ask. Declined: it denies it with a reason. In
every other case (Robin isn't running, the wait ran out, the user chose to answer in the terminal, anything
fails) it prints nothing and exits 0, and the terminal asks as usual. Standard library only, to start fast.
"""

import json
import os
import signal
import sys
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from robin.hooks.relay import MAX_INPUT, trim

REGISTER_TIMEOUT_S = 3.0
POLL_WAIT_S = 25
POLL_TIMEOUT_S = POLL_WAIT_S + 10
GRACE_S = 20  # past the deadline Robin gave, stop asking even if it hasn't said so
FAILURES_MAX = 3


class Robin:
    def __init__(self, data_dir: Path):
        self.url = json.loads((data_dir / "service.json").read_text())["url"]
        self.token = (data_dir / "cli.token").read_text().strip()

    def call(self, method: str, path: str, body: Any = None, timeout: float = REGISTER_TIMEOUT_S) -> Any:
        req = urllib.request.Request(
            f"{self.url}/api/v1{path}",
            data=json.dumps(body).encode() if body is not None else None,
            headers={"content-type": "application/json", "authorization": f"Bearer {self.token}"},
            method=method,
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - loopback only
            return json.loads(resp.read() or b"{}")


def decide(data_dir: Path, payload: dict[str, Any]) -> dict[str, Any] | None:
    """The hook's output for Claude Code, or None to let the terminal ask."""
    try:
        robin = Robin(data_dir)
        reg = robin.call(
            "POST",
            "/hooks/claude/questions",
            {"relay_id": uuid.uuid4().hex, "relayed_at": time.time(), "payload": trim(payload)},
        )
    except Exception:
        return None
    attention_id = reg.get("attention_id")
    if not attention_id:
        return None

    def release(*_: Any) -> None:  # Esc in the terminal stops the hook: hand the question back first
        try:
            robin.call("POST", f"/attention/{attention_id}/release", timeout=1.0)
        except Exception:
            pass
        os._exit(0)

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, release)

    give_up = float(reg.get("until") or 0) + GRACE_S
    failures = 0
    while time.time() < give_up:
        try:
            out = robin.call(
                "GET", f"/attention/{attention_id}/reply?wait={POLL_WAIT_S}", timeout=POLL_TIMEOUT_S
            )
            failures = 0
        except Exception:
            failures += 1
            if failures >= FAILURES_MAX:
                return None
            time.sleep(1)
            continue
        status = out.get("status")
        if status == "pending":
            continue
        if status == "answered":
            return _allow(payload.get("tool_input") or {}, out.get("answers") or {})
        if status == "declined":
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": out.get("message") or "The user declined to answer in Robin",
                }
            }
        return None
    return None


def _allow(tool_input: dict[str, Any], answers: dict[str, str]) -> dict[str, Any]:
    """The tool's own input plus the answers, keyed by the exact question text (Robin saw it trimmed)."""
    raw = [q.get("question") for q in tool_input.get("questions") or [] if isinstance(q, dict)]
    full = {trim(q): q for q in raw if isinstance(q, str)}
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "updatedInput": {**tool_input, "answers": {full.get(k, k): v for k, v in answers.items()}},
        }
    }


def main(argv: list[str] | None = None) -> None:
    try:
        args = list(sys.argv[1:] if argv is None else argv)
        data_dir = Path(args[args.index("--data") + 1])
        raw = sys.stdin.buffer.read(MAX_INPUT)
        while sys.stdin.buffer.read(65536):  # drain the rest so the harness never sees a broken pipe
            pass
        payload = json.loads(raw or b"{}")
        # Sessions Robin launched answer their questions through its runtime host.
        if (
            "ROBIN_LAUNCH_TOKEN" not in os.environ
            and isinstance(payload, dict)
            and payload.get("tool_name") == "AskUserQuestion"
            and payload.get("tool_use_id")
        ):
            out = decide(data_dir, payload)
            if out is not None:
                sys.stdout.write(json.dumps(out))
                sys.stdout.flush()
    except BaseException:
        pass
    os._exit(0)  # never fail the harness's hook


if __name__ == "__main__":
    main()
