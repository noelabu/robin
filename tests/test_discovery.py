"""Discovery and history ingestion against crafted stores in each runtime's format (never real history)."""

import json
from pathlib import Path

from sqlalchemy import select

from robin.adapters.history import claude_discover, codex_discover, pi_discover
from robin.discovery import scan
from robin.events.ingest import read_events
from robin.store.models import Session as SessionRow
from robin.workspace import tasks as ws


def jl(path: Path, *records, tail: bytes = b"") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"".join(json.dumps(r).encode() + b"\n" for r in records) + tail)
    return path


def claude_store(root: Path, cwd: str) -> Path:
    sid = "11111111-2222-3333-4444-555555555555"
    base = root / "-Users-me-app"
    jl(
        base / f"{sid}.jsonl",
        {
            "type": "user",
            "sessionId": sid,
            "cwd": cwd,
            "version": "2.1.300",
            "uuid": "u1",
            "timestamp": "2026-10-15T01:00:00.000Z",
            "message": {"role": "user", "content": "Review the PRD"},
        },
        {
            "type": "assistant",
            "sessionId": sid,
            "uuid": "a1",
            "timestamp": "2026-10-15T01:00:05.000Z",
            "message": {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "t1", "name": "Read", "input": {}}],
            },
        },
        {
            "type": "user",
            "sessionId": sid,
            "uuid": "u2",
            "timestamp": "2026-10-15T01:00:06.000Z",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "..."}],
            },
        },
        {
            "type": "assistant",
            "sessionId": sid,
            "uuid": "a2",
            "timestamp": "2026-10-15T01:00:09.000Z",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "Done."}]},
        },
        {"type": "brand-new-record-type", "timestamp": "2026-10-15T01:00:10.000Z"},
    )
    jl(
        base / sid / "subagents" / "agent-1.jsonl",
        {"type": "launched", "timestamp": "2026-10-15T01:00:01.000Z"},
        {"type": "result", "timestamp": "2026-10-15T01:00:04.000Z"},
    )
    return base / f"{sid}.jsonl"


def codex_store(root: Path, cwd: str) -> Path:
    tid = "019a0000-0000-7000-8000-000000000001"
    return jl(
        root / "2026" / "10" / "15" / f"rollout-2026-10-15T01-00-00-{tid}.jsonl",
        {
            "timestamp": "2026-10-15T01:00:00Z",
            "type": "session_meta",
            "payload": {"id": tid, "cwd": cwd, "cli_version": "0.162.1"},
        },
        {"timestamp": "2026-10-15T01:00:01Z", "type": "event_msg", "payload": {"type": "task_started"}},
        {
            "timestamp": "2026-10-15T01:00:02Z",
            "type": "response_item",
            "payload": {"type": "function_call", "call_id": "c1", "name": "shell"},
        },
        {"timestamp": "2026-10-15T01:00:03Z", "type": "event_msg", "payload": {"type": "agent_message"}},
        {"timestamp": "2026-10-15T01:00:04Z", "type": "event_msg", "payload": {"type": "task_complete"}},
    )


def pi_store(root: Path, cwd: str) -> Path:
    pid = "01a1aaaa-bbbb-7ccc-8ddd-eeeeeeeeeeee"
    return jl(
        root / "--Users-me-app--" / f"2026-10-15T01-00-00-000Z_{pid}.jsonl",
        {"type": "session", "version": 3, "id": pid, "timestamp": "2026-10-15T01:00:00.000Z", "cwd": cwd},
        {
            "type": "message",
            "id": "m1",
            "timestamp": "2026-10-15T01:00:01.000Z",
            "message": {"role": "user", "content": "hi"},
        },
        {
            "type": "message",
            "id": "m2",
            "timestamp": "2026-10-15T01:00:02.000Z",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "hello"}]},
        },
        {"type": "model_change", "timestamp": "2026-10-15T01:00:03.000Z"},
    )


def roots(tmp_path, cwd):
    r = {"claude": tmp_path / "claude", "codex": tmp_path / "codex", "pi": tmp_path / "pi"}
    claude_store(r["claude"], cwd)
    codex_store(r["codex"], cwd)
    pi_store(r["pi"], cwd)
    return r


def test_discoverers_identify_sessions_without_reading_whole_files(tmp_path):
    r = roots(tmp_path, "/Users/me/app")
    (c,) = claude_discover(r["claude"])
    assert c.native_id == "11111111-2222-3333-4444-555555555555" and c.cwd == "/Users/me/app"
    assert c.version == "2.1.300" and [p.name for p in c.extra_files] == ["agent-1.jsonl"]
    (x,) = codex_discover(r["codex"])
    assert x.native_id == "019a0000-0000-7000-8000-000000000001" and x.version == "0.162.1"
    (p,) = pi_discover(r["pi"])
    assert p.native_id == "01a1aaaa-bbbb-7ccc-8ddd-eeeeeeeeeeee" and p.version == "3"


