"""Continuing a session in another runtime: edges the main suite leaves open (ids, paths, window config,
reader robustness, turn shaping, non-Claude sources, inheritance and the CLI's confirm step)."""

import argparse
import asyncio
import json
import os
import shutil
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select, update

import test_continuation
from robin import discovery, ids
from robin.continuation import flow, paths, readers, size
from robin.continuation.canonical import Msg, Text, ToolCall, ToolResult, turns
from robin.store.models import Session as SessionRow
from robin.store.models import TaskSession
from test_continuation import (
    T0,
    claude_records,
    claude_source,
    codex_records,
    jsonl,
    pi_records,
)
from test_handoff import settled
from test_manager import connect, session_row

fake_target = test_continuation.fake_target  # the fixture, shared

WHEN = datetime(2026, 10, 18, 1, 2, 3, 456000, tzinfo=UTC)


# -- ids ----------------------------------------------------------------------------


def test_uuid7_is_an_rfc_9562_v7_carrying_the_current_millisecond():
    before = int(time.time() * 1000)
    u = uuid.UUID(ids.uuid7())
    after = int(time.time() * 1000)
    assert u.version == 7 and u.variant == uuid.RFC_4122
    assert before <= int.from_bytes(u.bytes[:6], "big") <= after


def test_uuid7s_from_later_milliseconds_sort_later_as_text():
    first = ids.uuid7()
    time.sleep(0.003)
    assert ids.uuid7() > first


# -- paths --------------------------------------------------------------------------


@pytest.mark.parametrize("runtime", ["claude", "codex", "pi"])
def test_locate_finds_what_target_path_names(tmp_path, monkeypatch, runtime):
    monkeypatch.setenv(f"ROBIN_HISTORY_{runtime.upper()}", str(tmp_path / "store"))
    monkeypatch.delenv("ROBIN_PI_SESSION_DIR", raising=False)
    native = ids.uuid7()
    path = paths.target_path(runtime, native, "/w/my app", WHEN)
    jsonl(path, [{"type": "x"}])
    jsonl(paths.target_path(runtime, ids.uuid7(), "/w/my app", WHEN), [{"type": "x"}])  # a neighbour
    assert paths.locate(runtime, native) == path
    assert paths.locate(runtime, "no-such-id") is None


def test_locate_looks_in_pis_session_dir_first_then_its_store(tmp_path, monkeypatch):
    monkeypatch.setenv("ROBIN_HISTORY_PI", str(tmp_path / "store"))
    monkeypatch.setenv("ROBIN_PI_SESSION_DIR", str(tmp_path / "flat"))
    flat = paths.target_path("pi", "flat-id", "/w/app", WHEN)
    assert flat.parent == tmp_path / "flat"
    jsonl(flat, [{"type": "session"}])
    nested = jsonl(tmp_path / "store" / paths.pi_folder("/w/app") / "2026-10-18_nested-id.jsonl", [{}])
    assert paths.locate("pi", "flat-id") == flat
    assert paths.locate("pi", "nested-id") == nested  # launched before the session dir was set


def test_claude_folder_names_over_200_characters_are_unwritable():
    at_limit = "/" + "a" * (paths.CLAUDE_FOLDER_MAX - 1)
    over = at_limit + "b"
    assert paths.unwritable("claude", at_limit) is None
    assert "too long" in paths.unwritable("claude", over)
    assert paths.unwritable("codex", over) is None and paths.unwritable("pi", over) is None


# -- context window ------------------------------------------------------------------


def test_codex_model_context_window_in_config_wins_over_the_models_cache(tmp_path):
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "config.toml").write_text('model = "m1"\nmodel_context_window = 128000\n')
    (tmp_path / ".codex" / "models_cache.json").write_text(
        json.dumps({"models": [{"slug": "m1", "context_window": 500_000}]})
    )
    assert size.window("codex", tmp_path) == size.Window("m1", 128_000, "config.toml")


