"""Live check: does each production runtime host resume a session file Robin wrote itself?

    uv run python supervisor/test_continue_live.py claude|codex|pi|pi-session-dir

Converts a small session (a codeword, reasoning, a tool call and its result) with Robin's own writers into
the runtime's session store, launches `python -m robin.runtime` with ROBIN_RESUME_NATIVE_ID, and asks for
the codeword spelled backwards. The written session stays in the runtime's resume list.
"""

import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from robin.continuation import writers
from robin.continuation.canonical import Msg, Text, Thinking, ToolCall, ToolResult, Transcript

ROOT = Path(__file__).resolve().parent.parent
CODEWORDS = {
    "claude": "OSPREY",
    "codex": "PELICAN",
    "pi": "CORMORANT",
    "pi-session-dir": "GANNET",
}
# Spelled backwards so only a model that read the history can produce it; a replay of the history can't.
QUESTION = "Reply with the codeword I asked you to remember, spelled backwards in capitals, and nothing else."


def transcript(source, codeword):
    """A source session in canonical form: a codeword, reasoning, a tool call and its result."""
    return Transcript(
        source,
        "live-source",
        None,
        None,
        [
            Msg("user", [Text(f"Remember the codeword {codeword}. Then list the files here.")]),
            Msg(
                "assistant",
                [
                    Thinking("The user wants a codeword kept and a listing."),
                    ToolCall("call_1", "Bash", {"command": "ls"}),
                ],
            ),
            Msg("tool", [ToolResult("call_1", "notes.txt")]),
            Msg("assistant", [Text(f"Codeword {codeword} noted. The folder contains notes.txt.")]),
        ],
    )


def place(rendered):
    os.replace(writers.stage(rendered), rendered.path)
    return rendered.path


def resume(runtime, cwd, native_id, env, timeout=240):
    """Run the production host on the written session; return (answered, event log)."""
    env = {**os.environ, **env, "ROBIN_RESUME_NATIVE_ID": native_id}
    proc = subprocess.Popen(
        [sys.executable, "-m", "robin.runtime", "--runtime", runtime, "--cwd", cwd],
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    lines: queue.Queue[str] = queue.Queue()
    threading.Thread(target=lambda: [lines.put(x) for x in proc.stdout], daemon=True).start()
    proc.stdin.write(json.dumps({"cmd": "send", "text": QUESTION, "action_id": "continue-live"}) + "\n")
    proc.stdin.flush()
    log, deadline = [], time.time() + timeout
    while time.time() < deadline and proc.poll() is None:
        try:
            log.append(lines.get(timeout=1))
        except queue.Empty:
            continue
        if '"host_error"' in log[-1]:
            break
        if any(w[::-1] in log[-1] for w in CODEWORDS.values()):
            break
    try:
        proc.stdin.write(json.dumps({"cmd": "stop"}) + "\n")
        proc.stdin.flush()
        proc.wait(timeout=20)
    except (BrokenPipeError, subprocess.TimeoutExpired):
        proc.kill()
    return log, proc.stderr.read()[-2000:]


def main(variant):
    runtime = variant.split("-")[0]
    codeword = CODEWORDS[variant]
    cwd = os.path.realpath(tempfile.mkdtemp(prefix=f"robin-continue-{variant}-"))
    Path(cwd, "notes.txt").write_text("notes\n")
    env = {}
    if variant == "pi-session-dir":
        env["ROBIN_PI_SESSION_DIR"] = tempfile.mkdtemp(prefix="robin-pi-sessions-")
        os.environ["ROBIN_PI_SESSION_DIR"] = env["ROBIN_PI_SESSION_DIR"]
    source = "codex" if runtime != "codex" else "claude"
    rendered = writers.render(transcript(source, codeword), runtime, cwd)
    path = place(rendered)
    sid = rendered.native_id
    log, stderr = resume(runtime, cwd, sid, env)
    answered = any(codeword[::-1] in x for x in log)
    started = next((json.loads(x) for x in log if '"session_started"' in x), {})
    print(
        json.dumps(
            {
                "variant": variant,
                "native_session_id": sid,
                "written": str(path).replace(str(Path.home()), "~"),
                "reported_native_id": started.get("native_session_id"),
                "answered": answered,
                "events": len(log),
            }
        )
    )
    if not answered:
        print("--- last events ---", *[x[:400] for x in log[-6:]], "--- stderr ---", stderr, sep="\n")
        sys.exit(1)
    print(f"PASS {variant}: the resumed session answered {codeword[::-1]}")


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in CODEWORDS:
        sys.exit(f"usage: test_continue_live.py {{{'|'.join(CODEWORDS)}}}")
    main(sys.argv[1])
