"""Phase 0 spike: the restart test against a real runtime session.

    uv run python supervisor/test_live.py claude   # uses your `claude` login (subscription)
    uv run python supervisor/test_live.py codex    # uses the existing Codex login
    uv run python supervisor/test_live.py pi       # uses Pi's configured provider (OpenRouter here)

Checks native ID capture, event streaming, an approval round-trip after
the service is SIGKILLed and reconnects, a follow-up, interruption, and
whether user settings or hooks pre-empt Robin's approval. Writes a
summary to <runtime>/evidence-<timestamp>.json.
"""

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from test_restart import HERE, PY, alive, check, events, run

ROOT = HERE.parent


def ev(type_):
    return lambda e: e["event"].get("type") == type_


def sdk(kind, **data):
    return lambda e: (e["event"].get("type") == "sdk_message" and e["event"]["kind"] == kind
                      and all(e["event"]["data"].get(k) == v for k, v in data.items()))


def codex(method):
    return lambda e: e["event"].get("type") == "codex_notification" and e["event"]["method"] == method


# Per-runtime: how to read the native ID, a turn result, and streamed output.
RUNTIMES = {
    "claude": {
        "native_event": sdk("SystemMessage", subtype="init"),
        "native_id": lambda e: e["event"]["data"]["data"].get("session_id"),
        "result": sdk("ResultMessage"),
        "streaming": sdk("AssistantMessage"),
        "prompt": "Create a file named robin_probe.txt containing the single word hello. Then reply DONE.",
        # Uses the owner's own `claude` login through Anthropic's flow; Robin never reads the token.
        "env": lambda: {},
    },
    "codex": {
        "native_event": ev("session_started"),
        "native_id": lambda e: e["event"].get("native_session_id"),
        "result": codex("turn/completed"),
        "streaming": codex("item/agentMessage/delta"),
        "prompt": "Run the shell command `touch robin_probe.txt` in the current directory. Then reply DONE.",
        # Stricter than the user's default so the command needs approval (never more permissive).
        "env": lambda: {"ROBIN_CODEX_APPROVAL_POLICY": "untrusted"},
    },
    "pi": {
        "native_event": ev("session_started"),
        "native_id": lambda e: e["event"].get("native_session_id"),
        "result": lambda e: e["event"].get("type") == "pi_event" and e["event"]["event"] == "agent_settled",
        "streaming": lambda e: e["event"].get("type") == "pi_event" and e["event"]["event"] == "message_update",
        "prompt": "Run the shell command `touch robin_probe.txt` in the current directory. Then reply DONE.",
        # Keep test sessions out of the user's Pi session list.
        "env": lambda: {"ROBIN_PI_SESSION_DIR": tempfile.mkdtemp(prefix="robin-pi-sessions-")},
    },
}


def wait_for(data, pred, timeout, label, after=0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if (data / "client-events.jsonl").exists():
            hit = next((e for e in events(data) if e["seq"] > after and pred(e)), None)
            if hit:
                return hit
        time.sleep(0.5)
    sys.exit(f"TIMEOUT waiting for {label} (evidence: {data})")


def watch_until(data, pred, timeout, label, after=0):
    """Keep a client connected (so events reach the log) until pred matches."""
    proc = subprocess.Popen([PY, "client.py", "--data", str(data), "--seconds", str(timeout), "watch"],
                            cwd=HERE, stdout=subprocess.DEVNULL)
    try:
        return wait_for(data, pred, timeout, label, after)
    finally:
        proc.terminate()
        proc.wait()


def main(runtime):
    rt = RUNTIMES[runtime]
    data = Path(tempfile.mkdtemp(prefix=f"robin-{runtime}-"))
    work = Path(tempfile.mkdtemp(prefix=f"robin-{runtime}-cwd-"))
    env = {**os.environ, **rt["env"]()}
    subprocess.run([PY, "supervisor.py", "start", "--data", str(data)], cwd=HERE, check=True, env=env)
    for _ in range(50):
        if (data / "supervisor.sock").exists():
            break
        time.sleep(0.1)

    a = subprocess.Popen([PY, "client.py", "--data", str(data), "--seconds", "180",
                          "launch", runtime, "--cwd", str(work), "--prompt", rt["prompt"]],
                         cwd=HERE, stdout=subprocess.DEVNULL)
    native = wait_for(data, rt["native_event"], 60, "native session id")
    native_id = rt["native_id"](native)
    check(bool(native_id), f"native session id captured: {native_id}")

    first = wait_for(data, lambda e: ev("approval_request")(e) or rt["result"](e), 120,
                     "approval request or result")
    if not ev("approval_request")(first):
        print("FAIL approval never reached Robin: settings, a hook or the permission mode approved the action")
        print(f"     file created: {(work / 'robin_probe.txt').exists()}  evidence: {data}")
        sys.exit(1)
    check(True, f"approval request reached Robin: {first['event']['tool']}")

    a.send_signal(signal.SIGKILL)  # the service dies while the agent waits on the approval
    a.wait()
    spawned = next(e for e in events(data) if ev("host_spawned")(e))
    session, pid = spawned["session"], spawned["event"]["pid"]
    time.sleep(3)
    check(alive(pid), "runtime host alive after service SIGKILL")

    run(data, "approve", session, first["event"]["request_id"], seconds=0.5)
    result1 = watch_until(data, rt["result"], 120, "first result")
    check((work / "robin_probe.txt").exists(), "approved action ran after reconnect")
    check(sum(ev("host_spawned")(e) for e in events(data)) == 1, "host spawned exactly once")

    run(data, "send", session, "Write a 1500-word essay about ocean tides.", seconds=0.5)
    watch_until(data, rt["streaming"], 90, "follow-up streaming", after=result1["seq"])
    run(data, "interrupt", session, seconds=0.5)
    result2 = watch_until(data, rt["result"], 60, "interrupted result", after=result1["seq"])
    check(True, f"interrupt ended the turn: {json.dumps(result2['event'].get('data') or result2['event'].get('params'))[:300]}")

    run(data, "stop", session, seconds=3)
    run(data, "shutdown")
    all_events = events(data)
    seqs = [e["seq"] for e in all_events]
    check(seqs == list(range(1, seqs[-1] + 1)), "event sequence contiguous across the restart")

    counts = {}
    for e in all_events:
        k = e["event"].get("kind") or e["event"].get("method") or e["event"]["type"]
        counts[k] = counts.get(k, 0) + 1
    summary = {
        "at": datetime.now(timezone.utc).isoformat(),
        "runtime": runtime,
        "native_session_id": native_id,
        "approval": first["event"]["tool"],
        "interrupt_result": result2["event"],
        "event_counts": counts,
        "evidence_dir": str(data),
    }
    out_dir = ROOT / runtime
    out_dir.mkdir(exist_ok=True)
    out = out_dir / f"evidence-{datetime.now():%Y%m%d-%H%M%S}.json"
    out.write_text(json.dumps(summary, indent=2, default=str))
    print(f"summary: {out}")


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in RUNTIMES:
        sys.exit(f"usage: test_live.py {{{'|'.join(RUNTIMES)}}}")
    main(sys.argv[1])