def test_pi_window_comes_from_the_default_providers_entry_in_models_store(tmp_path):
    agent = tmp_path / ".pi" / "agent"
    agent.mkdir(parents=True)
    (agent / "settings.json").write_text(json.dumps({"defaultProvider": "openrouter", "defaultModel": "k3"}))
    (agent / "models-store.json").write_text(
        json.dumps(
            {
                "anthropic": {"models": [{"id": "k3", "contextWindow": 1}]},
                "openrouter": {
                    "models": [{"id": "other", "contextWindow": 2}, {"id": "k3", "contextWindow": 262_144}]
                },
            }
        )
    )
    assert size.window("pi", tmp_path) == size.Window("k3", 262_144, "models-store.json")


def test_malformed_runtime_config_falls_back_to_the_default_window(tmp_path):
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "config.toml").write_text("model = = broken\n")
    (tmp_path / ".codex" / "models_cache.json").write_text("{not json")
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text("[1, 2")
    (tmp_path / ".pi" / "agent").mkdir(parents=True)
    (tmp_path / ".pi" / "agent" / "settings.json").write_text('"just a string"')
    (tmp_path / ".pi" / "agent" / "models-store.json").write_text("{")
    assert size.window("codex", tmp_path) == size.Window(None, 272_000, "default")
    assert size.window("claude", tmp_path) == size.Window(None, 200_000, "default")
    assert size.window("pi", tmp_path) == size.Window(None, 200_000, "default")


def test_a_claude_model_without_1m_keeps_the_200k_default(tmp_path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text(json.dumps({"model": "opus"}))
    assert size.window("claude", tmp_path) == size.Window("opus", 200_000, "default")


# -- reader robustness ----------------------------------------------------------------


def test_torn_and_corrupt_lines_are_skipped_without_losing_the_rest(tmp_path):
    clean = readers.read_claude(jsonl(tmp_path / "clean.jsonl", claude_records("s", "/w")))
    path = tmp_path / "torn.jsonl"
    lines = [json.dumps(r) for r in claude_records("s", "/w")]
    lines.insert(3, "\x00\x01 not json")
    lines.insert(5, "[1, 2, 3]")  # valid JSON, not a record
    path.write_text("\n".join(lines) + '\n{"type": "user", "message": {"role": "us')
    torn = readers.read_claude(path)
    assert [m.blocks for m in torn.msgs] == [m.blocks for m in clean.msgs]


def test_claude_redacted_thinking_and_unknown_blocks_are_counted_not_carried(tmp_path):
    base = {"sessionId": "s", "cwd": "/w", "timestamp": T0}
    recs = [
        {**base, "type": "user", "uuid": "u1", "message": {"role": "user", "content": "go"}},
        {
            **base,
            "type": "assistant",
            "uuid": "a1",
            "message": {
                "id": "m1",
                "role": "assistant",
                "content": [
                    {"type": "redacted_thinking", "data": "xxx"},
                    {"type": "server_tool_use", "id": "z"},
                    {"type": "text", "text": "done"},
                ],
            },
        },
    ]
    t = readers.read_claude(jsonl(tmp_path / "c.jsonl", recs))
    assert t.msgs[1].blocks == [Text("done")]
    assert t.dropped == {"redacted_thinking": 1, "unknown_items": 1}


def test_claude_tool_reference_results_read_as_text(tmp_path):
    base = {"sessionId": "s", "cwd": "/w", "timestamp": T0}
    recs = [
        {
            **base,
            "type": "user",
            "uuid": "u1",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "content": [{"type": "tool_reference", "tool_name": "WebFetch"}],
                    }
                ],
            },
        }
    ]
    t = readers.read_claude(jsonl(tmp_path / "c.jsonl", recs))
    assert t.msgs[0].blocks == [ToolResult("t1", "[tool reference: WebFetch]")]
    assert "unknown_items" not in t.dropped


def codex_items(*payloads: dict) -> list[dict]:
    return [{"timestamp": T0, "type": "session_meta", "payload": {"id": "s", "cwd": "/w"}}] + [
        {"timestamp": T0, "type": "response_item", "payload": p} for p in payloads
    ]


def test_codex_tool_arguments_that_are_not_json_cross_as_the_raw_string(tmp_path):
    t = readers.read_codex(
        jsonl(
            tmp_path / "x.jsonl",
            codex_items(
                {"type": "function_call", "name": "shell", "arguments": "ls -la {", "call_id": "c1"},
                {
                    "type": "function_call_output",
                    "call_id": "c1",
                    "output": {"content": "a\nb", "success": True},
                },
            ),
        )
    )
    assert t.msgs[0].blocks == [ToolCall("c1", "shell", "ls -la {")]
    assert t.msgs[1].blocks == [ToolResult("c1", "a\nb")]  # older rollouts wrap output in a dict


