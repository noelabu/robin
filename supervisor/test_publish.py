"""Phase 0 spike: finding publication against a real runtime (TR-067).

    uv run python supervisor/test_publish.py codex tool       # Robin MCP server
    uv run python supervisor/test_publish.py codex message    # no MCP: final-message block
    uv run python supervisor/test_publish.py pi tool|message  # Robin extension tool / block
    uv run python supervisor/test_publish.py claude tool|message

The agent reviews a small PRD in a temp folder and publishes one finding.
Approvals are granted automatically (the folder is disposable). Robin then
ingests the session's events and checks the finding's schema and attribution.
Writes the finding to <runtime>/finding-<path>-<timestamp>.json.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

from findings import MESSAGE_INSTRUCTIONS, ingest
from test_live import ROOT, RUNTIMES
from test_restart import HERE, PY, check, events, run

PRD = """# Login PRD
1. Users sign in with email and password.
2. The API reads the tenant from the X-Tenant header on every request.
3. Sessions never expire.
"""

TOOL_NAME = {"claude": "finding_publish", "codex": "finding_publish", "pi": "robin_finding_publish"}


def robin_tool_present(runtime, evs):
    """Did the session have Robin's publication tool? Read from the runtime's own reports."""
    for e in evs:
        ev = e["event"]
        if runtime == "claude" and ev.get("kind") == "SystemMessage" and ev["data"].get("subtype") == "init":
            init = ev["data"]["data"]
            if any(m.get("name") == "robin" for m in init.get("mcp_servers", [])) or \
                    any("robin" in t for t in init.get("tools", [])):
                return True
        if runtime == "codex" and ev.get("method") == "mcpServer/startupStatus/updated" and ev["params"].get("name") == "robin":
            return True
        if runtime == "pi" and ev.get("type") == "pi_event" and ev["data"].get("toolName") == "robin_finding_publish":
            return True
    return False


def main(runtime, path):
    rt = RUNTIMES[runtime]
    data = Path(tempfile.mkdtemp(prefix=f"robin-pub-{runtime}-"))
    work = Path(tempfile.mkdtemp(prefix=f"robin-pub-{runtime}-cwd-"))
    (work / "PRD.md").write_text(PRD)
    env = {**os.environ, "ROBIN_PUBLISH_PATH": path}
    if runtime == "pi":
        env.update(rt["env"]())  # temp session dir
    subprocess.run([PY, "supervisor.py", "start", "--data", str(data)], cwd=HERE, check=True, env=env)
    for _ in range(50):
        if (data / "supervisor.sock").exists():
            break
        time.sleep(0.1)

    how = (f"Publish exactly one finding by calling the {TOOL_NAME[runtime]} tool, citing PRD.md line numbers as evidence."
           if path == "tool" else MESSAGE_INSTRUCTIONS + " Cite PRD.md line numbers as evidence.")
    prompt = f"Review PRD.md in the current directory as a security reviewer. {how} Then reply DONE."
    run(data, "launch", runtime, "--cwd", str(work), "--prompt", prompt, seconds=1)
    answered, deadline, session = set(), time.time() + 240, None
    while time.time() < deadline:
        run(data, "watch", seconds=1)  # one client at a time; each poll pulls new events into the log
        evs = events(data)
        session = session or next((e["session"] for e in evs if e["event"]["type"] == "host_spawned"), None)
        for e in evs:
            if e["event"]["type"] == "approval_request" and e["event"]["request_id"] not in answered:
                answered.add(e["event"]["request_id"])
                print(f"  approving {e['event']['tool']}")
                run(data, "approve", session, e["event"]["request_id"], seconds=0.2)
        if any(rt["result"](e) for e in evs):
            break
    else:
        sys.exit(f"TIMEOUT waiting for the turn to finish (evidence: {data})")
    run(data, "stop", session, seconds=2)
    run(data, "shutdown")

    evs = events(data)
    subs = [e["event"] for e in evs if e["event"]["type"] == "finding_submission"]
    check(bool(subs), f"{len(subs)} finding submission(s) seen via the {path} path")
    findings, rejected = ingest(evs, task_id="task_prd_review")
    for r in rejected:
        print(f"  rejected: {r['errors']}")
    check(len(findings) >= 1, f"{len(findings)} valid finding(s), {len(rejected)} rejected")
    f = findings[0]
    native = rt["native_id"](next(e for e in evs if rt["native_event"](e)))
    check(f["author"]["runtime"] == runtime and f["author"]["native_session_id"] == native,
          f"attributed to {runtime} session {native} from the event stream")
    check(all(c.get("evidence") or c.get("unsupported") for c in f["claims"]), "every claim has evidence")
    if path == "message":
        check(not robin_tool_present(runtime, evs), "no Robin MCP server or tool in the session (MCP-disabled path)")
    out_dir = ROOT / runtime
    out_dir.mkdir(exist_ok=True)
    out = out_dir / f"finding-{path}-{datetime.now():%Y%m%d-%H%M%S}.json"
    out.write_text(json.dumps({"finding": f, "rejected": rejected, "evidence_dir": str(data)}, indent=2, default=str))
    print(f"finding: {out}")


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] not in RUNTIMES or sys.argv[2] not in ("tool", "message"):
        sys.exit("usage: test_publish.py {claude|codex|pi} {tool|message}")
    main(sys.argv[1], sys.argv[2])
