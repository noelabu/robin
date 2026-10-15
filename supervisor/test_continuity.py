"""Phase 0 spike: steering, native resume and terminal handoff (FR-013, FR-015, FR-037).

    uv run python supervisor/test_continuity.py codex|pi|claude

1. Steer: start a long answer, then steer it mid-turn to reply with a codeword.
2. Resume: stop the session (its runtime host exits), launch a new host that
   resumes the same native session, and ask for the codeword back.
3. Handoff: with no Robin host running, continue the same session from the
   runtime's own CLI and ask for the codeword again.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from test_live import RUNTIMES, load_key
from test_restart import HERE, PY, check, events, run

CODEWORD = "PELICAN"


def final_text(runtime, evs, after):
    """Assistant text produced after `after`, per runtime event shape."""
    out = []
    for e in (x for x in evs if x["seq"] > after):
        ev = e["event"]
        if runtime == "codex" and ev.get("method") == "item/completed":
            item = ev["params"].get("item", {})
            if item.get("type") == "agentMessage":
                out.append(item.get("text", ""))
        elif runtime == "pi" and ev.get("type") == "pi_event" and ev["event"] == "message_end":
            msg = ev["data"].get("message", {})
            if msg.get("role") == "assistant":
                c = msg.get("content")
                out.append(c if isinstance(c, str) else "".join(
                    b.get("text", "") for b in c or [] if isinstance(b, dict) and b.get("type") == "text"))
        elif runtime == "claude" and ev.get("type") == "sdk_message" and ev["kind"] == "ResultMessage":
            out.append(ev["data"].get("result") or "")
    return "\n".join(out)


def poll_until(data, pred, timeout, label, after=0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        run(data, "watch", seconds=1)
        hit = next((e for e in events(data) if e["seq"] > after and pred(e)), None)
        if hit:
            return hit
    sys.exit(f"TIMEOUT waiting for {label} (evidence: {data})")


def session_of(data, after=0):
    return next(e["session"] for e in events(data) if e["seq"] > after and e["event"]["type"] == "host_spawned")


def native_cli(runtime, native_id, work, prompt, env):
    if runtime == "codex":
        # The temp folder is neither trusted nor a git repo; real projects are git repos.
        cmd = ["codex", "exec", "resume", "--skip-git-repo-check", native_id, prompt]
    elif runtime == "pi":
        cmd = ["pi", "-p", "--no-approve", "--session", native_id, "--session-dir", env["ROBIN_PI_SESSION_DIR"], prompt]
    else:
        cmd = ["claude", "-p", "--resume", native_id, prompt]
    out = subprocess.run(cmd, cwd=work, capture_output=True, text=True, timeout=240, env=env,
                         stdin=subprocess.DEVNULL)
    return out.stdout + out.stderr


def main(runtime):
    rt = RUNTIMES[runtime]
    data = Path(tempfile.mkdtemp(prefix=f"robin-cont-{runtime}-"))
    work = Path(tempfile.mkdtemp(prefix=f"robin-cont-{runtime}-cwd-"))
    env = {**os.environ}
    if runtime == "claude":
        env["ANTHROPIC_API_KEY"] = load_key()
    if runtime == "pi":
        env.update(rt["env"]())
    subprocess.run([PY, "supervisor.py", "start", "--data", str(data)], cwd=HERE, check=True, env=env)
    for _ in range(50):
        if (data / "supervisor.sock").exists():
            break
        time.sleep(0.1)

    # 1. Steer mid-turn.
    run(data, "launch", runtime, "--cwd", str(work), "--prompt",
        "Write a 2000-word essay about ocean tides. Do not use any tools.", seconds=1)
    session = session_of(data)
    native = rt["native_id"](poll_until(data, rt["native_event"], 60, "native id"))
    poll_until(data, rt["streaming"], 90, "essay streaming")
    mark = events(data)[-1]["seq"]
    run(data, "send", session, f"Stop the essay now. Reply with exactly the word {CODEWORD} and nothing else.",
        seconds=1)
    sub = poll_until(data, lambda e: e["event"]["type"] == "input_submitted", 30, "steer submitted", after=mark)
    check(sub["event"].get("mode") == "steer" and not sub["event"].get("error"),
          f"input delivered as steer during the turn: {sub['event']}")
    done = poll_until(data, rt["result"], 180, "steered turn result", after=mark)
    text = final_text(runtime, events(data), mark)
    check(CODEWORD in text, f"agent followed the steer ({len(text)} chars of output, codeword present)")

    # 2. Resume after the runtime host exits.
    run(data, "stop", session, seconds=3)
    poll_until(data, lambda e: e["session"] == session and e["event"]["type"] == "host_exited", 30, "host exit")
    mark = events(data)[-1]["seq"]
    run(data, "launch", runtime, "--cwd", str(work), "--env", f"ROBIN_RESUME_NATIVE_ID={native}",
        "--prompt", "What single word did I ask you to reply with earlier? Reply with just that word.", seconds=1)
    session2 = session_of(data, after=mark)
    resumed = rt["native_id"](poll_until(data, rt["native_event"], 60, "resumed native id", after=mark))
    check(resumed == native, f"resumed the same native session {native}")
    poll_until(data, rt["result"], 120, "resumed turn result", after=mark)
    check(CODEWORD in final_text(runtime, events(data), mark), "resumed session remembered the codeword")
    run(data, "stop", session2, seconds=3)
    run(data, "shutdown")

    # 3. Handoff: the native CLI continues the same session with no Robin host attached.
    out = native_cli(runtime, native, work, "Repeat the single word I asked for earlier. Reply with just that word.", env)
    check(CODEWORD in out, f"native CLI continued the same session (handoff works)")
    print(json.dumps({"runtime": runtime, "native_session_id": native, "evidence_dir": str(data)}))


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in RUNTIMES:
        sys.exit("usage: test_continuity.py {claude|codex|pi}")
    main(sys.argv[1])
