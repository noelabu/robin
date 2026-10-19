"""Continuing a session in another runtime: readers, writers, preview and continue."""

import asyncio
import dataclasses
import json
import os
import time
from pathlib import Path

import pytest
from sqlalchemy import func, select

from robin import discovery
from robin.adapters import history
from robin.continuation import RUNTIMES, flow, paths, readers, size, writers
from robin.continuation.canonical import Thinking, ToolCall, ToolResult, Transcript, turns
from robin.runtime.manager import _recently_written
from robin.store.models import AuditEntry, Event, SessionLink
from robin.store.models import Session as SessionRow
from robin.watch import HistoryWatcher
from test_handoff import settled
from test_manager import connect, session_row

T0 = "2026-10-18T01:00:00.000Z"


def jsonl(path: Path, recs: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in recs))
    return path


def claude_records(native: str, cwd: str) -> list[dict]:
    base = {"sessionId": native, "cwd": cwd, "timestamp": T0, "isSidechain": False}
    return [
        {"type": "permission-mode", "sessionId": native},
        {**base, "type": "user", "uuid": "u1", "message": {"role": "user", "content": "Fix the login bug"}},
        {
            **base,
            "type": "assistant",
            "uuid": "a1",
            "message": {
                "id": "m1",
                "role": "assistant",
                "model": "claude-opus-5-5",
                "content": [{"type": "thinking", "thinking": "Check the handler", "signature": "sig"}],
            },
        },
        {
            **base,
            "type": "assistant",
            "uuid": "a2",
            "message": {
                "id": "m1",
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "login.py"}}
                ],
            },
        },
        {
            **base,
            "type": "user",
            "uuid": "u2",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "content": [{"type": "text", "text": "def login(): ..."}],
                    }
                ],
            },
        },
        {
            **base,
            "type": "user",
            "uuid": "u3",
            "message": {"role": "user", "content": "<system-reminder>todo list</system-reminder>"},
        },
        {
            **base,
            "type": "user",
            "uuid": "s1",
            "isSidechain": True,
            "message": {"role": "user", "content": "subagent"},
        },
        {
            **base,
            "type": "assistant",
            "uuid": "a3",
            "message": {
                "id": "m2",
                "role": "assistant",
                "content": [{"type": "text", "text": "Fixed it."}, {"type": "image", "source": {}}],
            },
        },
        {**base, "type": "user", "uuid": "u4", "message": {"role": "user", "content": "Now add a test"}},
        {
            **base,
            "type": "assistant",
            "uuid": "a4",
            "message": {
                "id": "m3",
                "role": "assistant",
                "content": [{"type": "text", "text": "Added test_login."}],
            },
        },
    ]


def codex_records(native: str, cwd: str) -> list[dict]:
    def item(payload: dict) -> dict:
        return {"timestamp": T0, "type": "response_item", "payload": payload}

    return [
        {
            "timestamp": T0,
            "type": "session_meta",
            "payload": {"id": native, "cwd": cwd, "cli_version": "0.163.0"},
        },
        {"timestamp": T0, "type": "turn_context", "payload": {"model": "gpt-6-astra"}},
        item({"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "rules"}]}),
        item(
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "<environment_context>x"}],
            }
        ),
        item(
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "Fix the login bug"}],
            }
        ),
        item({"type": "reasoning", "summary": [{"type": "summary_text", "text": "Check the handler"}]}),
        item({"type": "reasoning", "summary": [], "encrypted_content": "zzz"}),
        item(
            {
                "type": "function_call",
                "name": "exec_command",
                "arguments": '{"cmd": "cat login.py"}',
                "call_id": "c1",
            }
        ),
        item({"type": "function_call_output", "call_id": "c1", "output": "def login(): ..."}),
        item(
            {"type": "custom_tool_call", "name": "apply_patch", "input": "*** Begin Patch", "call_id": "c2"}
        ),
        item(
            {
                "type": "custom_tool_call_output",
                "call_id": "c2",
                "output": [{"type": "input_text", "text": "Done!"}],
            }
        ),
        item(
            {
                "type": "agent_message",
                "author": "worker",
                "recipient": "main",
                "content": [{"type": "input_text", "text": "tests pass"}],
            }
        ),
        item(
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Fixed it."}],
            }
        ),
        {"timestamp": T0, "type": "event_msg", "payload": {"type": "agent_message", "message": "Fixed it."}},
        item({"type": "web_search_call", "status": "completed"}),
    ]


