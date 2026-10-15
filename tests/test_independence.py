"""Independent reviewers (FR-032, TR-064) with the scripted runtime; the real sandboxes are in tests/live."""

import os
import time

from robin.events.ingest import read_events, read_payload_json
from robin.runtime.manager import RuntimeManager
from test_findings import aid, launch, wait


def started_with(store, sid):
    """The env the runtime host saw, as the scripted runtime reports it on session_started."""
    for e in reversed(read_events(store, session_id=sid, limit=10_000)):
        if e.event_type == "session.started":
            return read_payload_json(store, e.global_cursor)
    return None


def test_an_independent_reviewer_is_launched_with_robins_store_denied(api):
    store, c, task_id, _ = api
    r = c.post(
        "/sessions/launch",
        json={"runtime": "fake", "task_id": task_id, "independent": True, "action_id": aid()},
    )
    sid = r.json()["session_id"]
    ev = wait(lambda: started_with(store, sid))
    assert ev["deny_read"] == os.path.realpath(store.data_dir)
    assert c.get(f"/sessions/{sid}").json()["independence"] == "enforced"

    ordinary = launch(c, task_id)
    assert wait(lambda: started_with(store, ordinary))["deny_read"] is None
    assert c.get(f"/sessions/{ordinary}").json()["independence"] is None


def test_a_reviewer_stays_isolated_after_handoff_and_reclaim(api):
    store, c, task_id, _ = api
    sid = c.post(
        "/sessions/launch",
        json={"runtime": "fake", "task_id": task_id, "independent": True, "action_id": aid()},
    ).json()["session_id"]
    wait(lambda: c.get(f"/sessions/{sid}").json()["runtime_state"] == "idle")
    h = aid()
    c.post(f"/sessions/{sid}/actions", json={"type": "handoff", "action_id": h})
    wait(lambda: c.get(f"/actions/{h}").json()["state"] == "acknowledged")
    r = aid()
    c.post(f"/sessions/{sid}/actions", json={"type": "reclaim", "action_id": r})
    wait(lambda: c.get(f"/actions/{r}").json()["state"] == "acknowledged")
    time.sleep(0.2)
    assert started_with(store, sid)["deny_read"] == os.path.realpath(store.data_dir)


def test_runtimes_without_a_sandbox_are_labelled_not_isolated(store):
    env: dict[str, str] = {}
    manager = RuntimeManager(store)
    assert manager.independence_env("pi", env) == "not_isolated" and env == {}
    assert manager.independence_env("claude", env) == "enforced"
    assert env == {"ROBIN_DENY_READ": os.path.realpath(store.data_dir)}