def test_codex_encrypted_content_parts_are_counted_and_left_out(tmp_path):
    t = readers.read_codex(
        jsonl(
            tmp_path / "x.jsonl",
            codex_items(
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {"type": "encrypted_content", "encrypted_content": "gAAAA"},
                        {"type": "output_text", "text": "visible"},
                    ],
                }
            ),
        )
    )
    assert t.msgs[0].blocks == [Text("visible")]
    assert t.dropped == {"encrypted_reasoning": 1}


def test_pi_assistant_string_content_and_images(tmp_path):
    recs = [
        {"type": "session", "version": 3, "id": "s", "cwd": "/w"},
        {"type": "message", "id": "e1", "message": {"role": "user", "content": "hi"}},
        {"type": "message", "id": "e2", "message": {"role": "assistant", "content": "plain reply"}},
        {
            "type": "message",
            "id": "e3",
            "message": {
                "role": "assistant",
                "content": [{"type": "image", "data": "..."}, {"type": "mystery"}],
            },
        },
    ]
    t = readers.read_pi(jsonl(tmp_path / "p.jsonl", recs))
    assert [m.blocks for m in t.msgs] == [[Text("hi")], [Text("plain reply")], [Text("[image]")]]
    assert t.dropped == {"images": 1, "unknown_items": 1}


# -- turn shaping ---------------------------------------------------------------------


def test_a_transcript_ending_on_the_persons_message_gets_a_closing_reply():
    ts = datetime(2026, 10, 18, tzinfo=UTC)
    out = turns(
        [Msg("user", [Text("a")], ts), Msg("assistant", [Text("b")], ts), Msg("user", [Text("c")], ts)], "pi"
    )
    assert [r for r, _, _ in out] == ["user", "assistant", "user", "assistant"]
    assert "ended before replying" in out[-1][1] and out[-1][2] == ts


def test_a_transcript_opening_with_a_reply_gets_a_user_turn_first():
    out = turns([Msg("assistant", [Text("b")]), Msg("tool", [ToolResult("c", "r")])], "codex")
    assert out[0] == ("user", "[Continued from an earlier codex session]", None)
    assert [r for r, _, _ in out] == ["user", "assistant", "assistant"]  # tool results sit with the assistant


# -- preview: other sources, folders, permissions --------------------------------------


def indexed_source(store, roots, tmp_path, runtime, native="src-9"):
    """An observed Codex or Pi session in its own store, indexed and quiet for ten minutes."""
    cwd = tmp_path / "proj"
    cwd.mkdir(exist_ok=True)
    if runtime == "codex":
        rel = Path("2026/10/18") / f"rollout-2026-10-18T01-00-00-{native}.jsonl"
        recs = codex_records(native, str(cwd))
    else:
        rel = Path(paths.pi_folder(str(cwd))) / f"2026-10-18T01-00-00-000Z_{native}.jsonl"
        recs = pi_records(native, str(cwd))
    path = jsonl(roots[runtime] / rel, recs)
    discovery.scan(store, [runtime])
    old = time.time() - 600
    os.utime(path, (old, old))
    with store.reader.connect() as c:
        sid = c.execute(
            select(SessionRow.session_id).where(SessionRow.native_session_id == native)
        ).scalar_one()
    return sid, path


@pytest.mark.parametrize(
    ("runtime", "messages"),
    [
        ("codex", {"total": 8, "user": 1, "assistant": 5, "tool_calls": 2, "tool_results": 2}),
        ("pi", {"total": 4, "user": 1, "assistant": 2, "tool_calls": 1, "tool_results": 1}),
    ],
)
def test_codex_and_pi_sessions_are_found_in_their_stores_and_previewed(
    store,
    fake_target,
    tmp_path,
    runtime,
    messages,
):
    sid, _ = indexed_source(store, fake_target, tmp_path, runtime)
    p = flow.preview(store, sid, "fake")
    assert p["refusal"] is None and p["source"]["runtime"] == runtime
    assert p["messages"] == messages


