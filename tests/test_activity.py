"""The Map's activity projection: what each session and subagent is doing, from crafted transcripts."""

import json
from pathlib import Path

from sqlalchemy import select

from robin import activity
from robin.discovery import scan
from robin.store.models import SessionActivity
from test_context import FAKE_TOKEN, observed_session_with
from test_discovery import jl

SID = "aaaaaaaa-1111-2222-3333-444444444444"


def rows(store) -> dict[str, SessionActivity]:
    with store.reader.connect() as c:
        return {r.lane: r for r in c.execute(select(SessionActivity)).all()}


def claude_rec(kind, uuid, t, content, **extra):
    return {
        "type": kind,
        "sessionId": SID,
        "cwd": extra.pop("cwd", None),
        "uuid": uuid,
        "timestamp": f"2026-01-17T01:00:{t:02d}.000Z",
        "message": {"role": kind, "content": content},
        **extra,
    }


def tool_use(uuid, t, name, inp, tid=None):
    return claude_rec(
        "assistant", uuid, t, [{"type": "tool_use", "id": tid or f"tu-{uuid}", "name": name, "input": inp}]
    )


def claude_session(tmp_path: Path, cwd: Path, *records, subagents=None) -> dict[str, Path]:
    root = tmp_path / "claude"
    base = root / "-repo"
    jl(base / f"{SID}.jsonl", *records)
    for name, (meta, recs) in (subagents or {}).items():
        jl(base / SID / "subagents" / f"{name}.jsonl", *recs)
        if meta is not None:
            (base / SID / "subagents" / f"{name}.meta.json").write_text(json.dumps(meta))
    return {"claude": root, "codex": tmp_path / "none", "pi": tmp_path / "none"}


def test_claude_title_tool_files_and_todos(store, tmp_path):
    cwd = tmp_path / "repo"
    cwd.mkdir()
    roots = claude_session(
        tmp_path,
        cwd,
        claude_rec("user", "m0", 0, "<command-name>/clear</command-name>", cwd=str(cwd)),
        claude_rec("user", "m1", 1, "Caveat: injected", isMeta=True),
        claude_rec(
            "user",
            "m2",
            2,
            [
                {"type": "text", "text": "<system-reminder>context</system-reminder>"},
                {"type": "text", "text": "Add a   Map view\nto Robin"},
            ],
        ),
        tool_use(
            "a1",
            3,
            "TodoWrite",
            {
                "todos": [
                    {"content": "Plan it", "status": "completed"},
                    {"content": "Build it", "status": "in_progress"},
                    {"content": "Test it", "status": "pending"},
                ]
            },
        ),  # fmt: skip
        tool_use("a2", 4, "Edit", {"file_path": str(cwd / "web/src/App.tsx")}),
        tool_use("a3", 5, "Write", {"file_path": str(cwd / "README.md")}),
        tool_use("a4", 6, "Edit", {"file_path": str(cwd / "web/src/App.tsx")}),
        tool_use("a5", 7, "Bash", {"command": "npm test\n--watch=false"}),
    )
    scan(store, roots=roots)
    activity.catch_up(store)
    main = rows(store)[""]
    assert main.title == "Add a Map view to Robin"
    assert (main.tool, main.target) == ("Bash", "npm test")
    assert [f["path"] for f in json.loads(main.files)] == [
        "web/src/App.tsx",
        "README.md",
    ]  # distinct, newest first
    assert main.files_count == 2
    assert [(t["text"], t["status"]) for t in json.loads(main.todos)] == [
        ("Plan it", "completed"),
        ("Build it", "in_progress"),
        ("Test it", "pending"),
    ]
    assert (main.todo_done, main.todo_total) == (1, 3)
    assert main.turn_started_at.isoformat().startswith("2026-01-17T01:00:02")
    assert activity.lag(store) == 0


