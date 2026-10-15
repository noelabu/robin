"""Phase 0 spike: the restart test against a real Claude Agent SDK session.

Needs ANTHROPIC_API_KEY in the environment or in spikes/.env. Costs a few
cents. Run from spikes/:

    uv run python supervisor/test_claude.py

Checks native ID capture, event streaming, an approval round-trip through
can_use_tool after the service is SIGKILLed and reconnects, a follow-up,
interruption, and whether user settings or hooks pre-empt Robin's approval.
Writes a summary to spikes/claude/evidence-<timestamp>.json.
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

SPIKES = HERE.parent


def load_key():
    if os.environ.get("ANTHROPIC_API_KEY"):
        return os.environ["ANTHROPIC_API_KEY"]
    env = SPIKES / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.startswith("ANTHROPIC_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"')
    sys.exit("Set ANTHROPIC_API_KEY or put ANTHROPIC_API_KEY=... in spikes/.env")


def wait_for(data, pred, timeout, label):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if (data / "client-events.jsonl").exists():
            hit = next((e for e in events(data) if pred(e)), None)
            if hit:
                return hit
        time.sleep(0.5)
    sys.exit(f"TIMEOUT waiting for {label}")


def sdk(kind):
    return lambda e: e["event"].get("type") == "sdk_message" and e["event"]["kind"] == kind


def watch_until(data, pred, timeout, label):
    """Keep a client connected (so events flow to the log) until pred matches."""
    proc = subprocess.Popen([PY, "client.py", "--data", str(data), "--seconds", str(timeout), "watch"],
                            cwd=HERE, stdout=subprocess.DEVNULL)
    try:
        return wait_for(data, pred, timeout, label)
    finally:
        proc.terminate()
        proc.wait()


def main():
    key = load_key()
    data = Path(tempfile.mkdtemp(prefix="robin-claude-"))
    work = Path(tempfile.mkdtemp(prefix="robin-claude-cwd-"))
    env = {**os.environ, "ANTHROPIC_API_KEY": key}
    subprocess.run([PY, "supervisor.py", "start", "--data", str(data)], cwd=HERE, check=True, env=env)
    for _ in range(50):
        if (data / "supervisor.sock").exists():
            break
        time.sleep(0.1)

    prompt = "Create a file named robin_probe.txt containing the single word hello. Then reply DONE."
    a = subprocess.Popen([PY, "client.py", "--data", str(data), "--seconds", "120",
                          "launch", "claude", "--cwd", str(work), "--prompt", prompt],
                         cwd=HERE, stdout=subprocess.DEVNULL)
    init = wait_for(data, lambda e: sdk("SystemMessage")(e) and e["event"]["data"].get("subtype") == "init",
                    60, "SDK init message")
    native_id = init["event"]["data"].get("data", {}).get("session_id")
    check(bool(native_id), f"native session id captured: {native_id}")

    first = wait_for(data, lambda e: e["event"].get("type") == "approval_request"
                     or sdk("ResultMessage")(e), 90, "approval request or result")
    if first["event"].get("type") != "approval_request":
        print("FAIL approval never reached Robin: settings, a hook or permission mode approved the Write")
        print(f"     file created: {(work / 'robin_probe.txt').exists()}  evidence: {data}")
        sys.exit(1)
    check(True, f"approval request reached Robin for tool {first['event']['tool']}")

    a.send_signal(signal.SIGKILL)  # the service dies while Claude waits on the approval
    a.wait()
    spawned = next(e for e in events(data) if e["event"]["type"] == "host_spawned")
    session, pid = spawned["session"], spawned["event"]["pid"]
    time.sleep(3)
    check(alive(pid), "runtime host alive after service SIGKILL")

    run(data, "approve", session, first["event"]["request_id"], seconds=0.5)
    result1 = watch_until(data, sdk("ResultMessage"), 120, "first result")
    check((work / "robin_probe.txt").exists(), "approved Write ran after reconnect")
    check(sum(e["event"]["type"] == "host_spawned" for e in events(data)) == 1, "host spawned exactly once")

    run(data, "send", session, "Write a 1500-word essay about ocean tides.", seconds=0.5)
    watch_until(data, sdk("AssistantMessage"), 60, "follow-up streaming")
    run(data, "interrupt", session, seconds=0.5)
    results = lambda e: sdk("ResultMessage")(e) and e["seq"] > result1["seq"]
    result2 = watch_until(data, results, 60, "interrupted result")
    r2 = result2["event"]["data"]
    check(True, f"interrupt ended the turn: subtype={r2.get('subtype')} "
                f"terminal_reason={r2.get('terminal_reason')} stop_reason={r2.get('stop_reason')}")

    run(data, "stop", session, seconds=2)
    run(data, "shutdown")
    all_events = events(data)
    seqs = [e["seq"] for e in all_events]
    check(seqs == list(range(1, seqs[-1] + 1)), "event sequence contiguous across the restart")

    kinds = {}
    for e in all_events:
        k = e["event"].get("kind") or e["event"]["type"]
        kinds[k] = kinds.get(k, 0) + 1
    summary = {
        "at": datetime.now(timezone.utc).isoformat(),
        "sdk_version": subprocess.run([PY, "-c", "import importlib.metadata as m;print(m.version('claude-agent-sdk'))"],
                                      capture_output=True, text=True).stdout.strip(),
        "native_session_id": native_id,
        "approval_tool": first["event"]["tool"],
        "interrupt_result": {k: r2.get(k) for k in ("subtype", "terminal_reason", "stop_reason", "is_error")},
        "total_cost_usd": r2.get("total_cost_usd"),
        "event_counts": kinds,
        "evidence_dir": str(data),
    }
    out = SPIKES / "claude" / f"evidence-{datetime.now():%Y%m%d-%H%M%S}.json"
    out.write_text(json.dumps(summary, indent=2))
    print(f"summary: {out}")


if __name__ == "__main__":
    main()
