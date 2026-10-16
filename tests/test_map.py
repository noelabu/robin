"""The Map's snapshot and its API: clusters, cards, subagent edges, the now card, caps and auth."""

import httpx
from sqlalchemy import update

from robin import activity
from robin.discovery import scan
from robin.events.ingest import ensure_session
from robin.store.models import Session as SessionRow
from test_activity import claude_rec, claude_session, tool_use
from test_context import observed_session_with


def by_id(snapshot):
    return {c["id"]: c for c in snapshot["cards"]}


def test_snapshot_clusters_cards_and_subagent_edges(store, tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "web").mkdir()
    activity.cluster_of.cache_clear()
    roots = claude_session(
        tmp_path,
        repo,
        claude_rec("user", "m1", 1, "Build the map", cwd=str(repo / "web")),
        tool_use(
            "a1", 2, "TodoWrite", {"todos": [{"content": "a", "status": "completed"}, {"content": "b"}]}
        ),
        tool_use("a2", 3, "Edit", {"file_path": str(repo / "web" / "App.tsx")}),
        subagents={
            "agent-x": (
                {"agentType": "Explore", "description": "Look around"},
                [
                    claude_rec("user", "s1", 4, "go", cwd=str(repo)),
                    tool_use("s2", 5, "Grep", {"pattern": "needle"}),
                ],
            )
        },  # fmt: skip
    )
    scan(store, roots=roots)
    other = ensure_session(
        store, runtime="pi", mode="observed", native_session_id="p1", execution_cwd=str(tmp_path)
    )
    activity.catch_up(store)

    snap = activity.map_snapshot(store)
    cards = by_id(snap)
    (sid,) = [c["session_id"] for c in snap["cards"] if c["runtime"] == "claude" and not c["parent"]]
    main, sub = cards[sid], cards[f"{sid}:agent-x"]
    assert main["cluster"] == str(repo)  # a subfolder resolves to its repository
    assert main["title"] == "Build the map" and main["todos"] == {"done": 1, "total": 2}
    assert (main["files"], main["last_file"], main["tool"], main["target"]) == (
        1,
        "App.tsx",
        "Edit",
        "App.tsx",
    )
    assert main["state"] == "idle" and main["state_evidence"] == "inferred"  # transcript times are old
    assert sub["parent"] == sid and sub["label"] == "Explore" and sub["title"] == "Look around"
    pi = cards[other]
    assert pi["detail"] is False and pi["todos"] is None and pi["cluster"] == str(tmp_path)
    assert {c["key"]: c["sessions"] for c in snap["clusters"]} == {str(repo): 1, str(tmp_path): 1}
    assert snap["clusters"][0]["label"] in ("repo", tmp_path.name)
    assert snap["now"]["card_id"] == f"{sid}:agent-x" and snap["now"]["tool"] == "Grep"  # the latest action
    assert snap["pending_events"] == 0


def test_states_archived_and_caps(store, tmp_path):
    sid = observed_session_with(store, [("message.user", {"type": "user_message", "text": "fresh work"})])
    activity.catch_up(store)
    assert by_id(activity.map_snapshot(store))[sid]["state"] == "running"  # activity seconds ago, inferred

    store.writer.call(
        lambda c: c.execute(
            update(SessionRow).where(SessionRow.session_id == sid).values(runtime_state="waiting_for_input")
        )
    )
    card = by_id(activity.map_snapshot(store))[sid]
    assert (card["state"], card["state_evidence"]) == ("waiting_for_input", "reported")

    for i in range(5):
        ensure_session(
            store, runtime="codex", mode="observed", native_session_id=f"c{i}", execution_cwd=str(tmp_path)
        )
    capped = activity.map_snapshot(store, per_cluster=2)
    (cl,) = [c for c in capped["clusters"] if c["key"] == str(tmp_path)]
    assert (cl["sessions"], cl["shown"]) == (5, 2)
    expanded = activity.map_snapshot(store, per_cluster=2, cluster=str(tmp_path))
    assert len(expanded["cards"]) == 5 and [c["key"] for c in expanded["clusters"]] == [str(tmp_path)]

    store.writer.call(
        lambda c: c.execute(update(SessionRow).where(SessionRow.session_id == sid).values(archived=True))
    )
    assert sid not in by_id(activity.map_snapshot(store))


def test_subagents_shown_are_capped(store, tmp_path):
    subs = {
        f"agent-{i}": (None, [claude_rec("user", f"s{i}", i, f"sub {i}", cwd=str(tmp_path))])
        for i in range(activity.SUBAGENTS_SHOWN + 3)
    }
    scan(
        store,
        roots=claude_session(
            tmp_path, tmp_path, claude_rec("user", "m", 0, "p", cwd=str(tmp_path)), subagents=subs
        ),
    )
    activity.catch_up(store)
    snap = activity.map_snapshot(store)
    (main,) = [c for c in snap["cards"] if not c["parent"]]
    assert main["subagents_hidden"] == 3
    shown = [c["title"] for c in snap["cards"] if c["parent"]]
    assert (
        len(shown) == activity.SUBAGENTS_SHOWN and shown[0] == f"sub {activity.SUBAGENTS_SHOWN + 2}"
    )  # newest


def test_session_activity_detail(store, tmp_path):
    scan(
        store,
        roots=claude_session(
            tmp_path,
            tmp_path,
            claude_rec("user", "m", 0, "p", cwd=str(tmp_path)),
            tool_use("a", 1, "Write", {"file_path": str(tmp_path / "x.md")}),
        ),
    )
    activity.catch_up(store)
    (sid,) = [c["session_id"] for c in activity.map_snapshot(store)["cards"]]
    detail = activity.session_activity(store, sid)
    (lane,) = detail["lanes"]
    assert (
        lane["current"]["tool"] == "Write"
        and lane["files"][0]["path"] == "x.md"
        and lane["files"][0]["op"] == "write"
    )
    assert activity.session_activity(store, "nope") is None


def test_map_api(api):
    store, c, _, project = api
    sid = observed_session_with(
        store, [("tool.started", {"type": "tool_started", "tool": "shell", "input": {"command": "make"}})]
    )
    snap = c.get("/map").json()
    assert by_id(snap)[sid]["tool"] == "shell"  # caught up on request
    got = c.get(f"/map/cards?session_ids={sid},unknown").json()
    assert [x["id"] for x in got["cards"]] == [sid] and got["now"]["session_id"] == sid
    detail = c.get(f"/sessions/{sid}/activity").json()
    assert detail["lanes"][0]["current"] == {
        "tool": "shell",
        "target": "make",
        "at": detail["lanes"][0]["current"]["at"],
    }
    assert c.get("/sessions/nope/activity").status_code == 404
    assert c.get("/map?per_cluster=0").status_code == 422
    anon = httpx.get(str(c.base_url).rstrip("/") + "/map")
    assert anon.status_code == 401


def test_the_service_derives_activity_in_the_background(api):
    import time

    store, c, _, _ = api
    sid = observed_session_with(
        store, [("message.user", {"type": "user_message", "text": "background please"})]
    )
    deadline = time.time() + 10
    while time.time() < deadline:  # no /map call: only the service's own loop can fill this
        lanes = c.get(f"/sessions/{sid}/activity").json()["lanes"]
        if lanes:
            break
        time.sleep(0.1)
    assert lanes and lanes[0]["title"] == "background please"
