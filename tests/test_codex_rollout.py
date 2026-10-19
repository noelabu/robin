"""Codex rollouts as the CLI writes them today (0.163): items, subagent threads, replayed history."""

import json
import os
from pathlib import Path

from sqlalchemy import select, text

from robin import activity, search, watch
from robin.adapters.history import HISTORY
from robin.discovery import scan
from robin.events.ingest import read_events
from robin.store.models import Session as SessionRow
from robin.store.models import SessionActivity, SourceCheckpoint
from test_discovery import jl

ROOT = "01a10000-0000-7000-8000-00000000000a"
CHILD = "01a10000-0000-7000-8000-00000000000b"
GRAND = "01a10000-0000-7000-8000-00000000000c"
AGENTS_MD = "# AGENTS.md instructions\n\n<INSTRUCTIONS>\nUse the code-reviewer agent.\n</INSTRUCTIONS>"


def rec(t, kind, payload):
    return {"timestamp": f"2026-10-19T01:00:{t:02d}Z", "type": kind, "payload": payload}


def meta(t, tid, cwd, parent=None, agent_path=None, role=None, depth=1):
    payload = {"id": tid, "session_id": tid, "cwd": str(cwd), "cli_version": "0.163.0", "source": "vscode"}
    if parent:
        spawn = {
            "parent_thread_id": parent,
            "depth": depth,
            "agent_path": agent_path,
            "agent_nickname": "Tesla",
            "agent_role": role,
        }
        payload.update(
            parent_thread_id=parent,
            source={"subagent": {"thread_spawn": spawn}},
            agent_role=role,
            agent_path=agent_path,
        )
    return rec(t, "session_meta", payload)


def item(t, thread, body):
    return rec(
        t, "event_msg", {"type": "item_completed", "thread_id": thread, "turn_id": "turn", "item": body}
    )


def user_message(t, thread, words):
    return item(
        t, thread, {"type": "UserMessage", "id": f"u{t}", "content": [{"type": "text", "text": words}]}
    )


def command(t, thread, cmd):
    return item(
        t,
        thread,
        {
            "type": "CommandExecution",
            "id": f"exec-{t}",
            "command": ["/bin/zsh", "-lc", cmd],
            "status": "completed",
        },
    )


def spawn(t, call_id, task_name, agent_type):
    args = {"task_name": task_name, "agent_type": agent_type, "message": "gAAAAABencrypted"}
    return rec(
        t,
        "response_item",
        {"type": "function_call", "name": "spawn_agent", "call_id": call_id, "arguments": json.dumps(args)},
    )


def activity_item(t, thread, call_id, kind, agent, agent_path):
    return item(
        t,
        thread,
        {
            "type": "SubAgentActivity",
            "id": call_id,
            "kind": kind,
            "agent_thread_id": agent,
            "agent_path": agent_path,
        },
    )


def rollout(tmp_path: Path, tid: str, *records, minute: int = 0, tail: bytes = b"") -> Path:
    name = f"rollout-2026-10-19T01-{minute:02d}-00-{tid}.jsonl"
    return jl(tmp_path / "codex" / "2026" / "10" / "19" / name, *records, tail=tail)


def roots(tmp_path):
    return {"claude": tmp_path / "none", "codex": tmp_path / "codex", "pi": tmp_path / "none"}


def lanes(store, sid=None) -> dict[str, SessionActivity]:
    with store.reader.connect() as c:
        q = select(SessionActivity)
        if sid:
            q = q.where(SessionActivity.session_id == sid)
        return {r.lane: r for r in c.execute(q).all()}


def codex_sessions(store, archived=False):
    with store.reader.connect() as c:
        return dict(
            c.execute(
                select(SessionRow.native_session_id, SessionRow.session_id).where(
                    SessionRow.runtime == "codex", SessionRow.archived.is_(archived)
                )
            ).all()
        )


def test_title_comes_from_the_user_message_item_not_the_injected_agents_md(store, tmp_path):
    cwd = tmp_path / "repo"
    rollout(
        tmp_path,
        ROOT,
        meta(0, ROOT, cwd),
        rec(
            1,
            "response_item",
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": AGENTS_MD}]},
        ),
        user_message(2, ROOT, "Review the resume flow"),
    )
    scan(store, roots=roots(tmp_path))
    activity.catch_up(store)
    assert lanes(store)[""].title == "Review the resume flow"
    (sid,) = codex_sessions(store).values()
    assert [m["text"] for m in activity.conversation(store, sid) if m["role"] == "user"] == [
        "Review the resume flow"
    ]