def test_a_folder_too_long_for_claudes_store_is_refused_before_anything_is_written(
    store, fake_target, tmp_path
):
    deep = tmp_path / ("d" * 120) / ("e" * 120)
    deep.mkdir(parents=True)
    path = jsonl(
        fake_target["codex"] / "2026/10/18" / "rollout-2026-10-18T01-00-00-long-1.jsonl",
        codex_records("long-1", str(deep)),
    )
    discovery.scan(store, ["codex"])
    old = time.time() - 600
    os.utime(path, (old, old))
    with store.reader.connect() as c:
        sid = c.execute(
            select(SessionRow.session_id).where(SessionRow.native_session_id == "long-1")
        ).scalar_one()
    refusal = flow.preview(store, sid, "claude")["refusal"]
    assert refusal["code"] == "no_cwd" and "too long" in refusal["message"]
    assert not list(fake_target["claude"].rglob("*.jsonl"))


def test_a_session_whose_folder_is_gone_is_refused(store, fake_target, tmp_path):
    sid, _ = claude_source(store, fake_target, tmp_path)
    shutil.rmtree(tmp_path / "proj")
    assert flow.preview(store, sid, "fake")["refusal"]["code"] == "no_cwd"


def test_skip_permissions_is_dropped_with_a_warning_for_a_target_that_cant_skip(
    store,
    fake_target,
    tmp_path,
):
    sid, _ = claude_source(store, fake_target, tmp_path)
    store.writer.call(
        lambda c: c.execute(update(SessionRow).where(SessionRow.session_id == sid).values(skip_permissions=1))
    )
    assert flow.preview(store, sid, "fake")["inherits"]["skip_permissions"] is True
    p = flow.preview(store, sid, "pi")
    assert p["refusal"] is None and p["inherits"]["skip_permissions"] is False
    assert p["warnings"] == ["pi will ask for permissions: it can't skip permission prompts"]


async def test_the_continued_session_inherits_task_role_project_and_skip_permissions(
    supervised,
    fake_target,
    tmp_path,
):
    from robin.workspace import tasks as ws

    store = supervised
    sid, _ = claude_source(store, fake_target, tmp_path)
    project = ws.register_project(store, str(tmp_path / "proj"))
    task = ws.create_task(store, project_id=project["project_id"], title="t", objective="o")["task_id"]
    ws.attach_session(store, task, sid, role="reviewer")
    store.writer.call(
        lambda c: c.execute(
            update(SessionRow)
            .where(SessionRow.session_id == sid)
            .values(skip_permissions=1, project_id=project["project_id"])
        )
    )
    p = flow.preview(store, sid, "fake")
    assert p["inherits"]["task_id"] == task
    manager, client, t = await connect(store)
    out = await flow.continue_(store, manager, sid, "fake", "go", p["preview_hash"], "cont-inherit")
    await settled(manager, "cont-inherit")
    target = out["session_id"]
    row = session_row(store, target)
    assert (row["current_task_id"], row["project_id"], row["skip_permissions"]) == (
        task,
        project["project_id"],
        1,
    )
    with store.reader.connect() as c:
        role = c.execute(
            select(TaskSession.role).where(TaskSession.task_id == task, TaskSession.session_id == target)
        ).scalar_one()
    assert role == "reviewer"
    await manager.stop(target, action_id="s1")
    await client.close()
    await asyncio.wait_for(t, 5)


# -- CLI confirm step ------------------------------------------------------------------


@pytest.fixture
def served_cli(supervised):
    from robin.service.main import build, free_port, write_endpoint

    store = supervised
    port = free_port()
    server, _ = build(store, port)
    thread = threading.Thread(target=lambda: asyncio.run(server.serve()), daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.02)
    write_endpoint(store, port)
    yield store
    server.should_exit = True
    thread.join(10)


def continue_args(store, sid, confirm):
    # --in only offers the real runtimes; the scripted "fake" target is reached through cmd_continue itself.
    return argparse.Namespace(
        data=store.data_dir,
        session=sid,
        into="fake",
        confirm=confirm,
        text="go on",
        file=None,
        wait=10,
        no_chat=False,
    )