def test_task_create_and_update_use_the_real_task_id(store, tmp_path):
    cwd = tmp_path / "repo"
    cwd.mkdir()
    roots = claude_session(
        tmp_path,
        cwd,
        claude_rec("user", "m1", 1, 'Ship <pasted_content id="p1">a long paste', cwd=str(cwd)),
        tool_use("a1", 2, "TaskCreate", {"subject": "Draft notes"}, tid="tc1"),
        claude_rec(
            "user",
            "r1",
            3,
            [{"type": "tool_result", "tool_use_id": "tc1", "content": "Task #7 created successfully"}],
            toolUseResult={"task": {"id": "7", "subject": "Draft notes"}},
        ),
        tool_use("a2", 4, "TaskCreate", {"subject": "Publish"}, tid="tc2"),  # result never seen: numbered
        tool_use("a3", 5, "TaskUpdate", {"taskId": "7", "status": "completed"}),
        tool_use("a4", 6, "TaskCreate", {"subject": "Oops"}, tid="tc3"),
        tool_use("a5", 7, "TaskUpdate", {"taskId": "9", "status": "deleted"}),
    )
    scan(store, roots=roots)
    activity.catch_up(store)
    assert rows(store)[""].title == "Ship [pasted]"
    todos = json.loads(rows(store)[""].todos)
    assert [(t["id"], t["text"], t["status"]) for t in todos] == [
        ("7", "Draft notes", "completed"),
        ("8", "Publish", "pending"),
    ]


def test_subagents_get_their_own_lane(store, tmp_path):
    cwd = tmp_path / "repo"
    cwd.mkdir()
    roots = claude_session(
        tmp_path,
        cwd,
        claude_rec("user", "m1", 1, "Parent work", cwd=str(cwd)),
        subagents={
            "agent-abc": (
                {"agentType": "Explore", "description": "Find the event ingest"},
                [
                    claude_rec("user", "s1", 2, "You are exploring…", cwd=str(cwd)),
                    tool_use("s2", 3, "Grep", {"pattern": "payload_ref"}),
                    tool_use("s3", 4, "Edit", {"file_path": str(cwd / "notes.md")}),
                ],
            ),
            "agent-def": (None, [claude_rec("user", "s4", 5, "Review the diff", cwd=str(cwd))]),
        },
    )
    scan(store, roots=roots)
    activity.catch_up(store)
    got = rows(store)
    assert set(got) == {"", "agent-abc", "agent-def"}
    sub = got["agent-abc"]
    assert (sub.label, sub.title, sub.tool, sub.target) == (
        "Explore",
        "Find the event ingest",
        "Edit",
        "notes.md",
    )
    assert got["agent-def"].title == "Review the diff" and got["agent-def"].label is None
    assert got[""].files_count == 0  # a subagent's edits stay on its own lane


def test_codex_rollout(store, tmp_path):
    cwd = tmp_path / "repo"
    cwd.mkdir()
    tid = "019a0000-0000-7000-8000-0000000000ab"

    def rec(t, kind, payload):
        return {"timestamp": f"2026-10-17T01:00:{t:02d}Z", "type": kind, "payload": payload}

    jl(
        tmp_path / "codex" / "2026" / "10" / "17" / f"rollout-2026-10-17T01-00-00-{tid}.jsonl",
        rec(0, "session_meta", {"id": tid, "cwd": str(cwd), "cli_version": "0.170.0"}),
        rec(
            1,
            "response_item",
            {"type": "message", "role": "user", "content": [{"text": "<environment_context>"}]},
        ),
        rec(2, "event_msg", {"type": "user_message", "message": "Upgrade Vite"}),
        rec(3, "event_msg", {"type": "task_started"}),
        rec(
            4,
            "response_item",
            {
                "type": "function_call",
                "call_id": "c1",
                "name": "update_plan",
                "arguments": json.dumps(
                    {
                        "plan": [
                            {"step": "Bump", "status": "completed"},
                            {"step": "Fix config", "status": "in_progress"},
                        ]
                    }
                ),
            },
        ),  # fmt: skip
        rec(
            5,
            "response_item",
            {
                "type": "custom_tool_call",
                "call_id": "c2",
                "name": "apply_patch",
                "input": "*** Begin Patch\n*** Update File: vite.config.ts\n*** Add File: src/new.ts\n"
                "*** Update File: old.ts\n*** Move to: moved.ts\n*** End Patch",
            },
        ),  # fmt: skip
        rec(6, "event_msg", {"type": "patch_apply_end", "changes": {str(cwd / "package.json"): {}}}),
        rec(
            7,
            "response_item",
            {
                "type": "function_call",
                "call_id": "c3",
                "name": "exec_command",
                "arguments": json.dumps({"cmd": "npm run build"}),
            },
        ),  # fmt: skip
    )
    scan(store, roots={"claude": tmp_path / "none", "codex": tmp_path / "codex", "pi": tmp_path / "none"})
    activity.catch_up(store)
    main = rows(store)[""]
    assert main.title == "Upgrade Vite"
    assert (main.tool, main.target) == ("exec_command", "npm run build")
    assert [(t["text"], t["status"]) for t in json.loads(main.todos)] == [
        ("Bump", "completed"),
        ("Fix config", "in_progress"),
    ]
    assert [f["path"] for f in json.loads(main.files)] == [
        "package.json",
        "moved.ts",
        "old.ts",
        "src/new.ts",
        "vite.config.ts",
    ]


