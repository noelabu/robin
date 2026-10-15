"""Findings end to end with the scripted runtime (FR-020, TR-060, TR-064, TR-067)."""

import asyncio
import json
import threading
import time
import uuid

import httpx
import pytest

from robin.findings import schema
from robin.service.main import build, free_port


@pytest.fixture
def api(supervised):
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
        timeout=10,
    )
    project = store.data_dir.parent / "review-project"
    project.mkdir()
    pid = c.post("/projects", json={"path": str(project)}).json()["project_id"]
    task = c.post("/tasks", json={"project_id": pid, "title": "Review auth", "objective": "Find bugs"}).json()
    yield store, c, task["task_id"], project
    for s in c.get("/workspace").json()["sessions"]:
        c.post(f"/sessions/{s['session_id']}/actions", json={"type": "stop", "action_id": aid()})
    server.should_exit = True
    thread.join(10)


def aid():
    return f"t-{uuid.uuid4().hex}"


def wait(fn, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("timed out")


def launch(c, task_id=None, cwd=None):
    body = {"runtime": "fake", "action_id": aid(), "task_id": task_id, "cwd": cwd}
    sid = c.post("/sessions/launch", json={k: v for k, v in body.items() if v}).json()["session_id"]
    wait(lambda: c.get(f"/sessions/{sid}").json()["native_session_id"])
    return sid


def say(c, sid, text):
    r = c.post(f"/sessions/{sid}/actions", json={"type": "message", "text": text, "action_id": aid()})
    assert r.status_code == 202, r.text


def block(payload):
    return "Here is my review.\n```robin-finding\n" + json.dumps(payload) + "\n```\n"


GOOD = {
    "submission_id": "rev-1",
    "summary": "Token refresh races with logout",
    "claims": [
        {"text": "refresh() reads a revoked token", "evidence": [{"path": "src/auth.py", "range": "40-52"}]},
        {"text": "Probably also affects SSO", "unsupported": True},
    ],
    "recommendations": ["Lock the token store"],
    "questions": ["Is SSO in scope?"],
    "author": {"kind": "user"},  # self-reported identity is ignored
}


def test_message_block_finding_is_attributed_from_the_session(api):
    store, c, task_id, project = api
    sid = launch(c, task_id)
    say(c, sid, block(GOOD))
    (f,) = wait(lambda: c.get(f"/findings?task_id={task_id}").json()["findings"])
    full = c.get(f"/findings/{f['finding_id']}").json()
    session = c.get(f"/sessions/{sid}").json()
    assert full["author"] == {
        "kind": "agent",
        "runtime": "fake",
        "native_session_id": session["native_session_id"],
        "run_id": full["author"]["run_id"],
    }
    assert full["author"]["run_id"] is not None
    assert full["task_id"] == task_id and full["version"] == 1 and full["publication_path"] == "message"
    assert full["ignored_fields"] == ["author"] and full["questions"] == ["Is SSO in scope?"]
    assert full["claims"][1] == {"text": "Probably also affects SSO", "evidence": [], "unsupported": True}
    assert f["unsupported_claims"] == 1 and full["content_hash"].startswith("sha256:")
    # unshared findings live only in Robin's store, never in the project folder
    assert not any(GOOD["summary"] in p.read_text(errors="ignore") for p in project.rglob("*") if p.is_file())

    say(c, sid, block(GOOD))  # the same submission again
    time.sleep(0.5)
    assert len(c.get(f"/findings?task_id={task_id}").json()["findings"]) == 1


def test_tool_path_and_invalid_submissions(api):
    store, c, task_id, _ = api
    sid = launch(c, task_id)
    say(c, sid, "tool-finding:" + json.dumps({**GOOD, "submission_id": "tool-1"}))
    wait(lambda: c.get(f"/findings?task_id={task_id}").json()["findings"])
    assert c.get(f"/findings?task_id={task_id}").json()["findings"][0]["publication_path"] == "tool"

    say(c, sid, block({"summary": "No evidence", "claims": [{"text": "trust me"}]}))
    (r,) = wait(lambda: c.get(f"/findings/rejections?task_id={task_id}").json()["rejections"])
    assert r["errors"] == ['claims[0] needs evidence or "unsupported": true']
    item = wait(lambda: [i for i in c.get("/attention").json()["items"] if i["kind"] == "finding_review"])[0]
    assert item["session_id"] == sid and item["detail"]["errors"] == r["errors"]

    say(c, sid, "```robin-finding\n{not json\n```")
    wait(lambda: len(c.get(f"/findings/rejections?task_id={task_id}").json()["rejections"]) == 2)


def test_a_session_without_a_task_cannot_publish(api):
    store, c, _, project = api
    sid = launch(c, cwd=str(project))
    say(c, sid, block(GOOD))
    (r,) = wait(lambda: c.get("/findings/rejections").json()["rejections"])
    assert "isn't working on a task" in r["errors"][0] and r["session_id"] == sid


def test_user_findings_versions_and_lineage(api):
    store, c, task_id, _ = api
    sid = launch(c, task_id)
    say(c, sid, block(GOOD))
    (agent,) = wait(lambda: c.get(f"/findings?task_id={task_id}").json()["findings"])

    mine = {k: GOOD[k] for k in ("summary", "claims")} | {"parents": [agent["finding_id"]]}
    r = c.post(
        "/findings", json={**mine, "task_id": task_id, "author": {"kind": "agent", "runtime": "codex"}}
    )
    assert r.status_code == 201, r.text
    user = r.json()
    assert user["author"] == {"kind": "user"} and user["parents"] == [
        {"artifact_id": agent["finding_id"], "version": 1}
    ]
    assert (
        c.get(f"/findings/{agent['finding_id']}").json()["derived"][0]["child_artifact_id"]
        == user["finding_id"]
    )

    v2 = c.post(
        f"/findings/{user['finding_id']}/versions", json={**mine, "summary": "Revised", "expected_version": 1}
    )
    assert v2.status_code == 201 and v2.json()["version"] == 2 and v2.json()["versions"] == [1, 2]
    assert (
        c.get(f"/findings/{user['finding_id']}?version=1").json()["summary"] == GOOD["summary"]
    )  # immutable
    stale = c.post(f"/findings/{user['finding_id']}/versions", json={**mine, "expected_version": 1})
    assert stale.status_code == 409
    agent_edit = c.post(f"/findings/{agent['finding_id']}/versions", json={**mine, "expected_version": 1})
    assert agent_edit.status_code == 409 and agent_edit.json()["error"]["code"] == "not_editable"
    bad = c.post("/findings", json={"task_id": task_id, "summary": "x", "claims": [{"text": "y"}]})
    assert bad.status_code == 422


def test_schema_rules():
    assert schema.validate(GOOD) == []
    assert schema.validate([]) == ["submission must be a JSON object"]
    assert "summary must be a non-empty string" in schema.validate({"claims": []})
    assert schema.validate({"summary": "s", "claims": [{"text": "t", "evidence": [{"range": "1"}]}]}) == [
        "claims[0].evidence[0] needs a path or artifact_id"
    ]
    assert schema.parse_message_blocks("no blocks") == []
    assert schema.parse_message_blocks(block(GOOD) + block({"a": 1})) == [GOOD, {"a": 1}]


def test_duplicate_parents_are_rejected_and_projection_keeps_going(api):
    store, c, task_id, _ = api
    sid = launch(c, task_id)
    say(c, sid, block(GOOD))
    (agent,) = wait(lambda: c.get(f"/findings?task_id={task_id}").json()["findings"])
    fid = agent["finding_id"]
    say(c, sid, block({**GOOD, "submission_id": "dup", "parents": [fid, fid]}))
    (r,) = wait(lambda: c.get(f"/findings/rejections?task_id={task_id}").json()["rejections"])
    assert r["errors"] == ["parents lists the same artifact more than once"]
    say(c, sid, block({**GOOD, "submission_id": "after"}))
    wait(lambda: len(c.get(f"/findings?task_id={task_id}").json()["findings"]) == 2)
    dup = c.post("/findings", json={**GOOD, "task_id": task_id, "submission_id": "u", "parents": [fid, fid]})
    assert dup.status_code == 422


def test_a_failure_recording_a_finding_becomes_a_rejection_not_a_stall(api, monkeypatch):
    from robin.findings import store as findings_store

    store, c, task_id, _ = api
    real = findings_store._create

    def broken(*a, **kw):
        if kw.get("submission_id") == "explodes":
            raise RuntimeError("disk on fire")
        return real(*a, **kw)

    monkeypatch.setattr(findings_store, "_create", broken)
    sid = launch(c, task_id)
    say(c, sid, block({**GOOD, "submission_id": "explodes"}))
    (r,) = wait(lambda: c.get(f"/findings/rejections?task_id={task_id}").json()["rejections"])
    assert r["errors"] == ["Robin couldn't record this finding (RuntimeError)"]
    say(c, sid, block({**GOOD, "submission_id": "fine"}))
    (f,) = wait(lambda: c.get(f"/findings?task_id={task_id}").json()["findings"])
    assert c.get(f"/findings/{f['finding_id']}").json()["submission_id"] == "fine"


def test_a_corrected_retry_replaces_its_rejection(api):
    store, c, task_id, _ = api
    sid = launch(c, task_id)
    bad = {"submission_id": "s1", "summary": "No evidence yet", "claims": [{"text": "trust me"}]}
    say(c, sid, block(bad))
    wait(lambda: c.get(f"/findings/rejections?task_id={task_id}").json()["rejections"])
    wait(lambda: [i for i in c.get("/attention").json()["items"] if i["kind"] == "finding_review"])
    say(c, sid, block({**GOOD, "submission_id": "s1"}))
    (f,) = wait(lambda: c.get(f"/findings?task_id={task_id}").json()["findings"])
    assert c.get(f"/findings/{f['finding_id']}").json()["submission_id"] == "s1"
    assert c.get(f"/findings/rejections?task_id={task_id}").json()["rejections"] == []
    assert not [i for i in c.get("/attention").json()["items"] if i["kind"] == "finding_review"]
    resolved = c.get("/attention?state=resolved").json()["items"]
    assert [i["resolution"] for i in resolved if i["kind"] == "finding_review"] == ["corrected"]