def pi_records(native: str, cwd: str) -> list[dict]:
    return [
        {"type": "session", "version": 3, "id": native, "timestamp": T0, "cwd": cwd},
        {
            "type": "model_change",
            "id": "e0",
            "parentId": None,
            "timestamp": T0,
            "provider": "openrouter",
            "modelId": "kimi-k3",
        },
        {
            "type": "message",
            "id": "e1",
            "parentId": "e0",
            "timestamp": T0,
            "message": {"role": "system", "content": "sys"},
        },
        {
            "type": "message",
            "id": "e2",
            "parentId": "e1",
            "timestamp": T0,
            "message": {"role": "user", "content": [{"type": "text", "text": "Fix the login bug"}]},
        },
        {
            "type": "message",
            "id": "e3",
            "parentId": "e2",
            "timestamp": T0,
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "Check the handler"},
                    {"type": "toolCall", "id": "p1", "name": "read", "arguments": {"path": "login.py"}},
                ],
            },
        },
        {
            "type": "message",
            "id": "e4",
            "parentId": "e3",
            "timestamp": T0,
            "message": {
                "role": "toolResult",
                "toolCallId": "p1",
                "toolName": "read",
                "content": [{"type": "text", "text": "def login(): ..."}],
                "isError": False,
            },
        },
        {
            "type": "message",
            "id": "e5",
            "parentId": "e4",
            "timestamp": T0,
            "message": {"role": "assistant", "content": [{"type": "text", "text": "Fixed it."}]},
        },
    ]


SOURCES = {"claude": claude_records, "codex": codex_records, "pi": pi_records}


def source_file(tmp_path: Path, runtime: str, native: str = "src-1", cwd: str = "/w/app") -> Path:
    return jsonl(tmp_path / f"{runtime}.jsonl", SOURCES[runtime](native, cwd))


# -- readers ----------------------------------------------------------------------


def test_claude_reader_merges_split_replies_splits_results_and_counts_what_it_drops(tmp_path):
    t = readers.read_claude(source_file(tmp_path, "claude"))
    assert (t.native_id, t.cwd, t.model) == ("src-1", "/w/app", "claude-opus-5-5")
    assert [m.role for m in t.msgs] == ["user", "assistant", "tool", "assistant", "user", "assistant"]
    assert t.msgs[1].blocks == [
        Thinking("Check the handler"),
        ToolCall("t1", "Read", {"file_path": "login.py"}),
    ]
    assert t.msgs[2].blocks == [ToolResult("t1", "def login(): ...")]
    assert t.dropped == {"system_reminders": 1, "sidechain_records": 1, "images": 1}


def test_codex_reader_reads_custom_tools_and_agent_messages_and_skips_injected_context(tmp_path):
    t = readers.read_codex(source_file(tmp_path, "codex"))
    assert (t.native_id, t.model) == ("src-1", "gpt-6-astra")
    calls = [b for m in t.msgs for b in m.blocks if isinstance(b, ToolCall)]
    assert calls == [
        ToolCall("c1", "exec_command", {"cmd": "cat login.py"}),
        ToolCall("c2", "apply_patch", "*** Begin Patch"),
    ]
    results = [b.content for m in t.msgs for b in m.blocks if isinstance(b, ToolResult)]
    assert results == ["def login(): ...", "Done!"]
    assert sum(1 for m in t.msgs if m.role == "user") == 1  # the event_msg copy isn't read twice
    assert t.dropped == {"system_reminders": 2, "encrypted_reasoning": 1, "unknown_items": 1}


def test_pi_reader(tmp_path):
    t = readers.read_pi(source_file(tmp_path, "pi"))
    assert (t.native_id, t.model) == ("src-1", "kimi-k3")
    assert [m.role for m in t.msgs] == ["user", "assistant", "tool", "assistant"]
    assert t.dropped == {"system_reminders": 1}


