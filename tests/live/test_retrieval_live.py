"""Live: each runtime reads a granted finding through Robin's tools. Needs ROBIN_LIVE=1.

The user shares one finding with a task session; the agent reads it with Robin's finding_read tool and
repeats a codeword from it. Robin must record that read with the exact content returned.
"""

import os
import time
import uuid

import pytest

from robin.events.ingest import read_events, read_payload

pytestmark = pytest.mark.skipif(os.environ.get("ROBIN_LIVE") != "1", reason="set ROBIN_LIVE=1 to run")
TOOL = {"claude": "finding_read", "codex": "finding_read", "pi": "robin_finding_read"}
SELECTED = [r for r in os.environ.get("ROBIN_LIVE_RUNTIMES", "claude,codex,pi").split(",") if r in TOOL]


@pytest.mark.parametrize("runtime", SELECTED)
def test_agent_reads_a_granted_finding(api, runtime):
    store, c, task_id, _ = api
    c.timeout = 60
    codeword = f"ROBIN-SHARED-{uuid.uuid4().hex[:8]}"
    f = c.post(
        "/findings",
        json={
            "task_id": task_id,
            "summary": f"The codeword is {codeword}",
            "claims": [{"text": "planted", "unsupported": True}],
        },
    ).json()
    r = c.post(
        "/sessions/launch",
        json={"runtime": runtime, "task_id": task_id, "action_id": f"live-{uuid.uuid4().hex}"},
    )
    sid = r.json()["session_id"]
    deadline = time.time() + 60
    while not c.get(f"/sessions/{sid}").json()["native_session_id"] and time.time() < deadline:
        time.sleep(0.5)
    c.post("/grants", json={"session_id": sid, "artifacts": [{"id": f["finding_id"]}]})
    c.post(
        f"/sessions/{sid}/actions",
        json={
            "type": "message",
            "action_id": f"live-{uuid.uuid4().hex}",
            "text": f"Call the {TOOL[runtime]} tool with finding_id {f['finding_id']} and reply with the "
            "codeword it contains. Do not use any other tool.",
        },
    )
    deadline = time.time() + 240
    while time.time() < deadline:
        for item in c.get(f"/attention?session_id={sid}").json()["items"]:
            if item["kind"] == "approval":
                c.post(
                    f"/attention/{item['attention_id']}/responses",
                    json={"decision": "allow", "action_id": f"live-{uuid.uuid4().hex}"},
                )
        reads = c.get(f"/grants?session_id={sid}").json()["reads"]
        said = "\n".join(
            read_payload(store, e.global_cursor).decode(errors="replace")
            for e in read_events(store, session_id=sid, limit=100_000)
            if e.event_type == "message.completed"
        )
        if any(r["tool"] == "finding_read" for r in reads) and codeword in said:
            return
        time.sleep(1)
    raise AssertionError(f"{runtime}: reads={reads}")
