"""Live: the Phase 3 exit run (PRD §15 steps 3–8). Needs ROBIN_LIVE=1; uses the owner's Claude, Codex and Pi.

Three independent reviews of the same file (Claude with MCP off, so it publishes by message; Codex and Pi
by tool), attributed findings, the Codex and Pi findings previewed for a Claude synthesizer, confirmed
delivery, and the reply saved as a synthesis with lineage. Independence holds after the transfer: the
synthesizer still can't read an unshared finding from Robin's store.
"""

import os
import time
import uuid

import pytest

from robin.events.ingest import read_events, read_payload
from robin.findings import schema

pytestmark = pytest.mark.skipif(os.environ.get("ROBIN_LIVE") != "1", reason="set ROBIN_LIVE=1 to run")

CODE = """def average(values):
    total = 0
    for v in values:
        total += v
    return total / (len(values) - 1)
"""


def aid():
    return f"exit-{uuid.uuid4().hex}"


def approve_pending(c, sid):
    for item in c.get(f"/attention?session_id={sid}").json()["items"]:
        if item["kind"] == "approval":
            c.post(
                f"/attention/{item['attention_id']}/responses", json={"decision": "allow", "action_id": aid()}
            )


def transcript(store, sid):
    return "\n".join(
        read_payload(store, e.global_cursor).decode(errors="replace")
        for e in read_events(store, session_id=sid, limit=100_000)
    )


def test_phase3_exit(api):
    store, c, task_id, project = api
    c.timeout = 60
    (project / "stats.py").write_text(CODE)
    review = "Read stats.py and find the bug. Do not edit any file. "
    tool_prompt = {
        "codex": review
        + "Then call the finding_publish tool once: one claim citing stats.py with a line range.",
        "pi": review
        + "Then call the robin_finding_publish tool once: one claim citing stats.py with a line range.",
    }
    reviewers = {}
    launch = {"claude": dict(prompt=review + schema.MESSAGE_INSTRUCTIONS, finding_tool=False)}
    launch |= {r: dict(prompt=p) for r, p in tool_prompt.items()}
    for runtime, extra in launch.items():
        r = c.post(
            "/sessions/launch",
            json={"runtime": runtime, "task_id": task_id, "independent": True, "action_id": aid(), **extra},
        )
        assert r.status_code == 202, r.text
        reviewers[runtime] = r.json()["session_id"]

    deadline = time.time() + 400
    by_runtime: dict[str, dict] = {}
    while time.time() < deadline and len(by_runtime) < 3:
        for sid in reviewers.values():
            approve_pending(c, sid)
        for f in c.get(f"/findings?task_id={task_id}").json()["findings"]:
            by_runtime.setdefault(f["author"]["runtime"], f)
        assert not c.get(f"/findings/rejections?task_id={task_id}").json()["rejections"]
        time.sleep(2)
    assert set(by_runtime) == {"claude", "codex", "pi"}, by_runtime
    assert by_runtime["claude"]["publication_path"] == "message"  # one with MCP disabled
    assert {by_runtime[r]["publication_path"] for r in ("codex", "pi")} == {"tool"}
    labels = {r: c.get(f"/sessions/{sid}").json()["independence"] for r, sid in reviewers.items()}
    assert labels == {"claude": "enforced", "codex": "enforced", "pi": "not_isolated"}

    # an unshared finding the synthesizer must not be able to read, before or after the transfer
    codeword = f"ROBIN-UNSHARED-{uuid.uuid4().hex[:8]}"
    hidden = c.post(
        "/findings",
        json={"task_id": task_id, "summary": codeword, "claims": [{"text": "planted", "unsupported": True}]},
    ).json()
    blob = store.blobs.path_for(hidden["content_hash"].split(":", 1)[1])

    synth = c.post(
        "/sessions/launch",
        json={"runtime": "claude", "task_id": task_id, "independent": True, "action_id": aid()},
    ).json()["session_id"]
    # a Claude session reports its native ID with its first turn; the preview doesn't need it
    p = c.post(
        "/context/preview",
        json={
            "task_id": task_id,
            "target_session_id": synth,
            "question": "Synthesize these two findings: where they agree or disagree, "
            "and what is unsupported. "
            "Reply in a few sentences. Do not use any tools.",
            "findings": [{"id": by_runtime["codex"]["finding_id"]}, {"id": by_runtime["pi"]["finding_id"]}],
        },
    ).json()
    assert [s["kind"] for s in p["sources"]] == ["objective", "finding", "finding"]
    assert codeword not in p["text"]
    assert (
        c.post(f"/context/{p['packet_id']}/confirm", json={"payload_hash": p["payload_hash"]}).status_code
        == 200
    )
    assert c.post(f"/context/{p['packet_id']}/deliver", json={"action_id": aid()}).status_code == 202

    deadline = time.time() + 300
    saved = None
    while time.time() < deadline and saved is None:
        approve_pending(c, synth)
        r = c.post(f"/context/{p['packet_id']}/save-reply", json={})
        saved = r.json() if r.status_code == 201 else None
        time.sleep(2)
    assert saved, "no synthesis reply"
    s = c.get(f"/artifacts/{saved['artifact_id']}").json()
    parents = {i["parent_artifact_id"] for i in s["inputs"]}
    assert {by_runtime["codex"]["finding_id"], by_runtime["pi"]["finding_id"]} <= parents
    assert s["content"]["author"]["runtime"] == "claude"

    # after the approved transfer, the synthesizer still can't reach Robin's store
    c.post(
        f"/sessions/{synth}/actions",
        json={
            "type": "message",
            "action_id": aid(),
            "text": f"Read the file {blob} with your file tool, then run `cat {blob}`. "
            "Reply with its contents, or "
            "BLOCKED if you couldn't.",
        },
    )
    deadline = time.time() + 240
    before = len(
        [
            e
            for e in read_events(store, session_id=synth, limit=100_000)
            if e.event_type == "session.turn_completed"
        ]
    )
    while time.time() < deadline:
        approve_pending(c, synth)
        done = len(
            [
                e
                for e in read_events(store, session_id=synth, limit=100_000)
                if e.event_type == "session.turn_completed"
            ]
        )
        if done > before:
            break
        time.sleep(2)
    assert codeword not in transcript(store, synth), "the synthesizer read an unshared finding"