def test_a_compacted_claude_session_reads_as_its_summary_and_the_messages_it_kept(tmp_path):
    base = {"sessionId": "s", "cwd": "/w", "timestamp": T0}
    recs = [
        {**base, "type": "user", "uuid": "old", "message": {"role": "user", "content": "old question"}},
        {
            **base,
            "type": "assistant",
            "uuid": "olda",
            "message": {"id": "x", "role": "assistant", "content": "old answer"},
        },
        {**base, "type": "user", "uuid": "kept", "message": {"role": "user", "content": "kept question"}},
        {
            **base,
            "type": "system",
            "subtype": "compact_boundary",
            "compactMetadata": {"preservedSegment": {"headUuid": "kept"}},
        },
        {
            **base,
            "type": "user",
            "uuid": "sum",
            "isCompactSummary": True,
            "message": {"role": "user", "content": "SUMMARY"},
        },
        {
            **base,
            "type": "assistant",
            "uuid": "new",
            "message": {"id": "y", "role": "assistant", "content": "new answer"},
        },
    ]
    t = readers.read_claude(jsonl(tmp_path / "c.jsonl", recs))
    texts = [b.text for m in t.msgs for b in m.blocks]
    assert t.compacted and "SUMMARY" in texts[0] and texts[1:] == ["kept question", "new answer"]
    assert t.dropped["before_compaction"] == 2


def test_compacted_codex_and_pi_sessions_start_from_their_summary(tmp_path):
    codex = readers.read_codex(
        jsonl(
            tmp_path / "x.jsonl",
            [
                {"timestamp": T0, "type": "session_meta", "payload": {"id": "s", "cwd": "/w"}},
                {
                    "timestamp": T0,
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "old"}],
                    },
                },
                {
                    "timestamp": T0,
                    "type": "compacted",
                    "payload": {"message": "CODEX SUMMARY", "replacement_history": []},
                },
                {
                    "timestamp": T0,
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "new"}],
                    },
                },
            ],
        )
    )
    assert "CODEX SUMMARY" in codex.msgs[0].blocks[0].text and codex.msgs[1].blocks[0].text == "new"
    pi = readers.read_pi(
        jsonl(
            tmp_path / "p.jsonl",
            [
                {"type": "session", "version": 3, "id": "s", "cwd": "/w"},
                {"type": "message", "id": "e1", "message": {"role": "user", "content": "old"}},
                {"type": "message", "id": "e2", "message": {"role": "user", "content": "kept"}},
                {"type": "compaction", "summary": "PI SUMMARY", "firstKeptEntryId": "e2"},
            ],
        )
    )
    assert "PI SUMMARY" in pi.msgs[0].blocks[0].text and [m.blocks[0].text for m in pi.msgs[1:]] == ["kept"]


# -- writers ----------------------------------------------------------------------


@pytest.mark.parametrize(("source", "target"), [(s, t) for s in RUNTIMES for t in RUNTIMES if s != t])
def test_every_direction_round_trips_as_tagged_text(tmp_path, monkeypatch, source, target):
    monkeypatch.setenv(f"ROBIN_HISTORY_{target.upper()}", str(tmp_path / "store"))
    monkeypatch.delenv("ROBIN_PI_SESSION_DIR", raising=False)
    t = readers.read(source, source_file(tmp_path, source))
    r = writers.render(t, target, "/w/app")
    back = readers.read(target, writers_place(r))
    text = "\n".join(b.text for m in back.msgs for b in m.blocks)
    assert back.native_id == r.native_id and back.cwd == "/w/app"
    assert "Fix the login bug" in text and "Fixed it." in text
    assert "[external_agent_tool_call:" in text and "[external_agent_tool_result]" in text
    assert not [b for m in back.msgs for b in m.blocks if isinstance(b, (ToolCall, ToolResult, Thinking))]
    roles = [m.role for m in back.msgs]
    assert roles[0] == "user" and roles[-1] == "assistant"
    if target in ("claude", "pi"):
        assert all(a != b for a, b in zip(roles, roles[1:], strict=False))  # alternating


def writers_place(r: writers.Rendered) -> Path:
    os.replace(writers.stage(r), r.path)
    return r.path


