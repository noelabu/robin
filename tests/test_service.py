"""The HTTP API, auth and event stream against the real stack: service, supervisor, scripted runtime."""

import asyncio
import json
import threading
import time
import uuid

import httpx
import pytest

from robin.service.auth import CSRF_HEADER
from robin.service.main import build, free_port


@pytest.fixture
def service(supervised):
    store = supervised
    port = free_port()
    server, auth = build(store, port)
    thread = threading.Thread(target=lambda: asyncio.run(server.serve()), daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.02)
    yield store, port, auth
    server.should_exit = True
    thread.join(10)


def base(port):
    return f"http://127.0.0.1:{port}"


def browser(port, auth):
    """A signed-in browser client: exchanged bootstrap secret, session cookie, CSRF header."""
    c = httpx.Client(base_url=base(port), timeout=10)
    r = c.post("/api/v1/auth/exchange", json={"secret": auth.new_bootstrap()})
    assert r.status_code == 200, r.text
    c.headers[CSRF_HEADER] = r.json()["csrf_token"]
    return c


def cli(port, auth):
    return httpx.Client(
        base_url=base(port), timeout=10, headers={"authorization": f"Bearer {auth.cli_token}"}
    )


def work(store):
    """A project folder beside (not containing) Robin's data directory."""
    path = store.data_dir.parent / "project"
    path.mkdir(exist_ok=True)
    return str(path)


def aid():
    return "act-" + uuid.uuid4().hex