def test_host_events_and_hooks(store):
    sid = observed_session_with(
        store,
        [
            ("message.user", {"type": "user_message", "text": "Fix the flaky test"}),
            ("session.turn_started", {"type": "turn_started"}),
            ("tool.started", {"type": "tool_started", "tool": "shell", "input": {"command": "pytest -x"}}),
            ("file.change", {"type": "file_changed", "paths": ["/x/a.py", "/x/b.py"]}),
            ("unknown.raw", {"type": "plan_updated", "plan": []}),  # not a type the Map reads
            (
                "tool.started",
                {"hook": "PreToolUse", "tool_name": "Read", "tool_input": {"file_path": "/x/c.py"}},
            ),
            ("session.turn_started", {"hook": "UserPromptSubmit", "prompt": "and also the docs"}),
        ],
    )
    activity.catch_up(store)
    main = rows(store)[""]
    assert main.session_id == sid and main.title == "Fix the flaky test"
    assert (main.tool, main.target) == ("Read", "/x/c.py")
    assert [f["path"] for f in json.loads(main.files)] == ["/x/b.py", "/x/a.py"]


def test_plan_updates_from_managed_codex(store):
    observed_session_with(
        store,
        [
            (
                "session.plan_updated",
                {"type": "plan_updated", "plan": [{"step": "One", "status": "inProgress"}]},
            )
        ],
    )
    activity.catch_up(store)
    main = rows(store)[""]
    assert json.loads(main.todos) == [{"id": "1", "text": "One", "status": "in_progress"}]
    assert main.tool is None  # a plan isn't an action


def test_secrets_never_reach_the_table(store):
    observed_session_with(
        store,
        [
            ("message.user", {"type": "user_message", "text": f"use token {FAKE_TOKEN} please"}),
            (
                "tool.started",
                {
                    "type": "tool_started",
                    "tool": "TodoWrite",
                    "input": {"todos": [{"content": f"rotate {FAKE_TOKEN}", "status": "pending"}]},
                },
            ),
            (
                "tool.started",
                {"type": "tool_started", "tool": "shell", "input": {"command": f"echo {FAKE_TOKEN}"}},
            ),
            ("file.change", {"type": "file_changed", "paths": [f"/tmp/{FAKE_TOKEN}.txt"]}),
        ],
    )
    activity.catch_up(store)
    main = rows(store)[""]
    cells = [main.title, main.target, main.todos, main.files]
    assert all(FAKE_TOKEN not in c for c in cells)
    assert all("[REDACTED:github-pat]" in c for c in cells)