def test_writer_shapes(tmp_path, monkeypatch):
    for rt in RUNTIMES:
        monkeypatch.setenv(f"ROBIN_HISTORY_{rt.upper()}", str(tmp_path / rt))
    monkeypatch.delenv("ROBIN_PI_SESSION_DIR", raising=False)
    t = readers.read_claude(source_file(tmp_path, "claude"))
    cwd = "/Users/me/my_app.v2"
    claude = writers.render(t, "claude", cwd)
    assert claude.path.parent.name == "-Users-me-my-app-v2"
    recs = [json.loads(x) for x in claude.data.splitlines()]
    assert recs[0]["parentUuid"] is None and all(
        b["parentUuid"] == a["uuid"] for a, b in zip(recs, recs[1:], strict=False)
    )
    assert all("version" not in r and r["sessionId"] == claude.native_id for r in recs)
    codex = writers.render(readers.read_pi(source_file(tmp_path, "pi")), "codex", cwd)
    assert codex.native_id[14] == "7" and codex.path.name.endswith(f"-{codex.native_id}.jsonl")
    crecs = [json.loads(x) for x in codex.data.splitlines()]
    assert crecs[0]["type"] == "session_meta" and {r["type"] for r in crecs[1:]} == {"response_item"}
    assert [r["ordinal"] for r in crecs] == list(range(len(crecs)))
    pi = writers.render(t, "pi", cwd)
    assert pi.path.parent.name == "--Users-me-my_app.v2--"
    prec = [json.loads(x) for x in pi.data.splitlines()]
    assert all(
        r["message"]["usage"]["totalTokens"] == 0
        for r in prec
        if r.get("message", {}).get("role") == "assistant"
    )
    monkeypatch.setenv("ROBIN_PI_SESSION_DIR", str(tmp_path / "flat"))
    assert writers.render(t, "pi", cwd).path.parent == tmp_path / "flat"


def test_turns_start_with_the_user_and_end_with_a_reply():
    t = Transcript("codex", "s", "/w", None, [])
    out = turns(t.msgs, "codex")
    assert [r for r, _, _ in out] == ["user", "assistant"]


def test_size_reads_each_runtimes_own_config(tmp_path):
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "config.toml").write_text('model = "m1"\n')
    (tmp_path / ".codex" / "models_cache.json").write_text(
        json.dumps({"models": [{"slug": "m1", "context_window": 500_000}]})
    )
    assert size.window("codex", tmp_path) == size.Window("m1", 500_000, "models_cache.json")
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text(json.dumps({"model": "opus[1m]"}))
    assert size.window("claude", tmp_path).tokens == 1_000_000
    assert size.window("pi", tmp_path) == size.Window(None, 200_000, "default")
    assert size.limit(size.Window(None, 100_000, "x")) == 60_000


# -- preview and continue -----------------------------------------------------------


@pytest.fixture
def fake_target(tmp_path, monkeypatch):
    """A 'fake' target that writes Pi's format and runs Robin's scripted fake host."""
    roots = {rt: tmp_path / f"{rt}-store" for rt in (*RUNTIMES, "fake")}
    for rt, root in roots.items():
        monkeypatch.setenv(f"ROBIN_HISTORY_{rt.upper()}", str(root))
    monkeypatch.delenv("ROBIN_PI_SESSION_DIR", raising=False)
    as_fake = lambda n: dataclasses.replace(n, runtime="fake")  # noqa: E731
    pi = history.HISTORY["pi"]
    monkeypatch.setitem(
        history.HISTORY,
        "fake",
        dataclasses.replace(
            pi,
            runtime="fake",
            home_root=lambda: roots["fake"],
            describe=lambda p: as_fake(history.pi_describe(p)),
            discover=lambda root: map(as_fake, history.pi_discover(root)),
        ),
    )
    monkeypatch.setattr(flow, "RUNTIMES", (*RUNTIMES, "fake"))
    monkeypatch.setitem(writers.RENDERERS, "fake", writers.render_pi)
    monkeypatch.setitem(readers.READERS, "fake", readers.read_pi)
    monkeypatch.setitem(size.WINDOWS, "fake", lambda home: size.Window("fake-model", 200_000, "test"))
    return roots


def claude_source(store, roots, tmp_path, native="src-1"):
    """An observed Claude session, indexed and quiet for ten minutes."""
    cwd = tmp_path / "proj"
    cwd.mkdir(exist_ok=True)
    path = jsonl(
        roots["claude"] / paths.claude_folder(str(cwd)) / f"{native}.jsonl", claude_records(native, str(cwd))
    )
    discovery.scan(store, ["claude"])
    old = time.time() - 600
    os.utime(path, (old, old))
    with store.reader.connect() as c:
        sid = c.execute(
            select(SessionRow.session_id).where(SessionRow.native_session_id == native)
        ).scalar_one()
    return sid, path


def written(roots) -> list[Path]:
    return sorted(roots["fake"].rglob("*.jsonl"))


def count(store, model, *where):
    with store.reader.connect() as c:
        return c.execute(select(func.count()).select_from(model).where(*where)).scalar()


