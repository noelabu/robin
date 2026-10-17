"""The Map's snapshot and its API: clusters, cards, subagent edges, the now card, caps and auth."""

import json
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import update

from robin import activity
from robin.discovery import scan
from robin.events.ingest import ensure_session
from robin.store.models import Session as SessionRow
from test_activity import claude_rec, claude_session, tool_use
from test_context import FAKE_TOKEN, observed_session_with


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


def test_session_rows_carry_title_cluster_and_last_activity(api):
    store, c, _, _ = api
    sid = observed_session_with(store, [("message.user", {"type": "user_message", "text": "name me"})])
    activity.catch_up(store)
    row = c.get(f"/sessions/{sid}").json()
    assert row["title"] == "name me" and row["last_activity_at"] and "cluster" in row
    listed = {s["session_id"]: s for s in c.get("/workspace").json()["sessions"]}
    assert listed[sid]["title"] == "name me"
    bare = ensure_session(store, runtime="pi", mode="observed", native_session_id="bare")
    assert c.get(f"/sessions/{bare}").json()["title"] is None


# -- skills, workflows and background shells -------------------------------------------------------------

T0 = datetime(2026, 1, 17, 1, 0, tzinfo=UTC)  # claude_rec's minute


def result(uuid, t, tid, tur, is_error=False):
    block = {"type": "tool_result", "tool_use_id": tid, "content": "ok", "is_error": is_error}
    return claude_rec("user", uuid, t, [block], toolUseResult=tur)


def note(uuid, t, status, summary, tid=None, task=None, event=None):
    parts = ["<task-notification>"]
    parts += [f"<task-id>{task}</task-id>"] if task else []
    parts += [f"<tool-use-id>{tid}</tool-use-id>"] if tid else []
    parts += [f"<status>{status}</status>"] if status else []
    parts += [f"<summary>{summary}</summary>"]
    parts += [f"<event>{event}</event>"] if event else []
    return claude_rec(
        "user", uuid, t, "\n".join([*parts, "</task-notification>"]), origin={"kind": "task-notification"}
    )


def bg(uuid, t, tid, command, description=None):
    return tool_use(
        uuid, t, "Bash", {"command": command, "description": description, "run_in_background": True}, tid
    )


def cards_at(store, monkeypatch, minutes):
    monkeypatch.setattr(activity, "_now", lambda: T0 + timedelta(minutes=minutes))
    (sid,) = {c["session_id"] for c in activity.map_snapshot(store)["cards"]}
    return sid, by_id(activity.map_cards(store, [sid]))


def ingest(store, tmp_path, *records, **kw):
    scan(
        store,
        roots=claude_session(
            tmp_path, tmp_path, claude_rec("user", "m0", 0, "Go", cwd=str(tmp_path)), *records, **kw
        ),
    )
    activity.catch_up(store)


def test_background_shells_take_their_status_from_notifications(store, tmp_path, monkeypatch):
    ingest(
        store,
        tmp_path,
        bg("a1", 1, "sh1", f"pytest -q --token {FAKE_TOKEN}", "Run the suite"),
        result("r1", 2, "sh1", {"backgroundTaskId": "b1"}),
        bg("a2", 3, "sh2", "npm run build"),
        result("r2", 4, "sh2", {"backgroundTaskId": "b2"}),
        bg("a3", 5, "sh3", "npm run dev", "Dev server"),
        result("r3", 6, "sh3", {"backgroundTaskId": "b3"}),
        note(
            "n1",
            7,
            "completed",
            'Background command "Run the suite" completed (exit code 0)',
            tid="sh1",
            task="b1",
        ),
        note("n2", 8, "failed", 'Background command "npm run build" failed with exit code 2', task="b2"),
        note(
            "n3",
            9,
            "stopped",
            "Background shell command didn't finish before the session ended. Task ids: b3.",
        ),
    )
    sid, cards = cards_at(store, monkeypatch, 2)
    suite, build, dev = cards[f"{sid}:sh1"], cards[f"{sid}:sh2"], cards[f"{sid}:sh3"]
    assert (suite["kind"], suite["parent"], suite["label"], suite["title"]) == (
        "shell",
        sid,
        "Background shell",
        "Run the suite",
    )
    assert (suite["status"], suite["state"], suite["state_evidence"]) == ("completed", "idle", "reported")
    assert suite["summary"].endswith("(exit code 0)") and FAKE_TOKEN not in json.dumps(cards)
    assert (build["status"], build["state"], build["title"]) == ("failed", "failed", "npm run build")
    assert (dev["status"], dev["state"]) == ("killed", "stopped")

    # Ten minutes on, the finished ones fold into a count on the session card; the failed one stays.
    sid, cards = cards_at(store, monkeypatch, 15)
    assert (
        f"{sid}:sh1" not in cards and f"{sid}:sh3" not in cards and cards[f"{sid}:sh2"]["status"] == "failed"
    )
    assert cards[sid]["shells_done"] == 2
    jobs = {j["ref"]: j for j in activity.session_activity(store, sid)["jobs"]}
    assert (jobs["sh1"]["hidden"], jobs["sh2"]["hidden"]) == ("folded", None)