def test_exec_script_reports_the_shell_command_it_ran(store, tmp_path):
    cwd = tmp_path / "repo"
    rollout(
        tmp_path,
        ROOT,
        meta(0, ROOT, cwd),
        rec(
            1,
            "response_item",
            {
                "type": "custom_tool_call",
                "call_id": "c1",
                "name": "exec",
                "input": 'text(await tools.exec_command({cmd:"git diff --stat","max_output_tokens":2500}));',
            },
        ),
        command(2, ROOT, "git diff --stat"),
    )
    scan(store, roots=roots(tmp_path))
    activity.catch_up(store)
    main = lanes(store)[""]
    assert (main.tool, main.target) == ("shell", "git diff --stat")


def test_update_plan_inside_an_exec_script_becomes_the_todo_list(store, tmp_path):
    cwd = tmp_path / "repo"
    script = (
        "const p = await tools.update_plan({plan:[\n"
        '  {step:"Read the \\"spec\\"",status:"completed"},\n'
        '  {step:"Fix it",status:"in_progress"}\n'
        "]});\ntext(p);"
    )
    rollout(
        tmp_path,
        ROOT,
        meta(0, ROOT, cwd),
        rec(
            1, "response_item", {"type": "custom_tool_call", "call_id": "c1", "name": "exec", "input": script}
        ),
    )
    scan(store, roots=roots(tmp_path))
    activity.catch_up(store)
    todos = json.loads(lanes(store)[""].todos)
    assert [(t["text"], t["status"]) for t in todos] == [
        ('Read the "spec"', "completed"),
        ("Fix it", "in_progress"),
    ]


def test_file_change_items_record_what_was_written_and_edited(store, tmp_path):
    cwd = tmp_path / "repo"
    rollout(
        tmp_path,
        ROOT,
        meta(0, ROOT, cwd),
        item(
            1,
            ROOT,
            {
                "type": "FileChange",
                "id": "exec-1",
                "changes": {
                    str(cwd / "b.md"): {"type": "add", "content": "# B\n"},
                    str(cwd / "a.py"): {"type": "update", "unified_diff": "@@ -1 +1 @@\n-x\n+y\n"},
                },
            },
        ),
    )
    scan(store, roots=roots(tmp_path))
    activity.catch_up(store)
    files = json.loads(lanes(store)[""].files)
    assert sorted((f["path"], f["op"]) for f in files) == [("a.py", "edit"), ("b.md", "write")]


def tree(tmp_path, cwd, child_done=False):
    """A root that spawned a reviewer, which spawned a security reviewer. Each subagent file replays its
    parent's history before its own starts (at thread_settings_applied for its own thread)."""
    root = rollout(
        tmp_path,
        ROOT,
        meta(0, ROOT, cwd),
        rec(
            1,
            "response_item",
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": AGENTS_MD}]},
        ),
        user_message(2, ROOT, "Review the branch"),
        spawn(3, "call-1", "review", "code-reviewer"),
        activity_item(4, ROOT, "call-1", "started", CHILD, "/root/review"),
        *(
            [activity_item(20, ROOT, "subagent-completed-t1", "completed", CHILD, "/root/review")]
            if child_done
            else []
        ),
    )
    child = rollout(
        tmp_path,
        CHILD,
        meta(5, CHILD, cwd, ROOT, "/root/review", "code-reviewer"),
        meta(5, ROOT, cwd),
        rec(5, "event_msg", {"type": "task_started", "turn_id": "turn"}),
        rec(
            5,
            "response_item",
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "Review the branch"}],
            },
        ),
        rec(5, "event_msg", {"type": "thread_settings_applied", "thread_id": CHILD}),
        spawn(6, "call-2", "security", "security-reviewer"),
        activity_item(7, CHILD, "call-2", "started", GRAND, "/root/review/security"),
        activity_item(8, CHILD, "call-9", "interacted", ROOT, "/root"),
        command(9, CHILD, "rg TODO"),
        activity_item(15, CHILD, "subagent-completed-t2", "completed", GRAND, "/root/review/security"),
        minute=1,
    )
    grand = rollout(
        tmp_path,
        GRAND,
        meta(10, GRAND, cwd, CHILD, "/root/review/security", "security-reviewer", depth=2),
        meta(10, CHILD, cwd, ROOT, "/root/review", "code-reviewer"),
        rec(10, "event_msg", {"type": "thread_settings_applied", "thread_id": GRAND}),
        command(11, GRAND, "git log -1"),
        minute=2,
    )
    return root, child, grand


