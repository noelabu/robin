"""Agent retrieval: credentials, grants, scoped reads and their records (TR-066, TR-068)."""

import hashlib
import json

import httpx

from robin import retrieval
from robin.events.ingest import read_events
from robin.findings.store import canonical
from test_context import observed_session_with, publish_user
from test_findings import GOOD, aid, block, launch, say, wait
from test_independence import started_with


def agent(c, store, sid):
    """An HTTP client holding a session's agent credential, as its tools would."""
    token = retrieval.mint(store, sid)
    return httpx.Client(base_url=c.base_url, headers={"authorization": f"Bearer {token}"}, timeout=10)


def test_task_sessions_get_tools_and_the_credential_is_scoped(api):
    store, c, task_id, project = api
    sid = launch(c, task_id)
    assert wait(lambda: started_with(store, sid))["agent_tools"] is True
    plain = launch(c, cwd=str(project))
    assert wait(lambda: started_with(store, plain))["agent_tools"] is False

    a = agent(c, store, sid)
    assert a.get("/agent/task").json()["objective"] == "Find bugs"
    assert a.get("/workspace").status_code == 401  # never the user's API
    assert a.post("/grants", json={"session_id": sid, "artifacts": []}).status_code == 401
    assert c.get("/agent/task").status_code == 401  # and the CLI token isn't an agent credential
    old = a
    a = agent(c, store, sid)  # a new credential replaces the old one
    assert old.get("/agent/task").status_code == 401 and a.get("/agent/task").status_code == 200


def test_findings_are_own_or_granted_and_grants_never_widen(api):
    store, c, task_id, _ = api
    reviewer, other = launch(c, task_id), launch(c, task_id)
    say(c, other, block(GOOD))
    (theirs,) = wait(lambda: c.get(f"/findings?task_id={task_id}").json()["findings"])
    say(c, reviewer, block({**GOOD, "submission_id": "mine"}))
    wait(lambda: len(c.get(f"/findings?task_id={task_id}").json()["findings"]) == 2)
    user = publish_user(c, task_id)

    a = agent(c, store, reviewer)
    listed = a.get("/agent/findings").json()["findings"]
    assert [f["own"] for f in listed] == [True]  # independent by default: only its own
    r = a.get(f"/agent/findings/{theirs['finding_id']}")
    assert r.status_code == 403 and r.json()["error"]["code"] == "not_granted"

    g = c.post(
        "/grants",
        json={
            "session_id": reviewer,
            "artifacts": [{"id": theirs["finding_id"]}, {"id": user["finding_id"]}],
        },
    )
    assert g.status_code == 201
    assert a.get(f"/agent/findings/{theirs['finding_id']}").json()["summary"] == GOOD["summary"]
    c.post(
        f"/findings/{user['finding_id']}/versions",
        json={"claims": GOOD["claims"], "summary": "v2", "expected_version": 1},
    )
    read = a.get(f"/agent/findings/{user['finding_id']}").json()
    assert read["version"] == 1 and read["summary"] == GOOD["summary"]  # pinned: never moves to v2

    grant_id = next(x["grant_id"] for x in g.json()["grants"] if x["artifact_id"] == theirs["finding_id"])
    assert c.delete(f"/grants/{grant_id}").status_code == 200
    assert a.get(f"/agent/findings/{theirs['finding_id']}").status_code == 403


def test_excerpts_and_search_stay_inside_granted_ranges(api):
    store, c, task_id, _ = api
    reviewer = launch(c, task_id)
    src = observed_session_with(
        store,
        [
            ("message.user", {"text": "alpha needle"}),
            ("message.completed", {"text": "beta"}),
            ("message.user", {"text": "gamma needle"}),
        ],
    )
    cur = [e.global_cursor for e in read_events(store, session_id=src)]
    a = agent(c, store, reviewer)
    body = {"session_id": src, "after": cur[0] - 1, "through": cur[1]}
    assert a.post("/agent/excerpt", json=body).status_code == 403
    assert a.post("/agent/search", json={"query": "needle"}).json()["results"] == []

    c.post("/grants", json={"session_id": reviewer, "excerpts": [body]})
    got = a.post("/agent/excerpt", json=body).json()
    assert [m["text"] for m in got["messages"]] == ["alpha needle", "beta"]
    wider = {**body, "through": cur[2]}
    assert a.post("/agent/excerpt", json=wider).status_code == 403  # never wider than granted
    hits = a.post("/agent/search", json={"query": "needle"}).json()["results"]
    assert [h["cursor"] for h in hits] == [cur[0]]  # the third message is outside the grant


