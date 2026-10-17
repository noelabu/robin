"""Sessions launched with permission prompts skipped: an opt-in kept across relaunches (FR-012)."""

import time

from sqlalchemy import func, select, update

from robin import activity
from robin.adapters.claude.host import permission_mode
from robin.adapters.codex.host import approval_policy
from robin.runtime.manager import RuntimeManager
from robin.store.models import Session as SessionRow
from test_cli import run, served, session_id  # noqa: F401 - fixture
from test_findings import aid, launch, wait
from test_independence import started_with


def skipping(c, task_id, **extra):
    body = {"runtime": "fake", "task_id": task_id, "skip_permissions": True, "action_id": aid(), **extra}
    return c.post("/sessions/launch", json=body).json()["session_id"]


def test_a_session_launched_with_prompts_skipped_carries_it_to_the_host(api):
    store, c, task_id, _ = api
    sid = skipping(c, task_id)
    assert wait(lambda: started_with(store, sid))["skip_permissions"] == "1"
    assert c.get(f"/sessions/{sid}").json()["skip_permissions"] is True

    ordinary = launch(c, task_id)
    assert wait(lambda: started_with(store, ordinary))["skip_permissions"] is None
    assert c.get(f"/sessions/{ordinary}").json()["skip_permissions"] is False


def test_skipping_prompts_and_independence_go_together(api):
    store, c, task_id, _ = api
    sid = skipping(c, task_id, independent=True)
    ev = wait(lambda: started_with(store, sid))
    assert ev["skip_permissions"] == "1" and ev["deny_read"]
    assert c.get(f"/sessions/{sid}").json()["independence"] == "enforced"


def test_pi_is_refused_and_nothing_is_created(api):
    store, c, task_id, _ = api

    def count():
        with store.reader.connect() as conn:
            return conn.execute(select(func.count()).select_from(SessionRow)).scalar()

    before = count()
    r = c.post(
        "/sessions/launch",
        json={"runtime": "pi", "task_id": task_id, "skip_permissions": True, "action_id": aid()},
    )
    assert r.status_code == 422
    assert r.json()["error"]["message"] == "Pi has no permission prompts"
    assert count() == before


def test_it_survives_handoff_and_reclaim(api):
    store, c, task_id, _ = api
    sid = skipping(c, task_id)
    wait(lambda: c.get(f"/sessions/{sid}").json()["runtime_state"] == "idle")
    h = aid()
    c.post(f"/sessions/{sid}/actions", json={"type": "handoff", "action_id": h})
    wait(lambda: c.get(f"/actions/{h}").json()["state"] == "acknowledged")
    r = aid()
    c.post(f"/sessions/{sid}/actions", json={"type": "reclaim", "action_id": r})
    wait(lambda: c.get(f"/actions/{r}").json()["state"] == "acknowledged")
    time.sleep(0.2)
    assert started_with(store, sid)["skip_permissions"] == "1"


async def test_relaunch_env_follows_the_session(store):
    manager = RuntimeManager(store)
    row = {"native_session_id": "n1", "current_task_id": None, "independence": None, "runtime": "claude"}
    assert "ROBIN_SKIP_PERMISSIONS" not in await manager._relaunch_env("s1", {**row, "skip_permissions": 0})
    env = await manager._relaunch_env("s1", {**row, "skip_permissions": 1})
    assert env["ROBIN_SKIP_PERMISSIONS"] == "1"


def test_claude_permission_mode():
    assert permission_mode({}) == "default"
    assert permission_mode({"ROBIN_SKIP_PERMISSIONS": "1"}) == "bypassPermissions"
    assert permission_mode({"ROBIN_SKIP_PERMISSIONS": "yes"}) == "default"


def test_codex_approval_policy():
    assert approval_policy({}) is None
    assert approval_policy({"ROBIN_SKIP_PERMISSIONS": "1"}) == "never"
    # an explicit stricter override wins; anything looser than the user's default is ignored
    strict = {"ROBIN_SKIP_PERMISSIONS": "1", "ROBIN_CODEX_APPROVAL_POLICY": "untrusted"}
    assert approval_policy(strict) == "untrusted"
    assert approval_policy({"ROBIN_CODEX_APPROVAL_POLICY": "never"}) is None


def test_only_the_session_card_is_marked(api):
    store, c, task_id, _ = api
    sid = skipping(c, task_id)
    wait(lambda: c.get(f"/sessions/{sid}").json()["runtime_state"] == "idle")
    cards = {x["id"]: x for x in activity.map_snapshot(store)["cards"]}
    assert cards[sid]["skip_permissions"] is True
    store.writer.call(
        lambda conn: conn.execute(
            update(SessionRow).where(SessionRow.session_id == sid).values(skip_permissions=0)
        )
    )
    assert {x["id"]: x for x in activity.map_snapshot(store)["cards"]}[sid]["skip_permissions"] is False


def test_cli_run_skip_permissions(served, capsys):  # noqa: F811
    store, project = served
    d = str(store.data_dir)
    out = run(capsys, "run", "fake", "--cwd", str(project), "--skip-permissions", "--wait", "10", "--data", d)
    assert "permission prompts: skipped" in out
    sid = session_id(out)
    with store.reader.connect() as conn:
        assert conn.execute(select(SessionRow.skip_permissions).where(SessionRow.session_id == sid)).scalar()