def test_subagent_threads_are_lanes_of_one_root_session(store, tmp_path):
    cwd = tmp_path / "repo"
    tree(tmp_path, cwd)
    (report,) = [r for r in scan(store, roots=roots(tmp_path)) if r.runtime == "codex"]
    assert (report.found, report.new_sessions, report.errors) == (1, 1, [])
    activity.catch_up(store)
    sessions = codex_sessions(store)
    assert list(sessions) == [ROOT]
    sid = sessions[ROOT]
    got = lanes(store, sid)
    assert set(got) == {"", f"codex-{CHILD}", f"codex-{GRAND}"}
    assert got[""].title == "Review the branch"
    child, grand = got[f"codex-{CHILD}"], got[f"codex-{GRAND}"]
    assert (child.label, child.title, child.tool, child.target) == (
        "code-reviewer",
        "review",
        "shell",
        "rg TODO",
    )
    assert (grand.label, grand.title, grand.target) == ("security-reviewer", "security", "git log -1")

    cards = {c["ref"]: c for c in activity.map_cards(store, [sid])["cards"]}
    assert cards[""]["title"] == "Review the branch"
    subs = [c for c in cards.values() if c["kind"] == "subagent"]
    assert {c["ref"] for c in subs} == {f"codex-{CHILD}", f"codex-{GRAND}"}
    assert all(c["parent"] == sid for c in subs)
    assert cards[f"codex-{GRAND}"]["status"] == "completed"  # its spawn and end are in the child's file
    assert cards[f"codex-{CHILD}"]["status"] is None

    convo = activity.conversation(store, sid)
    assert [m["text"] for m in convo if m["role"] == "user"] == ["Review the branch"]
    shown = " ".join(" ".join(m.get("items", [])) for m in convo)
    assert "rg TODO" not in shown and "git log" not in shown


def test_a_finished_child_reads_completed(store, tmp_path):
    tree(tmp_path, tmp_path / "repo", child_done=True)
    scan(store, roots=roots(tmp_path))
    activity.catch_up(store)
    sid = codex_sessions(store)[ROOT]
    cards = {c["ref"]: c for c in activity.map_cards(store, [sid])["cards"]}
    assert cards[f"codex-{CHILD}"]["status"] == "completed"


def test_watcher_follows_a_new_subagent_rollout_into_its_root_session(store, tmp_path):
    cwd = tmp_path / "repo"
    root, child, grand = tree(tmp_path, cwd)
    full = grand.read_bytes()
    child_bytes = child.read_bytes()
    child.unlink()
    grand.unlink()
    w = watch.HistoryWatcher(store, roots={"codex": tmp_path / "codex"})
    w.cycle()
    sid = codex_sessions(store)[ROOT]

    child.write_bytes(child_bytes)
    assert HISTORY["codex"].main(child) == root
    w.cycle()
    assert codex_sessions(store) == {ROOT: sid}
    src = "file:" + os.path.realpath(child)
    assert any(e.source_id == src for e in read_events(store, session_id=sid))

    grand.write_bytes(full[:200])  # the first line isn't complete yet: nothing to identify it by
    c = w.cycle()
    assert not [e for r in c.reports for e in r.errors]
    assert codex_sessions(store) == {ROOT: sid}
    grand.write_bytes(full)
    w.cycle()
    assert codex_sessions(store) == {ROOT: sid}
    src = "file:" + os.path.realpath(grand)
    assert any(e.source_id == src for e in read_events(store, session_id=sid))


def test_a_subagent_ingested_on_its_own_moves_under_its_root(store, tmp_path):
    cwd = tmp_path / "repo"
    root, child, grand = tree(tmp_path, cwd)
    root_bytes = root.read_bytes()
    root.unlink()
    grand.unlink()
    scan(store, roots=roots(tmp_path))  # the root isn't there yet: the child stands alone
    activity.catch_up(store)
    search.index_events(store)
    orphan = codex_sessions(store)[CHILD]
    src = "file:" + os.path.realpath(child)

    root.write_bytes(root_bytes)
    scan(store, roots=roots(tmp_path))
    activity.catch_up(store)
    sessions = codex_sessions(store)
    assert list(sessions) == [ROOT]
    sid = sessions[ROOT]
    assert codex_sessions(store, archived=True) == {CHILD: orphan}
    assert not read_events(store, session_id=orphan)
    assert any(e.source_id == src for e in read_events(store, session_id=sid))
    with store.reader.connect() as c:
        assert set(
            c.execute(select(SourceCheckpoint.session_id).where(SourceCheckpoint.source_id == src)).scalars()
        ) == {sid}
        indexed = c.execute(
            text(
                "SELECT DISTINCT session_id FROM search_index WHERE kind = 'event' AND rowid IN "
                "(SELECT global_cursor FROM event WHERE source_id = :s)"
            ),
            {"s": src},
        ).scalars()
        assert set(indexed) == {sid}
    got = lanes(store, sid)
    assert set(got) == {"", f"codex-{CHILD}"}
    assert got[f"codex-{CHILD}"].target == "rg TODO"
    assert not lanes(store, orphan)