def test_scan_ingests_typed_history_and_is_idempotent(store, tmp_path):
    project = store.data_dir.parent / "app"
    project.mkdir()
    proj = ws.register_project(store, str(project))
    r = roots(tmp_path, str(project.resolve()))
    reports = {x.runtime: x for x in scan(store, roots=r)}
    assert {k: (v.found, v.new_sessions, v.errors) for k, v in reports.items()} == {
        "claude": (1, 1, []),
        "codex": (1, 1, []),
        "pi": (1, 1, []),
    }
    assert reports["claude"].events == 7  # 5 transcript records + 2 subagent records
    types = {e.runtime: [] for e in read_events(store, limit=1000)}
    for e in read_events(store, limit=1000):
        types[e.runtime].append(e.event_type)
    assert types["claude"][:4] == ["message.user", "tool.started", "tool.result", "message.completed"]
    assert "unknown.raw" in types["claude"]  # unknown record types are kept, not dropped
    assert {"session.started", "session.turn_started", "tool.started", "session.turn_completed"} <= set(
        types["codex"]
    )
    assert types["pi"][:3] == ["session.started", "message.user", "message.completed"]
    with store.reader.connect() as c:
        rows = c.execute(select(SessionRow.mode, SessionRow.project_id, SessionRow.native_session_id)).all()
    assert all(m == "observed" and p == proj["project_id"] and n for m, p, n in rows)

    again = {x.runtime: x for x in scan(store, roots=r)}
    assert all(v.new_sessions == 0 and v.events == 0 for v in again.values())  # idempotent
    jl(r["pi"] / "--Users-me-app--" / "x.jsonl")  # an empty extra file is harmless
    with open(next(r["pi"].rglob("*_01a1*.jsonl")), "ab") as f:
        f.write(
            json.dumps(
                {"type": "message", "id": "m3", "message": {"role": "user", "content": "more"}}
            ).encode()
            + b"\n"
        )
    assert {x.runtime: x for x in scan(store, roots=r)}["pi"].events == 1  # only the new record


def test_bad_files_are_reported_and_others_still_load(store, tmp_path):
    r = roots(tmp_path, "/Users/me/app")
    jl(
        r["codex"] / "2026" / "10" / "15" / "rollout-2026-10-15T02-00-00-019a-broken.jsonl",
        tail=b'{"timestamp": "x", "type": "session_meta", "payload": {"id": "broken"}}\n{not json}\n',
    )
    unreadable = jl(r["pi"] / "--Users-me-other--" / "2026_bad.jsonl", {"type": "session", "id": "pi-bad"})
    unreadable.chmod(0)
    try:
        reports = {x.runtime: x for x in scan(store, roots=r)}
    finally:
        unreadable.chmod(0o600)
    assert reports["codex"].found == 2 and reports["codex"].quarantined == 1  # malformed line quarantined
    assert reports["pi"].found == 2 and len(reports["pi"].errors) == 1  # unreadable file reported
    assert reports["claude"].errors == [] and reports["claude"].events == 7  # other runtimes unaffected


def test_a_failing_discoverer_does_not_stop_other_runtimes(store, tmp_path, monkeypatch):
    from robin.adapters import history

    r = roots(tmp_path, "/Users/me/app")

    def boom(root):
        raise RuntimeError("format changed")
        yield

    monkeypatch.setitem(
        history.HISTORY,
        "codex",
        history.HistoryAdapter(
            "codex",
            lambda: r["codex"],
            boom,
            history.codex_mapper,
            history.codex_files,
            history.codex_describe,
        ),
    )
    reports = {x.runtime: x for x in scan(store, roots=r)}
    assert reports["codex"].errors and "format changed" in reports["codex"].errors[0]
    assert reports["claude"].found == 1 and reports["pi"].found == 1


def test_sessions_robin_launched_are_not_double_ingested(store, tmp_path):
    from robin.events.ingest import ensure_session

    r = roots(tmp_path, "/Users/me/app")
    ensure_session(
        store, runtime="codex", mode="managed", native_session_id="019a0000-0000-7000-8000-000000000001"
    )
    reports = {x.runtime: x for x in scan(store, roots=r)}
    assert reports["codex"].skipped_managed == 1 and reports["codex"].events == 0


def test_claude_folder_is_found_after_leading_metadata_records(store, tmp_path):
    """Current transcripts open with metadata records that carry the session ID but no folder or version."""
    sid = "99999999-2222-3333-4444-555555555555"
    root = tmp_path / "claude"
    meta = [
        {"type": "last-prompt", "leafUuid": "x", "sessionId": sid},
        {"type": "mode", "mode": "normal", "sessionId": sid},
        {"type": "permission-mode", "permissionMode": "default", "sessionId": sid},
    ]
    snapshots = [{"type": "file-history-snapshot", "messageId": f"s{i}"} for i in range(22)]
    first = {
        "type": "user",
        "sessionId": sid,
        "cwd": "/Users/me/app",
        "version": "2.1.300",
        "uuid": "u1",
        "timestamp": "2026-10-15T01:00:00.000Z",
        "message": {"role": "user", "content": "hi"},
    }
    jl(root / "-Users-me-app" / f"{sid}.jsonl", *meta, *snapshots, first)
    (c,) = claude_discover(root)
    assert (c.native_id, c.cwd, c.version) == (sid, "/Users/me/app", "2.1.300")

    # A session indexed before its folder could be read gets it on the next scan.
    scan(store, runtimes=["claude"], roots={"claude": root})
    store.writer.call(lambda conn: conn.execute(SessionRow.__table__.update().values(execution_cwd=None)))
    scan(store, runtimes=["claude"], roots={"claude": root})
    with store.reader.connect() as conn:
        assert conn.execute(select(SessionRow.execution_cwd)).scalars().all() == ["/Users/me/app"]
