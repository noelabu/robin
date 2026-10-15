"""Live: each runtime publishes an attributed finding through Robin. Runs only with ROBIN_LIVE=1.

A task-bound session reviews a small file with a planted bug and publishes one finding with the finding tool.
One Claude run has the tool turned off and publishes with a final-message block instead (MCP stays optional,
TR-066). Approvals the runtime asks for are approved here, as the user would. Pick runtimes with
ROBIN_LIVE_RUNTIMES.
"""

import asyncio
import os
import threading
import time
import uuid

import httpx
import pytest

from robin.findings import schema
from robin.service.main import build, free_port

pytestmark = pytest.mark.skipif(os.environ.get("ROBIN_LIVE") != "1", reason="set ROBIN_LIVE=1 to run")

CODE = """def average(values):
    total = 0
    for v in values:
        total += v
    return total / (len(values) - 1)
"""
TOOL_NAMES = {"claude": "finding_publish", "codex": "finding_publish", "pi": "robin_finding_publish"}
SELECTED = [r for r in os.environ.get("ROBIN_LIVE_RUNTIMES", "claude,codex,pi").split(",") if r in TOOL_NAMES]


@pytest.fixture
def api(supervised, tmp_path):
    store = supervised
    port = free_port()
    server, auth = build(store, port)
    thread = threading.Thread(target=lambda: asyncio.run(server.serve()), daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.02)
    c = httpx.Client(
        base_url=f"http://127.0.0.1:{port}/api/v1",
        headers={"authorization": f"Bearer {auth.cli_token}"},
        timeout=30,
    )
    project = tmp_path / "stats"
    project.mkdir()
    (project / "stats.py").write_text(CODE)
    pid = c.post("/projects", json={"path": str(project)}).json()["project_id"]
    yield c, pid
    for s in c.get("/workspace").json()["sessions"]:
        c.post(
            f"/sessions/{s['session_id']}/actions",
            json={"type": "stop", "action_id": f"live-{uuid.uuid4().hex}"},
        )
    time.sleep(1)
    server.should_exit = True
    thread.join(10)


def review(c, pid, runtime, prompt, *, tool=True, timeout=300):
    task = c.post(
        "/tasks",
        json={"project_id": pid, "title": f"Review stats ({runtime})", "objective": "Find bugs in stats.py"},
    ).json()
    r = c.post(
        "/sessions/launch",
        json={
            "runtime": runtime,
            "task_id": task["task_id"],
            "prompt": prompt,
            "finding_tool": tool,
            "action_id": f"live-{uuid.uuid4().hex}",
        },
    )
    assert r.status_code == 202, r.text
    sid = r.json()["session_id"]
    deadline = time.time() + timeout
    while time.time() < deadline:
        for item in c.get(f"/attention?session_id={sid}").json()["items"]:
            if item["kind"] == "approval":  # the user approving a read or the publish call
                c.post(
                    f"/attention/{item['attention_id']}/responses",
                    json={"decision": "allow", "action_id": f"live-{uuid.uuid4().hex}"},
                )
        found = c.get(f"/findings?task_id={task['task_id']}").json()["findings"]
        rejected = c.get(f"/findings/rejections?task_id={task['task_id']}").json()["rejections"]
        if found:
            return sid, c.get(f"/findings/{found[0]['finding_id']}").json()
        assert not rejected, rejected
        time.sleep(1)
    raise AssertionError(f"{runtime} published no finding")


@pytest.mark.parametrize("runtime", SELECTED)
def test_tool_publication(api, runtime):
    c, pid = api
    prompt = (
        f"Read stats.py and find the bug. Then call the {TOOL_NAMES[runtime]} tool exactly once with one "
        "finding: a summary, one claim citing stats.py with its line range as evidence, and one "
        "recommendation. "
        "Do not edit any file."
    )
    sid, f = review(c, pid, runtime, prompt)
    session = c.get(f"/sessions/{sid}").json()
    assert f["publication_path"] == "tool"
    assert f["author"]["kind"] == "agent" and f["author"]["runtime"] == runtime
    assert f["author"]["native_session_id"] == session["native_session_id"]
    assert any(e.get("path", "").endswith("stats.py") for cl in f["claims"] for e in cl["evidence"])


@pytest.mark.skipif("claude" not in SELECTED, reason="claude not selected")
def test_message_publication_with_mcp_disabled(api):
    c, pid = api
    prompt = "Read stats.py and find the bug. Do not edit any file. " + schema.MESSAGE_INSTRUCTIONS
    sid, f = review(c, pid, "claude", prompt, tool=False)
    assert f["publication_path"] == "message" and f["author"]["runtime"] == "claude"