def wait_session(c, sid, pred, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = c.get(f"/api/v1/sessions/{sid}").json()
        if pred(s):
            return s
        time.sleep(0.05)
    raise TimeoutError(c.get(f"/api/v1/sessions/{sid}").json())


def test_rejects_foreign_host_origin_and_anonymous(service):
    _, port, auth = service
    with httpx.Client(base_url=base(port), timeout=5) as c:
        assert c.get("/api/v1/workspace", headers={"host": "evil.example"}).status_code == 403
        assert c.get("/api/v1/workspace", headers={"origin": "https://evil.example"}).status_code == 403
        r = c.get("/api/v1/workspace")
        assert r.status_code == 401 and r.json()["error"]["code"] == "unauthenticated"
        assert set(r.json()["error"]) == {"code", "message", "correlation_id", "retry"}


def test_bootstrap_secret_is_single_use_and_spent_by_wrong_guesses(service):
    _, port, auth = service
    with httpx.Client(base_url=base(port), timeout=5) as c:
        secret = auth.new_bootstrap()
        assert c.post("/api/v1/auth/exchange", json={"secret": "wrong"}).status_code == 401
        assert c.post("/api/v1/auth/exchange", json={"secret": secret}).status_code == 401  # spent
        good = auth.new_bootstrap()
        r = c.post("/api/v1/auth/exchange", json={"secret": good})
        assert r.status_code == 200 and "httponly" in r.headers["set-cookie"].lower()
        assert "samesite=strict" in r.headers["set-cookie"].lower()
        assert c.post("/api/v1/auth/exchange", json={"secret": good}).status_code == 401


def test_bootstrap_expires(service):
    _, port, auth = service
    secret = auth.new_bootstrap()
    auth._bootstrap_expires = time.monotonic() - 1
    with httpx.Client(base_url=base(port), timeout=5) as c:
        assert c.post("/api/v1/auth/exchange", json={"secret": secret}).status_code == 401


def test_exchange_is_rate_limited(service):
    _, port, auth = service
    with httpx.Client(base_url=base(port), timeout=5) as c:
        for _ in range(10):
            c.post("/api/v1/auth/exchange", json={"secret": "guess"})
        assert c.post("/api/v1/auth/exchange", json={"secret": auth.new_bootstrap()}).status_code == 401


def test_browser_mutations_need_csrf_cli_does_not(service):
    store, port, auth = service
    b = browser(port, auth)
    no_csrf = httpx.Client(base_url=base(port), cookies=b.cookies, timeout=5)
    body = {"runtime": "fake", "cwd": work(store), "action_id": aid()}
    r = no_csrf.post("/api/v1/sessions/launch", json=body)
    assert r.status_code == 403 and r.json()["error"]["code"] == "csrf"
    assert cli(port, auth).get("/api/v1/workspace").status_code == 200


def test_security_headers(service):
    _, port, auth = service
    r = browser(port, auth).get("/api/v1/workspace")
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["cache-control"] == "no-store" and "access-control-allow-origin" not in r.headers


def test_launch_message_interrupt_and_idempotent_actions(service):
    store, port, auth = service
    c = browser(port, auth)
    launch_id = aid()
    body = {"runtime": "fake", "cwd": work(store), "prompt": "go", "action_id": launch_id}
    r = c.post("/api/v1/sessions/launch", json=body)
    assert r.status_code == 202 and r.json()["action"]["state"] == "submitted"
    sid = r.json()["session_id"]
    again = c.post("/api/v1/sessions/launch", json=body)  # a retried request
    assert again.status_code == 202 and again.json()["session_id"] == sid
    s = wait_session(c, sid, lambda s: s["runtime_state"] == "running")
    assert s["controls"]["interrupt"]["enabled"] and s["native_session_id"]

    intr = aid()
    assert (
        c.post(f"/api/v1/sessions/{sid}/actions", json={"type": "interrupt", "action_id": intr}).status_code
        == 202
    )
    deadline = time.time() + 10
    while c.get(f"/api/v1/actions/{intr}").json()["state"] != "acknowledged" and time.time() < deadline:
        time.sleep(0.05)
    assert c.get(f"/api/v1/actions/{intr}").json()["state"] == "acknowledged"

    m = aid()
    c.post(f"/api/v1/sessions/{sid}/actions", json={"type": "message", "text": "hi", "action_id": m})
    conflict = c.post(
        f"/api/v1/sessions/{sid}/actions", json={"type": "message", "text": "other", "action_id": m}
    )
    assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "action_id_conflict"
    c.post(f"/api/v1/sessions/{sid}/actions", json={"type": "stop", "action_id": aid()})
    stopped = wait_session(c, sid, lambda s: s["connection_state"] == "disconnected")
    assert not stopped["controls"]["message"]["enabled"]
    assert stopped["controls"]["message"]["reason"] == "session is disconnected"


def test_validation_and_error_codes(service):
    store, port, auth = service
    c = browser(port, auth)
    r = c.post(
        "/api/v1/sessions/launch", json={"runtime": "fake", "cwd": "relative/path", "action_id": aid()}
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_cwd"
    r = c.post(
        "/api/v1/sessions/launch",
        json={"runtime": "fake", "cwd": str(store.artifacts.path), "action_id": aid()},
    )
    assert r.status_code == 422  # never run an agent inside Robin's artifact store
    (store.data_dir / "hooks").mkdir(exist_ok=True)
    r = c.post(
        "/api/v1/sessions/launch",
        json={"runtime": "fake", "cwd": str(store.data_dir / "hooks"), "action_id": aid()},
    )
    assert r.status_code == 422  # nor anywhere in its data directory, beside cli.token and robin.db
    r = c.post("/api/v1/sessions/launch", json={"runtime": "mystery", "cwd": "/tmp", "action_id": aid()})
    assert r.status_code == 409 and r.json()["error"]["code"] == "unsupported"
    r = c.post("/api/v1/sessions/nope/actions", json={"type": "interrupt", "action_id": aid()})
    assert r.status_code == 404
    assert (
        c.post("/api/v1/sessions/x/actions", json={"type": "message", "action_id": "short"}).status_code
        == 422
    )
    assert c.get("/api/v1/actions/does-not-exist").status_code == 404


def sse_events(c, path, want, timeout=10.0, headers=None):
    """Read SSE frames until `want` frames of type event arrive (or a resync)."""
    frames = []
    with c.stream("GET", path, timeout=timeout, headers=headers or {}) as r:
        assert r.status_code == 200
        frame = {}
        for line in r.iter_lines():
            if line.startswith("id: "):
                frame["id"] = int(line[4:])
            elif line.startswith("event: "):
                frame["event"] = line[7:]
            elif line.startswith("data: "):
                frame["data"] = json.loads(line[6:])
            elif line == "" and frame:
                frames.append(frame)
                if (
                    frame.get("event") == "resync_required"
                    or sum(f.get("event") == "event" for f in frames) >= want
                ):
                    return frames
                frame = {}
    return frames


def test_snapshot_then_stream_and_resume(service):
    store, port, auth = service
    c = browser(port, auth)
    watermark = c.get("/api/v1/workspace").json()["watermark"]
    r = c.post(
        "/api/v1/sessions/launch",
        json={"runtime": "fake", "cwd": work(store), "prompt": "go", "action_id": aid()},
    )
    sid = r.json()["session_id"]
    first = sse_events(c, f"/api/v1/stream?after={watermark}", want=5)
    ids = [f["id"] for f in first]
    assert ids == sorted(ids) and ids[0] > watermark and len(set(ids)) == len(ids)
    assert first[0]["data"]["session"]["session_id"] == sid  # projection travels with the event
    resumed = sse_events(c, "/api/v1/stream", want=2, headers={"last-event-id": str(ids[-1])})
    assert resumed[0]["id"] > ids[-1]
    c.post(f"/api/v1/sessions/{sid}/actions", json={"type": "stop", "action_id": aid()})


def test_stream_cursor_ahead_requires_resync(service):
    _, port, auth = service
    frames = sse_events(browser(port, auth), "/api/v1/stream?after=999999", want=1)
    assert frames[0]["event"] == "resync_required"


def test_cli_can_mint_a_new_signin_link(service):
    _, port, auth = service
    r = cli(port, auth).post("/api/v1/auth/bootstrap")
    secret = r.json()["url"].split("#bootstrap=")[1]
    with httpx.Client(base_url=base(port), timeout=5) as c:
        assert c.post("/api/v1/auth/exchange", json={"secret": secret}).status_code == 200
    assert browser(port, auth).post("/api/v1/auth/bootstrap").status_code == 401  # browsers can't


async def test_change_wait_does_not_miss_a_notify_that_came_first(store):
    """Regression (R20261015-145038-I2): a notify between the stream read and its wait is not missed."""
    from robin.service.runtime import ServiceRuntime

    rt = ServiceRuntime(store, start_supervisor=False)
    seen = rt.version
    await rt.notify()  # lands before anyone waits
    started = time.monotonic()
    assert await rt.wait_change(seen, timeout=5) != seen
    assert time.monotonic() - started < 1


def test_supervisor_link_recovers_from_unexpected_errors(supervised, monkeypatch):
    """Regression (R20261015-145038-I1): an error inside the bridge doesn't end the reconnect loop."""
    from robin.supervisor import client as client_mod

    real_run = client_mod.SupervisorBridge.run
    calls = {"n": 0}

    async def flaky_run(self):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("database is locked")
        await real_run(self)

    monkeypatch.setattr(client_mod.SupervisorBridge, "run", flaky_run)
    store = supervised
    port = free_port()
    server, auth = build(store, port)
    thread = threading.Thread(target=lambda: asyncio.run(server.serve()), daemon=True)
    thread.start()
    try:
        c = cli(port, auth)
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                if calls["n"] >= 2 and c.get("/api/v1/health").json()["supervisor_connected"]:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
        assert calls["n"] >= 2 and c.get("/api/v1/health").json()["supervisor_connected"]
    finally:
        server.should_exit = True
        thread.join(10)


def test_reloaded_page_recovers_csrf_from_cookie(service):
    _, port, auth = service
    b = browser(port, auth)
    assert b.get("/api/v1/auth/session").json()["csrf_token"] == b.headers[CSRF_HEADER]
    with httpx.Client(base_url=base(port), timeout=5) as anon:
        assert anon.get("/api/v1/auth/session").status_code == 401


def test_phase1_exit_session_survives_a_full_service_restart(supervised):
    """Launch, stop the whole HTTP service, start a new one: the session is still running and controllable."""

    def start(store):
        port = free_port()
        server, auth = build(store, port)
        thread = threading.Thread(target=lambda: asyncio.run(server.serve()), daemon=True)
        thread.start()
        while not server.started:
            time.sleep(0.02)
        return server, thread, port, auth

    store = supervised
    server, thread, port, auth = start(store)
    c = cli(port, auth)
    r = c.post(
        "/api/v1/sessions/launch",
        json={"runtime": "fake", "cwd": work(store), "prompt": "go", "action_id": aid()},
    )
    sid = r.json()["session_id"]
    wait_session(c, sid, lambda s: s["runtime_state"] == "running")
    server.should_exit = True
    thread.join(10)

    server2, thread2, port2, auth2 = start(store)  # a new service process
    try:
        c2 = cli(port2, auth2)
        s = wait_session(
            c2,
            sid,
            lambda s: (
                s["connection_state"] == "connected"
                and s["runtime_state"] in ("running", "waiting_for_input")
            ),
        )
        assert s["controls"]["interrupt"]["enabled"]
        intr = aid()
        c2.post(f"/api/v1/sessions/{sid}/actions", json={"type": "interrupt", "action_id": intr})
        deadline = time.time() + 10
        while c2.get(f"/api/v1/actions/{intr}").json()["state"] != "acknowledged" and time.time() < deadline:
            time.sleep(0.05)
        assert c2.get(f"/api/v1/actions/{intr}").json()["state"] == "acknowledged"
        launches = [
            e
            for e in c2.get("/api/v1/events?limit=500").json()["events"]
            if e["session_id"] == sid and e["event_type"] == "connection.connected"
        ]
        assert len(launches) == 1  # never relaunched
        c2.post(f"/api/v1/sessions/{sid}/actions", json={"type": "stop", "action_id": aid()})
    finally:
        server2.should_exit = True
        thread2.join(10)


def test_projects_tasks_and_launch_for_a_task(service):
    store, port, auth = service
    c = browser(port, auth)
    proj = store.data_dir.parent / "taskproj"
    proj.mkdir()
    (proj / "PRD.md").write_text("# PRD\n")
    p = c.post("/api/v1/projects", json={"path": str(proj)})
    assert p.status_code == 201
    pid = p.json()["project_id"]
    t = c.post(
        "/api/v1/tasks",
        json={"project_id": pid, "title": "PRD review", "objective": "Review it", "files": ["PRD.md"]},
    )
    assert t.status_code == 201
    tid = t.json()["task_id"]
    r = c.post(
        "/api/v1/sessions/launch",
        json={"runtime": "fake", "task_id": tid, "role": "reviewer", "prompt": "go", "action_id": aid()},
    )
    assert r.status_code == 202
    sid = r.json()["session_id"]
    s = wait_session(c, sid, lambda s: s["runtime_state"] in ("running", "waiting_for_input"))
    assert s["current_task_id"] == tid and s["execution_cwd"] == str(
        proj.resolve()
    )  # project folder by default
    task = c.get(f"/api/v1/tasks/{tid}").json()
    assert [x["session_id"] for x in task["sessions"]] == [sid] and task["sessions"][0]["role"] == "reviewer"
    assert [x["session_id"] for x in c.get(f"/api/v1/sessions?task_id={tid}").json()["sessions"]] == [sid]
    evs = c.get("/api/v1/events?limit=500").json()["events"]
    assert any(e["task_id"] == tid for e in evs if e["session_id"] == sid)  # events attributed to the task

    done = c.patch(f"/api/v1/tasks/{tid}", json={"expected_revision": 1, "status": "completed"})
    assert done.status_code == 422 and done.json()["error"]["code"] == "evidence_required"
    stale = c.patch(f"/api/v1/tasks/{tid}", json={"expected_revision": 9, "title": "x"})
    assert stale.status_code == 409 and stale.json()["error"]["detail"] == {"revision": 1}
    c.patch(f"/api/v1/sessions/{sid}", json={"archived": True})
    assert sid not in [x["session_id"] for x in c.get("/api/v1/sessions").json()["sessions"]]
    assert sid in [
        x["session_id"] for x in c.get("/api/v1/sessions?include_archived=true").json()["sessions"]
    ]
    c.post(f"/api/v1/sessions/{sid}/actions", json={"type": "stop", "action_id": aid()})


def test_attention_queue_answers_an_approval_once(service):
    store, port, auth = service
    c = browser(port, auth)
    r = c.post(
        "/api/v1/sessions/launch",
        json={"runtime": "fake", "cwd": work(store), "prompt": "go", "action_id": aid()},
    )
    sid = r.json()["session_id"]
    deadline = time.time() + 10
    items = []
    while not items and time.time() < deadline:
        items = c.get(f"/api/v1/attention?session_id={sid}").json()["items"]
        time.sleep(0.05)
    (item,) = items
    assert item["kind"] == "approval" and item["detail"]["tool"] == "Bash"
    first = aid()
    r = c.post(
        f"/api/v1/attention/{item['attention_id']}/responses", json={"decision": "allow", "action_id": first}
    )
    assert r.status_code == 202
    again = c.post(
        f"/api/v1/attention/{item['attention_id']}/responses", json={"decision": "deny", "action_id": aid()}
    )
    assert again.status_code == 409  # one request, one response
    retry = c.post(
        f"/api/v1/attention/{item['attention_id']}/responses", json={"decision": "allow", "action_id": first}
    )
    assert retry.status_code == 202  # a retried request reports the stored action
    deadline = time.time() + 10
    while (
        c.get(f"/api/v1/attention/{item['attention_id']}").json()["state"] != "resolved"
        and time.time() < deadline
    ):
        time.sleep(0.05)
    assert c.get(f"/api/v1/attention/{item['attention_id']}").json()["resolution"] == "allowed"
    c.post(f"/api/v1/sessions/{sid}/actions", json={"type": "stop", "action_id": aid()})


def test_a_question_is_answered_through_the_api(service):
    store, port, auth = service
    c = browser(port, auth)
    sid = c.post(
        "/api/v1/sessions/launch",
        json={"runtime": "fake", "cwd": work(store), "prompt": "ask", "action_id": aid()},
    ).json()["session_id"]
    deadline = time.time() + 10
    items = []
    while not items and time.time() < deadline:
        items = c.get(f"/api/v1/attention?session_id={sid}").json()["items"]
        time.sleep(0.05)
    (item,) = items
    assert item["kind"] == "question" and item["allowed_responses"] == ["answer", "deny"]
    url = f"/api/v1/attention/{item['attention_id']}/responses"
    bad = c.post(url, json={"decision": "answer", "answers": {"Which color?": "red"}, "action_id": aid()})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "invalid_answers"
    assert c.post(url, json={"decision": "answer", "action_id": aid()}).status_code == 422  # no answers
    answers = {"Which database?": "Postgres", "Which features?": ["Auth", "Billing"]}
    r = c.post(url, json={"decision": "answer", "answers": answers, "action_id": aid()})
    assert r.status_code == 202
    expected = 'Answers: {"Which database?": "Postgres", "Which features?": "Auth, Billing"}'
    deadline = time.time() + 10
    texts: list[str] = []
    while expected not in texts and time.time() < deadline:
        texts = [m.get("text") for m in c.get(f"/api/v1/sessions/{sid}/conversation").json()["messages"]]
        time.sleep(0.05)
    assert expected in texts
    assert c.get(f"/api/v1/attention/{item['attention_id']}").json()["resolution"] == "answered"
    c.post(f"/api/v1/sessions/{sid}/actions", json={"type": "stop", "action_id": aid()})


def test_declining_a_question_tells_the_agent(service):
    store, port, auth = service
    c = browser(port, auth)
    sid = c.post(
        "/api/v1/sessions/launch",
        json={"runtime": "fake", "cwd": work(store), "prompt": "ask", "action_id": aid()},
    ).json()["session_id"]
    deadline = time.time() + 10
    items = []
    while not items and time.time() < deadline:
        items = c.get(f"/api/v1/attention?session_id={sid}").json()["items"]
        time.sleep(0.05)
    r = c.post(
        f"/api/v1/attention/{items[0]['attention_id']}/responses",
        json={"decision": "deny", "action_id": aid()},
    )
    assert r.status_code == 202
    deadline = time.time() + 10
    texts: list[str] = []
    while "No answer: The user declined to answer" not in texts and time.time() < deadline:
        texts = [m.get("text") for m in c.get(f"/api/v1/sessions/{sid}/conversation").json()["messages"]]
        time.sleep(0.05)
    assert "No answer: The user declined to answer" in texts
    c.post(f"/api/v1/sessions/{sid}/actions", json={"type": "stop", "action_id": aid()})


def test_the_conversation_carries_the_cursor_it_covers(service):
    store, port, auth = service
    c = browser(port, auth)
    sid = c.post(
        "/api/v1/sessions/launch",
        json={"runtime": "fake", "cwd": work(store), "prompt": "ask", "action_id": aid()},
    ).json()["session_id"]
    deadline = time.time() + 10
    while not c.get(f"/api/v1/attention?session_id={sid}").json()["items"] and time.time() < deadline:
        time.sleep(0.05)
    page = c.get(f"/api/v1/sessions/{sid}/conversation").json()
    evs = c.get(f"/api/v1/events?session_id={sid}&limit=500").json()["events"]
    assert page["cursor"] >= max(e["global_cursor"] for e in evs)
    c.post(f"/api/v1/sessions/{sid}/actions", json={"type": "stop", "action_id": aid()})


def test_handoff_and_reclaim_through_the_api(service):
    store, port, auth = service
    c = browser(port, auth)
    sid = c.post(
        "/api/v1/sessions/launch", json={"runtime": "fake", "cwd": work(store), "action_id": aid()}
    ).json()["session_id"]
    deadline = time.time() + 10
    while c.get(f"/api/v1/sessions/{sid}").json()["runtime_state"] != "idle" and time.time() < deadline:
        time.sleep(0.05)
    assert c.post(f"/api/v1/sessions/{sid}/terminal/open").status_code == 409  # Robin still holds it
    handoff = aid()
    assert (
        c.post(f"/api/v1/sessions/{sid}/actions", json={"type": "handoff", "action_id": handoff}).status_code
        == 202
    )
    while c.get(f"/api/v1/actions/{handoff}").json()["state"] != "acknowledged" and time.time() < deadline:
        time.sleep(0.05)
    s = c.get(f"/api/v1/sessions/{sid}").json()
    assert s["control_owner"] == "terminal"
    assert s["controls"]["message"] == {
        "enabled": False,
        "reason": "handed off to a terminal; take control back first",
    }
    assert s["controls"]["reclaim"]["enabled"] is True
    info = c.get(f"/api/v1/sessions/{sid}/terminal")  # the scripted runtime has no terminal of its own
    assert info.status_code == 409 and info.json()["error"]["code"] == "unsupported"
    reclaim = aid()
    assert (
        c.post(f"/api/v1/sessions/{sid}/actions", json={"type": "reclaim", "action_id": reclaim}).status_code
        == 202
    )
    deadline = time.time() + 10
    while c.get(f"/api/v1/actions/{reclaim}").json()["state"] != "acknowledged" and time.time() < deadline:
        time.sleep(0.05)
    assert c.get(f"/api/v1/sessions/{sid}").json()["control_owner"] == "robin"
    c.post(f"/api/v1/sessions/{sid}/actions", json={"type": "stop", "action_id": aid()})


def test_a_session_started_outside_robin_is_continued_through_the_api(service):
    from robin.events.ingest import ensure_session

    store, port, auth = service
    c = browser(port, auth)
    sid = ensure_session(
        store, runtime="fake", mode="observed", native_session_id="obs-api", execution_cwd=work(store)
    )
    s = c.get(f"/api/v1/sessions/{sid}").json()
    assert s["controls"]["takeover"] == {"enabled": True, "reason": None}
    assert s["controls"]["message"]["enabled"] is False
    act = f"/api/v1/sessions/{sid}/actions"
    assert c.post(act, json={"type": "takeover", "action_id": aid()}).status_code == 422  # no text
    takeover = aid()
    assert (
        c.post(act, json={"type": "takeover", "action_id": takeover, "text": "keep going"}).status_code == 202
    )
    deadline = time.time() + 10
    while c.get(f"/api/v1/actions/{takeover}").json()["state"] != "acknowledged" and time.time() < deadline:
        time.sleep(0.05)
    s = wait_session(c, sid, lambda s: s["connection_state"] == "connected")
    assert s["mode"] == "managed" and s["controls"]["message"]["enabled"] is True
    assert s["controls"]["takeover"] == {"enabled": False, "reason": "Robin already manages this session"}
    repeat = c.post(act, json={"type": "takeover", "action_id": takeover, "text": "keep going"})
    assert repeat.status_code == 202 and repeat.json()["action"]["state"] == "acknowledged"
    again = c.post(act, json={"type": "takeover", "action_id": aid(), "text": "again"})
    assert again.status_code == 409 and again.json()["error"]["code"] == "already_managed"
    c.post(act, json={"type": "stop", "action_id": aid()})


def test_launch_warns_when_sessions_share_a_folder_and_history_is_filtered(service):
    store, port, auth = service
    c = browser(port, auth)
    folder = work(store)
    first = c.post(
        "/api/v1/sessions/launch", json={"runtime": "fake", "cwd": folder, "action_id": aid()}
    ).json()
    assert first["warnings"] == []
    second = c.post(
        "/api/v1/sessions/launch", json={"runtime": "fake", "cwd": folder, "action_id": aid()}
    ).json()
    assert first["session_id"] in second["warnings"][0]
    a, b = first["session_id"], second["session_id"]
    deadline = time.time() + 10
    while not c.get(f"/api/v1/sessions/{a}").json()["shared_folder_with"] and time.time() < deadline:
        time.sleep(0.05)
    assert c.get(f"/api/v1/sessions/{a}").json()["shared_folder_with"] == [b]
    ws = c.get("/api/v1/workspace").json()
    assert "projects" in ws and "tasks" in ws
    while len(c.get(f"/api/v1/events?session_id={a}").json()["events"]) < 3 and time.time() < deadline:
        time.sleep(0.05)
    latest = c.get(f"/api/v1/events?session_id={a}&latest=true&limit=2&previews=true").json()["events"]
    every = c.get(f"/api/v1/events?session_id={a}&limit=500").json()["events"]
    assert [e["global_cursor"] for e in latest] == [e["global_cursor"] for e in every[-2:]]
    assert all("preview" in e and "noise" in e and e["session_id"] == a for e in latest)
    for sid in (a, b):
        c.post(f"/api/v1/sessions/{sid}/actions", json={"type": "stop", "action_id": aid()})
    # an archived or stopped session no longer counts
    c.patch(f"/api/v1/sessions/{b}", json={"archived": True})
    assert c.get(f"/api/v1/sessions/{a}").json()["shared_folder_with"] == []


def test_event_previews_are_redacted_in_events_and_stream(service):
    from test_context import FAKE_TOKEN, observed_session_with

    store, port, auth = service
    c = browser(port, auth)
    watermark = c.get("/api/v1/workspace").json()["watermark"]
    sid = observed_session_with(
        store,
        [
            ("message.completed", {"type": "message_completed", "text": f"the key is {FAKE_TOKEN}"}),
            (
                "tool.started",
                {"type": "tool_started", "tool": "Bash", "input": {"command": f"echo {FAKE_TOKEN}"}},
            ),
        ],
    )
    events = c.get(f"/api/v1/events?session_id={sid}&previews=true").json()["events"]
    frames = sse_events(c, f"/api/v1/stream?after={watermark}", want=2)
    previews = [e["preview"] for e in events] + [
        f["data"]["preview"] for f in frames if f.get("event") == "event"
    ]
    assert len(previews) == 4 and all(p and "[REDACTED:github-pat]" in p for p in previews)
    assert FAKE_TOKEN not in json.dumps(events) + json.dumps(frames)


def test_session_list_pages_and_snapshot_is_bounded(service, monkeypatch):
    from robin.events.ingest import ensure_session
    from robin.service import app as service_app

    store, port, auth = service
    monkeypatch.setattr(service_app, "SNAPSHOT_SESSIONS", 3)
    observed = [ensure_session(store, runtime="claude", mode="observed") for _ in range(7)]
    managed = ensure_session(store, runtime="fake", mode="managed")
    c = cli(port, auth)

    seen, offset = [], 0
    while offset is not None:
        page = c.get(f"/api/v1/sessions?mode=observed&limit=3&offset={offset}").json()
        assert page["total"] == 7
        seen += [s["session_id"] for s in page["sessions"]]
        offset = page["next_offset"]
    assert sorted(seen) == sorted(observed) and len(seen) == 7  # every session once, none twice

    ws = c.get("/api/v1/workspace").json()
    ids = [s["session_id"] for s in ws["sessions"]]
    assert managed in ids  # managed sessions are always in the snapshot
    assert len(ids) == 4 and ws["observed_total"] == 7
    assert ids[1:] == seen[:3]  # the most recent observed ones, in the same order as page one

    assert c.get("/api/v1/sessions?limit=0").status_code == 422
    assert c.get("/api/v1/sessions?limit=1001").status_code == 422


def test_paging_from_a_cursor_skips_nothing_when_sessions_are_archived_meanwhile(service):
    from robin.events.ingest import ensure_session

    store, port, auth = service
    observed = [ensure_session(store, runtime="claude", mode="observed") for _ in range(7)]
    c = cli(port, auth)
    page = c.get("/api/v1/sessions?mode=observed&limit=3").json()
    seen = [s["session_id"] for s in page["sessions"]]
    # Archive one of the first page: an offset of 3 would now start one row late and skip a session.
    c.patch(f"/api/v1/sessions/{seen[0]}", json={"archived": True})
    cursor = page["next_before"]
    while cursor:
        q = httpx.QueryParams(
            mode="observed", limit=3, before_updated_at=cursor["updated_at"], before_id=cursor["session_id"]
        )
        page = c.get(f"/api/v1/sessions?{q}").json()
        seen += [s["session_id"] for s in page["sessions"]]
        cursor = page["next_before"]
    assert sorted(seen) == sorted(observed) and len(seen) == 7

    assert c.get("/api/v1/sessions?before_id=x").status_code == 422  # both halves of the cursor, or neither
    naive = c.get("/api/v1/sessions?before_id=x&before_updated_at=2026-10-15T00:00:00")
    assert naive.status_code == 422


def test_conversation_through_the_api(service):
    from test_context import FAKE_TOKEN, observed_session_with

    store, port, auth = service
    c = browser(port, auth)
    sid = observed_session_with(
        store,
        [
            ("message.user", {"type": "user_message", "text": "hello"}),
            ("message.completed", {"type": "message_completed", "text": f"key {FAKE_TOKEN}"}),
        ],
    )
    r = c.get(f"/api/v1/sessions/{sid}/conversation")
    assert r.status_code == 200
    assert [(m["role"], m["text"]) for m in r.json()["messages"]] == [
        ("user", "hello"),
        ("assistant", "key [REDACTED:github-pat]"),
    ]
    assert c.get("/api/v1/sessions/nope/conversation").status_code == 404


TERMINAL_QUESTION = {
    "session_id": "ext-route",
    "tool_name": "AskUserQuestion",
    "tool_use_id": "toolu_route",
    "tool_input": {"questions": [{"question": "Ship it?", "options": [{"label": "Yes"}, {"label": "No"}]}]},
}


def register_question(c, payload=TERMINAL_QUESTION):
    return c.post(
        "/api/v1/hooks/claude/questions",
        json={"relay_id": uuid.uuid4().hex, "relayed_at": time.time(), "payload": payload},
    )


def test_only_claude_has_a_question_hook_and_only_questions_register(service):
    store, port, auth = service
    c = cli(port, auth)
    body = {"relay_id": "x", "relayed_at": time.time(), "payload": TERMINAL_QUESTION}
    r = c.post("/api/v1/hooks/codex/questions", json=body)
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"
    r = register_question(c, {**TERMINAL_QUESTION, "tool_name": "Bash"})
    assert r.status_code == 422


def test_a_registered_question_is_pending_until_answered_once(service):
    store, port, auth = service
    c = cli(port, auth)
    reg = register_question(c).json()
    assert reg["attention_id"] and reg["wait_s"] == 300 and reg["until"] > time.time()
    assert register_question(c).json()["until"] == reg["until"]  # a retried registration keeps its deadline
    att = reg["attention_id"]
    assert c.get(f"/api/v1/attention/{att}/reply?wait=0").json()["status"] == "pending"

    first = aid()
    body = {"decision": "answer", "answers": {"Ship it?": "Yes"}, "action_id": first}
    r = c.post(f"/api/v1/attention/{att}/responses", json=body)
    assert r.status_code == 202 and r.json()["action"]["action_id"] == first
    retry = c.post(f"/api/v1/attention/{att}/responses", json=body)
    assert retry.status_code == 202 and retry.json()["action"]["action_id"] == first
    again = c.post(f"/api/v1/attention/{att}/responses", json={"decision": "deny", "action_id": aid()})
    assert again.status_code == 409 and again.json()["error"]["code"] == "not_answerable"
    assert c.get(f"/api/v1/attention/{att}/reply?wait=0").json() == {
        "status": "answered",
        "answers": {"Ship it?": "Yes"},
    }


def test_releasing_a_registered_question_hands_it_to_the_terminal(service):
    store, port, auth = service
    c = cli(port, auth)
    att = register_question(c).json()["attention_id"]
    assert c.post(f"/api/v1/attention/{att}/release").json() == {"status": "closed"}
    item = c.get(f"/api/v1/attention/{att}").json()
    assert item["allowed_responses"] == [] and item["detail"]["answer_in"] == "terminal"
    r = c.post(f"/api/v1/attention/{att}/responses", json={"decision": "deny", "action_id": aid()})
    assert r.status_code == 409 and r.json()["error"]["code"] == "answer_in_terminal"


def test_question_wait_setting(service):
    store, port, auth = service
    c = cli(port, auth)
    assert c.get("/api/v1/settings/questions").json() == {"wait_s": 300}
    assert c.put("/api/v1/settings/questions", json={"wait_s": 120}).json() == {"wait_s": 120}
    assert c.get("/api/v1/settings/questions").json() == {"wait_s": 120}
    assert c.put("/api/v1/settings/questions", json={"wait_s": 541}).status_code == 422
    assert c.put("/api/v1/settings/questions", json={"wait_s": -1}).status_code == 422
    assert c.get("/api/v1/settings/questions").json() == {"wait_s": 120}
    assert c.put("/api/v1/settings/questions", json={"wait_s": 0}).json() == {"wait_s": 0}
    assert register_question(c).json() == {"attention_id": None, "reason": "disabled"}


def test_a_managed_question_cannot_be_handed_to_a_terminal(service):
    store, port, auth = service
    c = browser(port, auth)
    sid = c.post(
        "/api/v1/sessions/launch",
        json={"runtime": "fake", "cwd": work(store), "prompt": "ask", "action_id": aid()},
    ).json()["session_id"]
    deadline = time.time() + 10
    items = []
    while not items and time.time() < deadline:
        items = c.get(f"/api/v1/attention?session_id={sid}").json()["items"]
        time.sleep(0.05)
    r = c.post(
        f"/api/v1/attention/{items[0]['attention_id']}/responses",
        json={"decision": "terminal", "action_id": aid()},
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_response"
    c.post(f"/api/v1/sessions/{sid}/actions", json={"type": "stop", "action_id": aid()})


class Tailnet:
    """Tailscale faked at its two calls: this Mac is reachable on ::1, and whois answers `owner`."""

    def __init__(self, monkeypatch):
        from robin import remote

        self.owner = True
        self.me = remote.Tailnet(ips=("::1",), user_id=1, login="me@example.com", fqdn="mac.tail1.ts.net")
        monkeypatch.setattr(remote, "tailnet_status", lambda: self.me)
        monkeypatch.setattr(remote, "remove_legacy_serve", lambda port: None)

        async def check(_self, ip):
            return self.owner and ip == "::1"

        monkeypatch.setattr(remote.TailnetTrust, "check", check)


def turn_on(port, auth):
    r = cli(port, auth).put("/api/v1/settings/remote", json={"enabled": True})
    assert r.status_code == 200, r.text
    return r.json()


def phone(port, **headers):
    return httpx.Client(base_url=f"http://[::1]:{port}", timeout=5, headers=headers)


def test_owners_device_is_signed_in_without_a_link(service, monkeypatch):
    _, port, auth = service
    Tailnet(monkeypatch)
    view = turn_on(port, auth)
    assert view["listening"] and view["owner"] == "me@example.com"
    assert view["urls"][:2] == [f"http://mac:{port}", f"http://mac.tail1.ts.net:{port}"]
    with phone(port, host=f"mac:{port}") as c:
        assert c.get("/api/v1/workspace").status_code == 401
        r = c.get("/api/v1/auth/session")
        assert r.status_code == 200 and "secure" not in r.headers["set-cookie"].lower()
        assert "samesite=strict" in r.headers["set-cookie"].lower()
        csrf = r.json()["csrf_token"]
        assert c.get("/api/v1/workspace").status_code == 200
        assert c.get("/api/v1/auth/session").json()["csrf_token"] == csrf  # a reload keeps the session
        assert c.put("/api/v1/presence", json={"session_id": None}).status_code == 403  # no CSRF
        r = c.put("/api/v1/presence", json={"session_id": None}, headers={CSRF_HEADER: csrf})
        assert r.status_code == 200


def test_other_devices_are_refused_everywhere(service, monkeypatch):
    _, port, auth = service
    net = Tailnet(monkeypatch)
    turn_on(port, auth)
    net.owner = False
    with phone(port, host=f"mac:{port}") as c:
        for path in ("/", "/manifest.webmanifest", "/api/v1/auth/session"):
            r = c.get(path)
            assert r.status_code == 403 and r.json()["error"]["code"] == "not_your_device", path


@pytest.mark.parametrize(
    ("host", "origin", "ok"),
    [
        ("mac:{port}", None, True),
        ("MAC.tail1.ts.net:{port}", "http://mac.tail1.ts.net:{port}", True),
        ("[::1]:{port}", "http://[::1]:{port}", True),
        ("evil.com:{port}", None, False),  # DNS rebinding
        ("mac", None, False),
        ("127.0.0.1:{port}", None, False),
        ("mac:{port}", "https://mac:{port}", False),
        ("mac:{port}", "http://evil.com", False),
    ],
)
def test_tailnet_host_and_origin(service, monkeypatch, host, origin, ok):
    _, port, auth = service
    Tailnet(monkeypatch)
    turn_on(port, auth)
    headers = {"host": host.format(port=port)}
    if origin:
        headers["origin"] = origin.format(port=port)
    with phone(port, **headers) as c:
        assert (c.get("/api/v1/auth/session").status_code == 200) is ok


def test_tailnet_refuses_cli_token_links_and_loopback_cookies(service, monkeypatch):
    _, port, auth = service
    Tailnet(monkeypatch)
    turn_on(port, auth)
    mac = browser(port, auth)
    with phone(port, host=f"mac:{port}") as c:
        bearer = {"authorization": f"Bearer {auth.cli_token}"}
        assert c.get("/api/v1/workspace", headers=bearer).status_code == 401
        r = c.post("/api/v1/auth/exchange", json={"secret": auth.new_bootstrap()})
        assert r.status_code == 403
        c.cookies.set("robin_session", mac.cookies.get("robin_session"))
        assert c.get("/api/v1/workspace").status_code == 401  # a Mac session doesn't work here
        sid = c.get("/api/v1/auth/session").cookies.get("robin_session")
    with httpx.Client(base_url=base(port), timeout=5, cookies={"robin_session": sid}) as local:
        assert local.get("/api/v1/workspace").status_code == 401  # nor a phone session on the Mac
    r = cli(port, auth).put("/api/v1/settings/remote", json={"enabled": True})
    assert r.status_code == 200
    with phone(port, host=f"mac:{port}") as c:
        c.get("/api/v1/auth/session")
        csrf = c.get("/api/v1/auth/session").json()["csrf_token"]
        r = c.put("/api/v1/settings/remote", json={"enabled": False}, headers={CSRF_HEADER: csrf})
        assert r.status_code == 403  # only the Mac turns it off


def test_cross_site_requests_dont_get_a_session(service, monkeypatch):
    _, port, auth = service
    Tailnet(monkeypatch)
    turn_on(port, auth)
    with phone(port, host=f"mac:{port}") as c:
        assert c.get("/api/v1/auth/session", headers={"sec-fetch-site": "cross-site"}).status_code == 401
        assert c.get("/api/v1/auth/session", headers={"sec-fetch-site": "same-site"}).status_code == 401


def test_off_stops_listening_and_signs_phones_out(service, monkeypatch):
    store, port, auth = service
    from robin import remote

    Tailnet(monkeypatch)
    turn_on(port, auth)
    with phone(port, host=f"mac:{port}") as c:
        c.get("/api/v1/auth/session")
    assert any(s.remote for s in auth._sessions.values())
    view = cli(port, auth).put("/api/v1/settings/remote", json={"enabled": False}).json()
    assert view == {
        "enabled": False,
        "listening": False,
        "urls": [],
        "owner": None,
        "problem": None,
        "removed_serve": None,
    }
    assert not any(s.remote for s in auth._sessions.values()) and auth.listen_ips == frozenset()
    assert remote.settings(store) == {"enabled": False}
    with pytest.raises(httpx.ConnectError), phone(port, host=f"mac:{port}") as c:
        c.get("/api/v1/auth/session")


def test_waits_for_tailscale_and_survives_a_restart(service, monkeypatch):
    store, port, auth = service
    from robin import remote

    net = Tailnet(monkeypatch)

    def logged_out():
        raise remote.RemoteError(409, "tailscale_logged_out", "Tailscale isn't connected")

    monkeypatch.setattr(remote, "tailnet_status", logged_out)
    view = turn_on(port, auth)
    assert view["enabled"] and not view["listening"] and view["problem"] == "Tailscale isn't connected"
    assert remote.settings(store) == {"enabled": True}
    cli(port, auth).put("/api/v1/settings/remote", json={"enabled": False})

    remote.set_settings(store, True)  # on, as a restart finds it
    monkeypatch.setattr(remote, "tailnet_status", lambda: net.me)
    restarted, rauth = build(store, free_port())
    thread = threading.Thread(target=lambda: asyncio.run(restarted.serve()), daemon=True)
    thread.start()
    try:
        deadline = time.time() + 5
        while not rauth.listen_ips and time.time() < deadline:
            time.sleep(0.02)
        assert rauth.listen_ips == frozenset({"::1"})
    finally:
        restarted.should_exit = True
        thread.join(10)


def test_home_screen_files_are_served_without_signing_in(service):
    _, port, _ = service
    with httpx.Client(base_url=base(port), timeout=5) as c:
        m = c.get("/manifest.webmanifest")
        assert m.status_code == 200 and m.json()["display"] == "standalone"
        assert m.headers["content-type"].startswith("application/manifest+json")
        for icon in ("/apple-touch-icon.png", "/icon-192.png", "/icon-512.png"):
            r = c.get(icon)
            assert r.status_code == 200 and r.headers["content-type"] == "image/png", icon


def test_remote_sessions_are_capped_and_the_macs_are_never_evicted():
    from robin.service.auth import REMOTE_SESSIONS_MAX, Auth

    auth = Auth(port=1, cli_token="t")
    mac = auth.exchange(auth.new_bootstrap())
    assert mac is not None
    first = auth.trusted_session()[0]
    newer = [auth.trusted_session()[0] for _ in range(REMOTE_SESSIONS_MAX)]
    assert sum(s.remote for s in auth._sessions.values()) == REMOTE_SESSIONS_MAX
    assert auth.browser_session(first) is None  # the oldest phone session went
    assert all(auth.browser_session(sid) for sid in newer)
    assert auth.browser_session(mac[0]) is not None


def test_off_keeps_the_macs_own_browser_signed_in(service, monkeypatch):
    _, port, auth = service
    Tailnet(monkeypatch)
    turn_on(port, auth)
    mac = browser(port, auth)
    with phone(port, host=f"mac:{port}") as c:
        c.get("/api/v1/auth/session")
    cli(port, auth).put("/api/v1/settings/remote", json={"enabled": False})
    assert mac.get("/api/v1/workspace").status_code == 200


def test_loopback_rules_hold_while_remote_is_on(service, monkeypatch):
    _, port, auth = service
    Tailnet(monkeypatch)
    turn_on(port, auth)
    with phone(port, host=f"mac:{port}") as c:
        sid = c.get("/api/v1/auth/session").cookies.get("robin_session")
    with httpx.Client(base_url=base(port), timeout=5) as local:
        r = local.get("/api/v1/auth/session", headers={"host": f"mac:{port}"})
        assert r.status_code == 403 and r.json()["error"]["code"] == "bad_host"
        r = local.get("/api/v1/auth/session", headers={"origin": f"http://mac:{port}"})
        assert r.status_code == 403 and r.json()["error"]["code"] == "bad_origin"
        local.cookies.set("robin_session", sid)
        r = local.get("/api/v1/auth/session")  # a phone's cookie doesn't hand out its CSRF token here
        assert r.status_code == 401 and "set-cookie" not in r.headers