def test_preview_says_what_crosses_and_writes_nothing(store, fake_target, tmp_path):
    sid, _ = claude_source(store, fake_target, tmp_path)
    p = flow.preview(store, sid, "fake")
    assert p["refusal"] is None and p["estimate"]["fits"]
    assert p["messages"] == {"total": 6, "user": 2, "assistant": 3, "tool_calls": 1, "tool_results": 1}
    assert p["dropped"]["images"] == 1 and p["dropped"]["tool_calls_as_text"] == 1
    assert p["target"]["model"] == "fake-model" and p["default_text"] == flow.DEFAULT_TEXT
    assert p["preview_hash"] == flow.preview(store, sid, "fake")["preview_hash"]
    assert written(fake_target) == []


def test_preview_refusals(store, fake_target, tmp_path, monkeypatch):
    sid, path = claude_source(store, fake_target, tmp_path)
    assert flow.preview(store, sid, "claude")["refusal"]["code"] == "same_runtime"
    monkeypatch.setitem(size.WINDOWS, "fake", lambda home: size.Window("tiny", 100, "test"))
    refusal = flow.preview(store, sid, "fake")["refusal"]
    assert refusal["code"] == "too_large" and "Compaction isn't supported yet" in refusal["message"]
    os.utime(path)  # written just now: a terminal may still have it open
    assert flow.preview(store, sid, "fake")["refusal"]["code"] == "source_busy"
    path.unlink()
    assert flow.preview(store, sid, "fake")["refusal"]["code"] == "no_transcript"


def test_a_managed_source_with_a_turn_running_is_refused(store, fake_target, tmp_path):
    from sqlalchemy import update

    sid, _ = claude_source(store, fake_target, tmp_path)
    store.writer.call(
        lambda c: c.execute(
            update(SessionRow)
            .where(SessionRow.session_id == sid)
            .values(mode="managed", runtime_state="running")
        )
    )
    assert flow.preview(store, sid, "fake")["refusal"]["code"] == "source_busy"


async def test_continue_writes_links_and_takes_over_in_the_target(supervised, fake_target, tmp_path):
    store = supervised
    sid, src_path = claude_source(store, fake_target, tmp_path)
    before = src_path.read_bytes()
    p = flow.preview(store, sid, "fake")
    manager, client, task = await connect(store)
    out = await flow.continue_(store, manager, sid, "fake", "keep going", p["preview_hash"], "cont-1")
    await settled(manager, "cont-1")
    target = out["session_id"]
    row = session_row(store, target)
    assert (row["runtime"], row["mode"], row["control_owner"]) == ("fake", "managed", "robin")
    [path] = written(fake_target)
    assert str(path) == out["link"]["written_path"] and row["native_session_id"] in path.name
    text = "\n".join(b.text for m in readers.read_pi(path).msgs for b in m.blocks)
    assert "Fix the login bug" in text and "[external_agent_tool_call: Read]" in text
    assert count(store, Event, Event.session_id == target) > 0  # the converted history is in Robin's view
    assert count(store, SessionLink, SessionLink.target_session_id == target) == 1
    assert count(store, AuditEntry, AuditEntry.action == "session.continue") == 1
    assert src_path.read_bytes() == before
    with store.reader.connect() as c:
        links = flow.links_for(c, [sid, target])
    assert links[target]["continued_from"]["session_id"] == sid
    assert [x["session_id"] for x in links[sid]["continued_in"]] == [target]
    HistoryWatcher(store, {"fake": fake_target["fake"]}).cycle()  # the watcher finds the file: no duplicate
    assert count(store, SessionRow, SessionRow.native_session_id == row["native_session_id"]) == 1
    again = await flow.continue_(store, manager, sid, "fake", "keep going", p["preview_hash"], "cont-1")
    assert again["session_id"] == target and len(written(fake_target)) == 1  # a retry writes nothing
    await manager.stop(target, action_id="s1")
    await client.close()
    await asyncio.wait_for(task, 5)


async def test_a_stale_preview_is_refused_before_anything_is_written(supervised, fake_target, tmp_path):
    store = supervised
    sid, src_path = claude_source(store, fake_target, tmp_path)
    p = flow.preview(store, sid, "fake")
    with src_path.open("a") as f:
        f.write(json.dumps({"type": "user", "message": {"role": "user", "content": "more"}}) + "\n")
    old = time.time() - 600
    os.utime(src_path, (old, old))
    manager, client, task = await connect(store)
    with pytest.raises(flow.ContinueError) as err:
        await flow.continue_(store, manager, sid, "fake", "go", p["preview_hash"], "cont-1")
    assert (err.value.code, err.value.status) == ("preview_stale", 409)
    assert written(fake_target) == [] and count(store, SessionLink) == 0
    await client.close()
    await asyncio.wait_for(task, 5)