def test_limits_and_robustness(store):
    long = "x" * 500
    observed_session_with(
        store,
        [
            ("message.user", {"type": "user_message", "text": long}),
            (
                "tool.started",
                {
                    "type": "tool_started",
                    "tool": "TodoWrite",
                    "input": {"todos": [{"content": str(i), "status": "pending"} for i in range(80)]},
                },
            ),  # fmt: skip
            ("file.change", {"type": "file_changed", "paths": [f"/f{i}" for i in range(600)]}),
            ("tool.started", {"_robin_pruned": True}),
            ("tool.started", ["not", "a", "dict"]),
            ("tool.started", {"type": "tool_started", "tool": None, "input": 5}),
            ("tool.started", {"type": "assistant", "message": {"content": "no blocks"}}),
            ("tool.started", {"type": "response_item", "payload": None}),
        ],
    )
    activity.catch_up(store)
    main = rows(store)[""]
    assert len(main.title) == activity.TEXT_LIMIT and main.title.endswith("…")
    assert main.todo_total == activity.MAX_TODOS and main.files_count == activity.MAX_FILES


def test_incremental_and_rebuilt_when_rules_change(store):
    observed_session_with(store, [("message.user", {"type": "user_message", "text": "codename bluebird"})])
    assert activity.catch_up(store) > 0
    before = rows(store)[""]
    assert activity.catch_up(store) == 0  # nothing new
    assert rows(store)[""].title == before.title

    (store.data_dir / "redact.toml").write_text(
        '[[rules]]\nid = "codename"\ndescription = "codenames"\nregex = "bluebird"\n'
    )
    activity.catch_up(store)
    assert rows(store)[""].title == "codename [REDACTED:codename]"


def test_a_stale_watermark_applies_nothing(store, monkeypatch):
    observed_session_with(store, [("file.change", {"type": "file_changed", "paths": ["/a"]})])
    real = store.writer.call

    def race(fn):  # another pass moves the watermark between this batch's read and its write
        real(lambda c: activity._set_meta(c, activity.CURSOR_KEY, "999999"))
        return real(fn)

    monkeypatch.setattr(store.writer, "call", race)
    activity._fingerprint(store)
    real(lambda c: activity._set_meta(c, activity.RULES_KEY, activity._fingerprint(store)))
    assert activity.project_batch(store) == 0
    assert rows(store) == {}
    with store.reader.connect() as c:
        assert activity._meta(c, activity.CURSOR_KEY) == "999999"


def test_cluster_of(tmp_path):
    activity.cluster_of.cache_clear()
    repo = tmp_path / "repo"
    (repo / ".git" / "worktrees" / "wt1").mkdir(parents=True)
    (repo / "src" / "deep").mkdir(parents=True)
    wt = tmp_path / "wt1"
    wt.mkdir()
    (wt / ".git").write_text(f"gitdir: {repo / '.git' / 'worktrees' / 'wt1'}\n")
    plain = tmp_path / "plain"
    plain.mkdir()
    assert activity.cluster_of(str(repo / "src" / "deep")) == str(repo)
    assert activity.cluster_of(str(wt)) == str(repo)
    assert activity.cluster_of(str(plain)) == str(plain)
    assert activity.cluster_of(None) == ""


def test_new_lines_fold_onto_the_stored_lane(store, tmp_path):
    cwd = tmp_path / "repo"
    cwd.mkdir()
    roots = claude_session(tmp_path, cwd, claude_rec("user", "m1", 1, "First prompt", cwd=str(cwd)))
    scan(store, roots=roots)
    activity.catch_up(store)
    transcript = next((tmp_path / "claude").rglob(f"{SID}.jsonl"))
    with open(transcript, "ab") as f:
        f.write(json.dumps(tool_use("a1", 2, "Edit", {"file_path": str(cwd / "x.py")})).encode() + b"\n")
    scan(store, roots=roots)
    activity.catch_up(store)
    main = rows(store)[""]
    assert main.title == "First prompt" and [f["path"] for f in json.loads(main.files)] == ["x.py"]


def _summaries(store, sid):
    from robin.events.ingest import read_events

    evs = read_events(store, session_id=sid, limit=100)
    return [(e.event_type, *s) for e, s in zip(evs, activity.summaries(store, evs), strict=True)]