def test_a_shell_with_no_end_signal_runs_while_the_session_is_active(store, tmp_path, monkeypatch):
    ingest(
        store, tmp_path, bg("a1", 1, "sh1", "npm run dev"), result("r1", 2, "sh1", {"backgroundTaskId": "b1"})
    )
    sid, cards = cards_at(store, monkeypatch, 5)
    assert (cards[f"{sid}:sh1"]["state"], cards[f"{sid}:sh1"]["state_evidence"]) == ("running", "inferred")
    sid, cards = cards_at(store, monkeypatch, 60)
    assert (
        f"{sid}:sh1" not in cards and cards[sid]["shells_done"] == 1
    )  # quiet for an hour: idle, then folded


def test_monitor_events_and_task_stop(store, tmp_path, monkeypatch):
    monitor = (
        tool_use("a1", 1, "Monitor", {"description": "Watch the deploy", "command": "tail -f log"}, "mo1"),
        result("r1", 2, "mo1", {"taskId": "m9"}),
        note("n1", 3, None, "Monitor event: 250/1833", task="m9", event="250/1833"),
    )
    ingest(store, tmp_path, *monitor)
    sid, cards = cards_at(store, monkeypatch, 1)
    mon = cards[f"{sid}:mo1"]
    assert (mon["kind"], mon["label"], mon["summary"], mon["status"]) == (
        "shell",
        "Monitor",
        "250/1833",
        None,
    )

    ingest(store, tmp_path, *monitor, tool_use("a2", 4, "TaskStop", {"task_id": "m9"}))  # the file grew
    sid, cards = cards_at(store, monkeypatch, 1)
    assert cards[f"{sid}:mo1"]["status"] == "killed"


def journal_run():
    return [
        {"type": "launched"},
        {"type": "started", "agentId": "w1", "label": "find:correctness", "phase": "Find"},
        {"type": "started", "agentId": "w2", "label": "find:security", "phase": "Find"},
        {"type": "started", "agentId": "w3", "label": "verify:1", "phase": "Verify"},
        {"type": "result", "agentId": "w1", "result": {"findings": []}},
        {"type": "failed", "agentId": "w2"},
    ]


def wf_agent(name, phase, t):
    return {
        "agentType": f"{name}-reviewer",
        "description": f"{phase.lower()}:{name}",
        "workflowPhase": phase,
    }, [claude_rec("user", f"{name}-u", t, "Review", cwd=None)]


