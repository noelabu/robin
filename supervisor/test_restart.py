"""Phase 0 spike: prove a session survives the service being killed (TR-006).

Uses the fake runtime, so it needs no network or credentials:

    python test_restart.py

Steps: start a detached supervisor, launch a session through a client,
SIGKILL the client mid-run, let events pile up, reconnect from the saved
cursor, then answer the pending approval, interrupt, and shut down.
Checks that the host never restarted, the event sequence has no gaps or
duplicates, and approval/interrupt round-trips work after reconnect.
"""

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PY = sys.executable


def run(data, *args, seconds=0.5):
    out = subprocess.run([PY, "client.py", "--data", str(data), "--seconds", str(seconds), *args],
                         cwd=HERE, capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return [json.loads(l) for l in out.stdout.splitlines()]


def events(data):
    return [json.loads(l) for l in (data / "client-events.jsonl").read_text().splitlines()]


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def check(cond, label):
    print(("PASS " if cond else "FAIL ") + label)
    if not cond:
        sys.exit(1)


def main():
    data = Path(tempfile.mkdtemp(prefix="robin-spike-"))
    subprocess.run([PY, "supervisor.py", "start", "--data", str(data)], cwd=HERE, check=True)
    for _ in range(50):
        if (data / "supervisor.sock").exists():
            break
        time.sleep(0.1)

    # Service A launches a session and is killed with SIGKILL mid-run.
    a = subprocess.Popen([PY, "client.py", "--data", str(data), "--seconds", "30",
                          "launch", "fake", "--prompt", "review the PRD"],
                         cwd=HERE, stdout=subprocess.PIPE, text=True)
    time.sleep(0.6)
    a.send_signal(signal.SIGKILL)
    a.wait()
    before = events(data)
    spawned = next(e for e in before if e["event"]["type"] == "host_spawned")
    session, pid = spawned["session"], spawned["event"]["pid"]
    cursor_at_kill = before[-1]["seq"]
    check(alive(pid), f"runtime host {pid} alive after service SIGKILL")

    time.sleep(1.5)  # events accumulate in the spool with no service connected

    # Service B reconnects from the persisted cursor and keeps watching.
    run(data, "watch", seconds=0.5)
    after = events(data)
    seqs = [e["seq"] for e in after]
    check(seqs == list(range(1, seqs[-1] + 1)), f"events 1..{seqs[-1]} contiguous, no duplicates")
    check(seqs[-1] > cursor_at_kill, f"replayed events emitted while disconnected ({cursor_at_kill} -> {seqs[-1]})")
    check(sum(e["event"]["type"] == "host_spawned" for e in after) == 1, "host spawned exactly once")
    check(sum(e["event"]["type"] == "turn_started" for e in after) == 1, "prompt delivered exactly once")

    req = next(e for e in after if e["event"]["type"] == "approval_request")
    run(data, "approve", session, req["event"]["request_id"], seconds=0.5)
    run(data, "approve", session, req["event"]["request_id"], seconds=0.3)  # second reply must be rejected
    run(data, "interrupt", session, seconds=0.5)
    run(data, "stop", session, seconds=0.5)
    final = events(data)
    types = [e["event"]["type"] for e in final]
    check("approval_answered" in types and "tool_decision" in types, "approval answered after reconnect")
    check("approval_rejected" in types, "duplicate approval reply rejected")
    check("interrupt_acknowledged" in types and "turn_interrupted" in types, "interrupt acknowledged")
    check(types[-1] == "host_exited", "host exited on stop")

    run(data, "shutdown")
    time.sleep(0.5)
    print(f"evidence: {data}")


if __name__ == "__main__":
    main()