def test_event_summaries(store):
    approval = {"type": "approval_request", "tool": "Bash", "input": {"command": "make clean"}}
    sid = observed_session_with(
        store,
        [
            ("message.user", {"type": "user_message", "text": "fix the login test"}),
            (
                "message.user",
                {"type": "user", "message": {"content": "<system-reminder>x</system-reminder>"}},
            ),
            ("message.completed", {"type": "message_completed", "text": "\nDone.\nThree files changed."}),
            ("tool.started", {"type": "tool_started", "tool": "Bash", "input": {"command": "npm test"}}),
            ("tool.error", {"type": "tool_error", "tool": "Bash", "output": "1 failed\nmore"}),
            ("file.change", {"type": "file_changed", "paths": ["/a/x.py", "/a/y.py", "/a/z.py"]}),
            ("session.turn_completed", {"type": "turn_completed"}),
            ("attention.approval_requested", approval),
            ("unknown.raw", {"type": "whatever"}),
            ("tool.result", {"type": "tool_result", "output": "ok"}),
            ("message.user", {"type": "response_item", "payload": {"type": "message", "role": "user"}}),
            ("tool.started", {"_robin_pruned": True}),
        ],
    )
    assert _summaries(store, sid) == [
        ("message.user", "You: fix the login test", False),
        ("message.user", None, True),
        ("message.completed", "Claude: Done.", False),
        ("tool.started", "Bash npm test", False),
        ("tool.error", "Bash failed: 1 failed", False),
        ("file.change", "Changed z.py, y.py (+1)", False),
        ("session.turn_completed", "Turn completed", False),
        ("attention.approval_requested", "Approval requested: Bash make clean", False),
        ("unknown.raw", None, True),
        ("tool.result", None, True),
        ("message.user", None, True),
        ("tool.started", "Pruned to stay under the storage cap; no longer available.", False),
    ]


def test_summaries_are_redacted(store):
    long_line = "x" * 10 + FAKE_TOKEN + "y" * 6000
    approval = {"type": "approval_request", "tool": "Bash", "input": {"command": FAKE_TOKEN}}
    sid = observed_session_with(
        store,
        [
            ("message.user", {"type": "user_message", "text": f"token {FAKE_TOKEN}"}),
            ("message.completed", {"type": "message_completed", "text": long_line}),
            (
                "tool.started",
                {"type": "tool_started", "tool": "Bash", "input": {"command": f"echo {FAKE_TOKEN}"}},
            ),
            ("file.change", {"type": "file_changed", "paths": [f"/tmp/{FAKE_TOKEN}.txt"]}),
            ("attention.approval_requested", approval),
            ("session.failed", {"type": "turn_failed", "reason": f"bad {FAKE_TOKEN}"}),
        ],
    )
    texts = [t for _, t, _ in _summaries(store, sid)]
    assert all(t and FAKE_TOKEN not in t for t in texts)
    assert sum("[REDACTED:github-pat]" in t for t in texts) >= 5


def test_file_edits_are_credited_to_the_task_in_progress(store):
    def todos(*statuses):
        names = ("Plan", "Build", "Ship")
        return {"todos": [{"content": n, "status": s} for n, s in zip(names, statuses, strict=False)]}

    def edit(path):
        return ("tool.started", {"type": "tool_started", "tool": "Edit", "input": {"file_path": path}})

    observed_session_with(
        store,
        [
            edit("/r/early.py"),  # no task in progress yet: unattributed
            (
                "tool.started",
                {"type": "tool_started", "tool": "TodoWrite", "input": todos("in_progress", "pending")},
            ),
            edit("/r/plan.md"),
            (
                "tool.started",
                {"type": "tool_started", "tool": "TodoWrite", "input": todos("completed", "in_progress")},
            ),
            edit("/r/app.py"),
            edit("/r/app.py"),
            edit("/r/util.py"),
        ],
    )
    activity.catch_up(store)
    main = rows(store)[""]
    plan, build = json.loads(main.todos)
    assert [f["path"] for f in plan["files"]] == ["/r/plan.md"]  # kept across the TodoWrite rewrite
    assert [(f["path"], f["n"]) for f in build["files"]] == [("/r/util.py", 1), ("/r/app.py", 2)]
    assert [f["path"] for f in json.loads(main.files)] == [
        "/r/util.py",
        "/r/app.py",
        "/r/plan.md",
        "/r/early.py",
    ]