def test_a_skill_chain_is_one_card_with_its_workflow_agents_and_shells_under_it(store, tmp_path, monkeypatch):
    ingest(
        store,
        tmp_path,
        tool_use("a1", 1, "Skill", {"skill": "review-lite", "args": "the diff"}, "sk1"),
        result("r1", 2, "sk1", {"success": True, "commandName": "review-lite"}),
        tool_use("a2", 3, "Skill", {"skill": "code-reviewer", "args": "depth=lite"}, "sk2"),
        tool_use("a3", 4, "Workflow", {"scriptPath": "/x/review-pipeline.js"}, "wf1"),
        result("r3", 5, "wf1", {"taskId": "t1", "runId": "wf_1", "workflowName": "review-pipeline"}),
        tool_use("a4", 6, "Agent", {"subagent_type": "Explore", "description": "Look", "prompt": "p"}, "ag1"),
        result("r4", 7, "ag1", {"status": "async_launched", "agentId": "x1"}),
        bg("a5", 8, "sh1", "uv run pytest"),
        note("n1", 9, "completed", 'Agent "Look" finished', tid="ag1", task="x1"),
        claude_rec("user", "m2", 10, "Now something else"),
        tool_use(
            "a6", 11, "Agent", {"subagent_type": "Plan", "description": "Plan it", "prompt": "p"}, "ag2"
        ),
        subagents={
            "agent-x1": (
                {"agentType": "Explore", "description": "Look", "toolUseId": "ag1"},
                [claude_rec("user", "x1u", 8, "Look")],
            ),
            "agent-x2": (
                {"agentType": "Plan", "description": "Plan it", "toolUseId": "ag2"},
                [claude_rec("user", "x2u", 12, "Plan")],
            ),
        },
        workflows={
            "wf_1": (
                journal_run(),
                {
                    "agent-w1": wf_agent("correctness", "Find", 5),
                    "agent-w2": wf_agent("security", "Find", 5),
                    "agent-w3": wf_agent("verify", "Verify", 6),
                },
            )
        },
    )
    sid, cards = cards_at(store, monkeypatch, 1)
    skill, wf = cards[f"{sid}:sk1"], cards[f"{sid}:wf1"]
    assert f"{sid}:sk2" not in cards
    assert (skill["kind"], skill["title"], skill["inner"], skill["parent"]) == (
        "skill",
        "review-lite",
        ["code-reviewer"],
        sid,
    )
    assert (wf["kind"], wf["title"], wf["parent"]) == ("workflow", "review-pipeline", skill["id"])
    assert wf["phases"] == [
        {"name": "Find", "done": 1, "failed": 1, "total": 2},
        {"name": "Verify", "done": 0, "failed": 0, "total": 1},
    ]
    w1, w2, w3 = cards[f"{sid}:agent-w1"], cards[f"{sid}:agent-w2"], cards[f"{sid}:agent-w3"]
    assert {w1["parent"], w2["parent"], w3["parent"]} == {wf["id"]}
    assert (w1["status"], w2["status"], w3["status"], w1["phase"]) == ("completed", "failed", None, "Find")
    assert wf["state"] == "failed" and skill["state"] == "failed"  # a failed agent shows up the chain
    assert (
        cards[f"{sid}:agent-x1"]["parent"] == skill["id"]
        and cards[f"{sid}:agent-x1"]["status"] == "completed"
    )
    assert cards[f"{sid}:sh1"]["parent"] == skill["id"]
    assert cards[f"{sid}:agent-x2"]["parent"] == sid  # after the next prompt: not the skill's


def test_a_finished_skill_folds_with_everything_under_it(store, tmp_path, monkeypatch):
    ingest(
        store,
        tmp_path,
        tool_use("a1", 1, "Skill", {"skill": "build"}, "sk1"),
        tool_use("a2", 2, "Agent", {"subagent_type": "Plan", "description": "Plan", "prompt": "p"}, "ag1"),
        result(
            "r2", 3, "ag1", {"status": "completed", "agentId": "x1"}
        ),  # a sync agent: its result is its end
        subagents={
            "agent-x1": ({"agentType": "Plan", "toolUseId": "ag1"}, [claude_rec("user", "x1u", 2, "Plan")])
        },
    )
    sid, cards = cards_at(store, monkeypatch, 5)
    assert cards[f"{sid}:sk1"]["status"] == "completed" and cards[f"{sid}:agent-x1"]["status"] == "completed"
    sid, cards = cards_at(store, monkeypatch, 45)
    assert set(cards) == {sid} and cards[sid]["groups_done"] == 1