def test_the_cli_confirm_step_continues_and_names_the_new_session(served_cli, fake_target, tmp_path, capsys):
    from robin.cli import cmd_continue

    store = served_cli
    sid, _ = claude_source(store, fake_target, tmp_path)
    p = flow.preview(store, sid, "fake")
    cmd_continue(continue_args(store, sid, p["preview_hash"]))
    out = capsys.readouterr().out
    with store.reader.connect() as c:
        target, native = c.execute(
            select(SessionRow.session_id, SessionRow.native_session_id).where(SessionRow.runtime == "fake")
        ).one()
    assert f"continued in fake: session {target} (native {native})" in out
    assert "acknowledged" in out


def test_one_command_continues_from_the_start_of_an_id_and_opens_the_chat(
    served_cli, fake_target, tmp_path, monkeypatch
):
    from robin import cli
    from robin.chat import app as chat_app

    store = served_cli
    sid, _ = claude_source(store, fake_target, tmp_path)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True, raising=False)
    monkeypatch.setattr("builtins.input", lambda prompt: "")  # Enter takes the default: yes
    chats = []
    monkeypatch.setattr(chat_app, "run_chat", lambda data, session_id: chats.append(session_id))
    cli.cmd_continue(continue_args(store, sid[:8], None))
    with store.reader.connect() as c:
        target = c.execute(select(SessionRow.session_id).where(SessionRow.runtime == "fake")).scalar_one()
    assert chats == [target]


def test_the_start_of_an_id_must_name_one_session(served_cli, fake_target, tmp_path):
    from robin.cli import CliError, client, resolve_session

    store = served_cli
    sid, _ = claude_source(store, fake_target, tmp_path)
    with client(store.data_dir) as c:
        assert resolve_session(c, sid[:-1]) == sid  # one character short, as copied from a narrow terminal
        assert resolve_session(c, sid) == sid
        with pytest.raises(CliError, match="no session starting"):
            resolve_session(c, "ffffffff-0000")


def test_a_start_shared_by_several_sessions_lists_them(monkeypatch):
    from robin import cli

    two = [
        {"session_id": f"c4bf2d75-{n}", "runtime": "claude", "runtime_state": "stopped"} for n in ("a", "b")
    ]
    monkeypatch.setattr(cli, "call", lambda c, method, path: {"sessions": two})
    with pytest.raises(cli.CliError, match="more than one session") as err:
        cli.resolve_session(None, "c4bf")
    assert "c4bf2d75-a" in str(err.value) and "c4bf2d75-b" in str(err.value)


def test_the_cli_says_where_the_session_is_kept_when_the_target_never_starts(
    served_cli,
    fake_target,
    tmp_path,
    capsys,
    monkeypatch,
):
    from robin.cli import CliError, cmd_continue
    from robin.runtime.manager import RuntimeManager

    store = served_cli
    sid, _ = claude_source(store, fake_target, tmp_path)
    real = RuntimeManager._relaunch_env

    async def failing(self, session_id, row):
        return {**await real(self, session_id, row), "ROBIN_FAKE_FAIL_START": "1"}

    monkeypatch.setattr(RuntimeManager, "_relaunch_env", failing)
    p = flow.preview(store, sid, "fake")
    with pytest.raises(CliError) as err:
        cmd_continue(continue_args(store, sid, p["preview_hash"]))
    with store.reader.connect() as c:
        target = c.execute(select(SessionRow.session_id).where(SessionRow.runtime == "fake")).scalar_one()
    assert "fake didn't start" in str(err.value) and f"kept as {target}" in str(err.value)
    assert session_row(store, target)["mode"] == "observed"


def test_pi_folder_names_too_long_for_the_filesystem_are_unwritable(monkeypatch):
    monkeypatch.delenv("ROBIN_PI_SESSION_DIR", raising=False)
    long_cwd = "/" + "/".join(["d" * 50] * 6)  # Pi's folder name for it is over 255 bytes
    assert paths.unwritable("pi", long_cwd) == "the folder's path is too long for Pi's session store"
    assert paths.unwritable("pi", "/w/app") is None
    monkeypatch.setenv(
        "ROBIN_PI_SESSION_DIR", "/tmp/flat"
    )  # files sit flat there: no folder name to overflow
    assert paths.unwritable("pi", long_cwd) is None