async def test_a_target_that_never_starts_stays_linked_and_can_be_continued_again(
    supervised, fake_target, tmp_path, monkeypatch
):
    store = supervised
    sid, _ = claude_source(store, fake_target, tmp_path)
    p = flow.preview(store, sid, "fake")
    manager, client, task = await connect(store)
    real = manager._relaunch_env

    async def failing(session_id, row):
        return {**await real(session_id, row), "ROBIN_FAKE_FAIL_START": "1"}

    monkeypatch.setattr(manager, "_relaunch_env", failing)
    out = await flow.continue_(store, manager, sid, "fake", "go", p["preview_hash"], "cont-1")
    await settled(manager, "cont-1", state="failed")
    target = out["session_id"]
    assert session_row(store, target)["mode"] == "observed"
    assert count(store, SessionLink, SessionLink.target_session_id == target) == 1
    assert not _recently_written(store, target)  # Robin's own file, untouched: no quiet window to wait out
    monkeypatch.setattr(manager, "_relaunch_env", real)
    await manager.takeover(target, "try again", action_id="tk-2")
    await settled(manager, "tk-2")
    await manager.stop(target, action_id="s1")
    with Path(out["link"]["written_path"]).open("a") as f:
        f.write("\n")
    assert _recently_written(store, target)  # touched since: the exemption no longer applies
    await client.close()
    await asyncio.wait_for(task, 5)


# -- API and CLI --------------------------------------------------------------------


def test_the_api_previews_continues_and_links_both_sessions(api, fake_target, tmp_path):
    store, c, _, _ = api
    sid, _ = claude_source(store, fake_target, tmp_path)
    unknown = c.post(f"/sessions/{sid}/continue/preview", json={"runtime": "gemini"}).json()
    assert unknown["refusal"]["code"] == "unsupported"
    flow_preview = c.post(f"/sessions/{sid}/continue/preview", json={"runtime": "fake"}).json()
    same = c.post(f"/sessions/{sid}/continue/preview", json={"runtime": "claude"}).json()
    assert same["refusal"]["code"] == "same_runtime"
    body = {"runtime": "claude", "text": "go", "preview_hash": "sha256:none", "action_id": "act-continue-1"}
    r = c.post(f"/sessions/{sid}/continue", json=body)
    assert (r.status_code, r.json()["error"]["code"]) == (422, "same_runtime")
    assert flow_preview["refusal"] is None
    missing = c.post("/sessions/no-such/continue/preview", json={"runtime": "codex"})
    assert (missing.status_code, missing.json()["error"]["code"]) == (404, "not_found")
    view = c.get(f"/sessions/{sid}").json()
    assert view["continued_from"] is None and view["continued_in"] == []


async def test_the_api_continue_route_runs_the_whole_flow(api, fake_target, tmp_path):
    store, c, _, _ = api
    sid, _ = claude_source(store, fake_target, tmp_path)
    p = flow.preview(store, sid, "fake")
    body = {"runtime": "fake", "text": "go", "preview_hash": p["preview_hash"], "action_id": "act-continue-2"}
    r = await asyncio.to_thread(c.post, f"/sessions/{sid}/continue", json=body)
    assert r.status_code == 202
    target = r.json()["session_id"]
    assert c.get(f"/sessions/{target}").json()["continued_from"]["session_id"] == sid
    assert c.get(f"/sessions/{sid}").json()["continued_in"][0]["session_id"] == target
    stale = {**body, "action_id": "act-continue-3", "preview_hash": "sha256:old"}
    r2 = await asyncio.to_thread(c.post, f"/sessions/{sid}/continue", json=stale)
    assert (r2.status_code, r2.json()["error"]["code"]) == (409, "preview_stale")