def test_a_slash_command_skill_opens_a_group(store, tmp_path, monkeypatch):
    ingest(
        store,
        tmp_path,
        claude_rec("user", "c1", 1, "<command-name>/clear</command-name>"),
        claude_rec(
            "user", "c2", 2, "<command-message>fix</command-message>\n<command-name>/fix</command-name>"
        ),
        claude_rec("user", "c3", 3, "Base directory for this skill: /skills/fix", isMeta=True),
        tool_use(
            "a1", 4, "Agent", {"subagent_type": "sherlock", "description": "Find it", "prompt": "p"}, "ag1"
        ),
        subagents={
            "agent-x1": (
                {"agentType": "sherlock", "toolUseId": "ag1"},
                [claude_rec("user", "x1u", 5, "Find")],
            )
        },
    )
    sid, cards = cards_at(store, monkeypatch, 1)
    (skill,) = [c for c in cards.values() if c["kind"] == "skill"]
    assert skill["title"] == "fix" and cards[f"{sid}:agent-x1"]["parent"] == skill["id"]


def test_notifications_seen_only_in_queue_records_still_count(store, tmp_path, monkeypatch):
    text = (
        "<task-notification>\n<tool-use-id>{}</tool-use-id>\n<status>completed</status>\n"
        "<summary>done</summary>\n</task-notification>"
    )
    ingest(
        store,
        tmp_path,
        tool_use("a1", 1, "Agent", {"subagent_type": "Explore", "prompt": "p"}, "ag1"),
        result("r1", 2, "ag1", {"status": "async_launched", "agentId": "x1"}),
        bg("a2", 3, "sh1", "make"),
        {
            "type": "queue-operation",
            "operation": "enqueue",
            "timestamp": "2026-01-17T01:00:05.000Z",
            "content": text.format("ag1"),
        },
        {
            "type": "attachment",
            "uuid": "at1",
            "timestamp": "2026-01-17T01:00:06.000Z",
            "attachment": {"type": "queued_command", "prompt": text.format("sh1")},
        },
        subagents={
            "agent-x1": ({"agentType": "Explore", "toolUseId": "ag1"}, [claude_rec("user", "x1u", 4, "Look")])
        },
    )
    sid, cards = cards_at(store, monkeypatch, 1)
    assert cards[f"{sid}:agent-x1"]["status"] == "completed" and cards[f"{sid}:sh1"]["status"] == "completed"


def test_subagent_status_from_meta_and_resumes(store, tmp_path, monkeypatch):
    ingest(
        store,
        tmp_path,
        tool_use("a1", 1, "Agent", {"subagent_type": "Plan", "prompt": "p"}, "ag1"),
        result("r1", 2, "ag1", {"status": "async_launched", "agentId": "x1"}),
        tool_use("a2", 3, "Agent", {"subagent_type": "Plan", "prompt": "p"}, "ag2"),
        result("r2", 4, "ag2", {"status": "async_launched", "agentId": "x2"}),
        note("n2", 5, "completed", "done", tid="ag2"),
        subagents={
            "agent-x1": (
                {"agentType": "Plan", "toolUseId": "ag1", "finished": True},
                [claude_rec("user", "x1u", 3, "P")],
            ),
            "agent-x2": (
                {"agentType": "Plan", "toolUseId": "ag2"},
                [claude_rec("user", "x2u", 4, "P"), claude_rec("user", "x2v", 40, "More")],
            ),
        },
    )
    sid, cards = cards_at(store, monkeypatch, 0.75)
    assert cards[f"{sid}:agent-x1"]["status"] == "completed"
    x2 = cards[f"{sid}:agent-x2"]
    assert (x2["status"], x2["state"]) == (None, "running")  # messaged again after it finished


def test_a_workflow_caps_its_agents(store, tmp_path, monkeypatch):
    n = activity.SUBAGENTS_SHOWN + 3
    journal = [{"type": "started", "agentId": f"w{i}", "phase": "Find"} for i in range(n)]
    ingest(
        store,
        tmp_path,
        tool_use("a1", 1, "Workflow", {"name": "fanout"}, "wf1"),
        result("r1", 2, "wf1", {"runId": "wf_9"}),
        workflows={"wf_9": (journal, {f"agent-w{i}": wf_agent(f"a{i}", "Find", 3) for i in range(n)})},
    )
    sid, cards = cards_at(store, monkeypatch, 1)
    assert cards[f"{sid}:wf1"]["subagents_hidden"] == 3
    assert sum(c["parent"] == f"{sid}:wf1" for c in cards.values()) == activity.SUBAGENTS_SHOWN
