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
    assert all("preview" in e and e["session_id"] == a for e in latest)
    for sid in (a, b):
        c.post(f"/api/v1/sessions/{sid}/actions", json={"type": "stop", "action_id": aid()})
    # an archived or stopped session no longer counts
    c.patch(f"/api/v1/sessions/{b}", json={"archived": True})
    assert c.get(f"/api/v1/sessions/{a}").json()["shared_folder_with"] == []