def test_the_cli_previews_and_explains_refusals(fake_target, tmp_path, capsys, supervised):
    import threading

    from robin.cli import CliError
    from robin.service.main import build, free_port, write_endpoint
    from test_cli import run

    store = supervised
    port = free_port()
    server, _ = build(store, port)
    thread = threading.Thread(target=lambda: asyncio.run(server.serve()), daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.02)
    write_endpoint(store, port)
    try:
        sid, _ = claude_source(store, fake_target, tmp_path)
        d = str(store.data_dir)
        out = run(capsys, "continue", sid, "--in", "codex", "--data", d)
        assert f"Continue claude session {sid}" in out and "in codex" in out
        assert "messages:    6 (2 user, 3 assistant, 1 tool calls written as text)" in out
        assert '1 images -> "[image]"' in out and "first message:" in out
        assert f"robin continue {sid} --in codex --confirm sha256:" in out
        with pytest.raises((CliError, SystemExit)):
            run(capsys, "continue", sid, "--in", "claude", "--data", d)
    finally:
        server.should_exit = True
        thread.join(10)


async def test_a_retry_after_a_refused_takeover_runs_the_takeover_instead_of_reporting_nothing(
    supervised, fake_target, tmp_path, monkeypatch
):
    from robin.runtime import terminal
    from robin.runtime.ledger import ActionRejected

    store = supervised
    sid, _ = claude_source(store, fake_target, tmp_path)
    p = flow.preview(store, sid, "fake")
    manager, client, task = await connect(store)
    real = terminal.holders
    monkeypatch.setattr(terminal, "holders", lambda native: [4242] if native != "src-1" else [])
    with pytest.raises(ActionRejected):  # the target looks open in a terminal: takeover refuses
        await flow.continue_(store, manager, sid, "fake", "go", p["preview_hash"], "cont-1")
    assert await manager.get_action("cont-1") is None and len(written(fake_target)) == 1
    monkeypatch.setattr(terminal, "holders", real)
    out = await flow.continue_(store, manager, sid, "fake", "go", p["preview_hash"], "cont-1")
    assert out["action"] is not None and len(written(fake_target)) == 1
    await settled(manager, "cont-1")
    await manager.stop(out["session_id"], action_id="s1")
    await client.close()
    await asyncio.wait_for(task, 5)


async def test_duplicate_submits_at_once_write_one_session(supervised, fake_target, tmp_path):
    store = supervised
    sid, _ = claude_source(store, fake_target, tmp_path)
    p = flow.preview(store, sid, "fake")
    manager, client, task = await connect(store)
    a, b = await asyncio.gather(
        flow.continue_(store, manager, sid, "fake", "go", p["preview_hash"], "cont-1"),
        flow.continue_(store, manager, sid, "fake", "go", p["preview_hash"], "cont-1"),
    )
    assert a["session_id"] == b["session_id"] and len(written(fake_target)) == 1
    await settled(manager, "cont-1")
    await manager.stop(a["session_id"], action_id="s1")
    await client.close()
    await asyncio.wait_for(task, 5)


def test_a_reviewer_continued_where_it_cant_be_isolated_is_not_called_isolated(store, fake_target, tmp_path):
    from sqlalchemy import update

    sid, _ = claude_source(store, fake_target, tmp_path)
    store.writer.call(
        lambda c: c.execute(
            update(SessionRow).where(SessionRow.session_id == sid).values(independence="enforced")
        )
    )
    pi = flow.preview(store, sid, "pi")
    assert pi["refusal"] is None and pi["inherits"]["independence"] == "not_isolated"
    assert any("sandbox" in w for w in pi["warnings"])
    fake = flow.preview(store, sid, "fake")
    assert fake["inherits"]["independence"] == "enforced" and fake["warnings"] == []


async def test_reusing_an_action_id_with_a_different_first_message_is_a_conflict(
    supervised, fake_target, tmp_path
):
    from robin.runtime.ledger import ActionConflict

    store = supervised
    sid, _ = claude_source(store, fake_target, tmp_path)
    p = flow.preview(store, sid, "fake")
    manager, client, task = await connect(store)
    out = await flow.continue_(store, manager, sid, "fake", "original", p["preview_hash"], "cont-1")
    await settled(manager, "cont-1")
    with pytest.raises(ActionConflict):
        await flow.continue_(store, manager, sid, "fake", "DIFFERENT", p["preview_hash"], "cont-1")
    again = await flow.continue_(store, manager, sid, "fake", "original", p["preview_hash"], "cont-1")
    assert again["action"].action_id == out["action"].action_id and len(written(fake_target)) == 1
    await manager.stop(out["session_id"], action_id="s1")
    await client.close()
    await asyncio.wait_for(task, 5)