def test_every_read_is_recorded_with_its_exact_content(api):
    store, c, task_id, _ = api
    reviewer = launch(c, task_id)
    a = agent(c, store, reviewer)
    task = a.get("/agent/task").json()
    reads = c.get(f"/grants?session_id={reviewer}").json()["reads"]
    assert [r["tool"] for r in reads] == ["task_get"]
    assert reads[0]["content_hash"] == "sha256:" + hashlib.sha256(canonical(task)).hexdigest()
    assert store.blobs.read(reads[0]["content_hash"].split(":")[1]) == canonical(task)


def test_a_synthesis_links_to_its_inputs(api):
    store, c, task_id, _ = api
    writer = launch(c, task_id)
    f = publish_user(c, task_id)
    a = agent(c, store, writer)
    denied = a.post("/agent/artifacts", json={"title": "S", "text": "t", "inputs": [f["finding_id"]]})
    assert denied.status_code == 403
    c.post("/grants", json={"session_id": writer, "artifacts": [{"id": f["finding_id"]}]})
    r = a.post(
        "/agent/artifacts", json={"title": "Synthesis", "text": "Both agree.", "inputs": [f["finding_id"]]}
    )
    assert r.status_code == 201, r.text
    derived = c.get(f"/findings/{f['finding_id']}").json()["derived"]
    assert derived == [
        {"child_artifact_id": r.json()["artifact_id"], "child_version": 1, "relation": "input"}
    ]
    session = c.get(f"/sessions/{writer}").json()
    assert json.loads(json.dumps(session))["session_id"] == writer


def test_agent_reads_are_redacted_like_packets(api):
    from test_context import FAKE_TOKEN

    store, c, task_id, _ = api
    reviewer = launch(c, task_id)
    src = observed_session_with(
        store, [("tool.result", {"tool": "shell", "output": f"GITHUB_TOKEN={FAKE_TOKEN}"})]
    )
    cur = read_events(store, session_id=src)[0].global_cursor
    leaky = publish_user(c, task_id, summary=f"found {FAKE_TOKEN} in .env")
    c.post(
        "/grants",
        json={
            "session_id": reviewer,
            "artifacts": [{"id": leaky["finding_id"]}],
            "excerpts": [{"session_id": src, "after": cur - 1, "through": cur}],
        },
    )
    a = agent(c, store, reviewer)
    excerpt = a.post("/agent/excerpt", json={"session_id": src, "after": cur - 1, "through": cur}).json()
    finding = a.get(f"/agent/findings/{leaky['finding_id']}").json()
    probe = a.post("/agent/search", json={"query": FAKE_TOKEN[:12]}).json()  # can't search for a secret
    hits = a.post("/agent/search", json={"query": "GITHUB_TOKEN"}).json()

    def leaked(value) -> bool:  # a bare bool, so a failure never prints the token
        return FAKE_TOKEN in (value if isinstance(value, str) else json.dumps(value))

    for result in (excerpt, finding, probe, hits):
        assert not leaked(result)
    assert "[REDACTED:github-pat]" in excerpt["messages"][0]["text"] and excerpt["redactions"] == [
        "github-pat"
    ]
    assert probe["results"] == [] and hits["results"]
    for r in c.get(f"/grants?session_id={reviewer}").json()["reads"]:  # nor in what Robin recorded
        assert not leaked(store.blobs.read(r["content_hash"].split(":")[1]).decode())


def test_a_sessions_credential_dies_with_its_host(api):
    store, c, task_id, _ = api
    sid = launch(c, task_id)
    a = agent(c, store, sid)
    assert a.get("/agent/task").status_code == 200
    c.post(f"/sessions/{sid}/actions", json={"type": "stop", "action_id": aid()})
    wait(lambda: c.get(f"/sessions/{sid}").json()["connection_state"] == "disconnected")
    assert wait(lambda: a.get("/agent/task").status_code == 401)
    assert a.post("/agent/artifacts", json={"title": "t", "text": "x", "inputs": []}).status_code == 401


def test_a_grant_of_a_sessions_own_work_back_needs_an_explicit_allow(api):
    """FR-025 on the retrieval path: a finding built on the session's own output isn't granted by default."""
    _, c, task_id, _ = api
    writer = launch(c, task_id)
    say(c, writer, block(GOOD))
    (own,) = wait(lambda: c.get(f"/findings?task_id={task_id}").json()["findings"])
    derived = c.post(
        "/findings",
        json={
            **{k: GOOD[k] for k in ("summary", "claims")},
            "task_id": task_id,
            "parents": [own["finding_id"]],
        },
    ).json()
    body = {"session_id": writer, "artifacts": [{"id": derived["finding_id"]}]}
    r = c.post("/grants", json=body)
    assert r.status_code == 409 and r.json()["error"]["code"] == "context_loop"
    assert c.post("/grants", json={**body, "allow_loops": [derived["finding_id"]]}).status_code == 201
