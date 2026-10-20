"""Opt-in hook installers, the relay and hook ingestion (FR-041, TR-076).

Every test points the installers at temporary config files; the user's real harness configs are never read
or written here.
"""

import asyncio
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from sqlalchemy import select

from robin.events.ingest import ensure_session, read_events
from robin.hooks import ingest as hook_ingest
from robin.hooks import install as hooks
from robin.service.main import build, free_port, write_endpoint
from robin.store.models import Session as SessionRow

EXISTING = {
    "model": "opus",
    "permissions": {"allow": ["Bash(ls:*)"]},
    "hooks": {
        "PreToolUse": [
            {
                "matcher": "Bash",
                "hooks": [{"type": "command", "command": "~/.orca/agent-hooks/claude-hook.sh"}],
            }
        ],
        "PermissionRequest": [{"hooks": [{"type": "command", "command": "my-own-guard", "timeout": 3}]}],
    },
}


@pytest.fixture
def configs(tmp_path, monkeypatch):
    paths = {
        "claude": tmp_path / "claude" / "settings.json",
        "codex": tmp_path / "codex" / "hooks.json",
        "pi": tmp_path / "pi" / "extensions" / "robin-observer.ts",
    }
    for runtime, path in paths.items():
        monkeypatch.setenv(f"ROBIN_HOOK_CONFIG_{runtime.upper()}", str(path))
    paths["claude"].parent.mkdir()
    paths["claude"].write_text(json.dumps(EXISTING, indent=2) + "\n")
    data = tmp_path / "data"
    data.mkdir(mode=0o700)
    return paths, data


def test_install_appends_only_robin_entries_and_uninstall_restores_the_file(configs):
    paths, data = configs
    original = paths["claude"].read_text()
    plan = hooks.plan_install("claude", data)
    assert plan.action == "install" and "+" in plan.diff()
    backup = hooks.apply(plan, data)
    assert backup is not None and Path(backup).read_text() == original

    after = json.loads(paths["claude"].read_text())
    assert after["model"] == "opus" and after["permissions"] == EXISTING["permissions"]
    pre = after["hooks"]["PreToolUse"]
    assert pre[0] == EXISTING["hooks"]["PreToolUse"][0]  # theirs first, untouched
    assert hooks.is_robin(pre[1]["hooks"][0]) and pre[1]["hooks"][0]["timeout"] == hooks.HOOK_TIMEOUT_S
    assert (
        after["hooks"]["PermissionRequest"] == EXISTING["hooks"]["PermissionRequest"]
    )  # never an approval hook
    assert set(hooks.EVENTS["claude"]) <= set(after["hooks"])
    assert hooks.status("claude", data) == {
        "runtime": "claude",
        "config": str(paths["claude"]),
        "installed": True,
        "drift": [],
    }
    assert hooks.plan_install("claude", data).action == "nothing"  # no duplicates

    hooks.apply(hooks.plan_uninstall("claude", data), data)
    assert paths["claude"].read_text() == original
    assert not hooks.status("claude", data)["installed"]


def test_codex_install_creates_the_file_and_uninstall_leaves_it_empty(configs):
    paths, data = configs
    hooks.apply(hooks.plan_install("codex", data), data)
    groups = json.loads(paths["codex"].read_text())["hooks"]
    assert set(groups) == set(hooks.EVENTS["codex"])
    hooks.apply(hooks.plan_uninstall("codex", data), data)
    assert json.loads(paths["codex"].read_text()) == {}


def test_drift_is_reported_and_reinstall_repairs_a_moved_relay(configs):
    paths, data = configs
    hooks.apply(hooks.plan_install("claude", data), data)
    cfg = json.loads(paths["claude"].read_text())
    cfg["hooks"]["Stop"] = []  # someone removed Robin's Stop hook
    for g in cfg["hooks"]["SessionStart"]:
        for h in g["hooks"]:
            h["command"] = h["command"].replace(sys.executable, "/old/venv/bin/python")
    paths["claude"].write_text(json.dumps(cfg, indent=2) + "\n")
    drift = hooks.status("claude", data)["drift"]
    assert any("missing for Stop" in d for d in drift) and any("different relay command" in d for d in drift)

    hooks.apply(hooks.plan_install("claude", data), data)
    assert hooks.status("claude", data)["drift"] == []
    commands = {
        h["command"]
        for gs in json.loads(paths["claude"].read_text())["hooks"].values()
        for g in gs
        for h in g["hooks"]
        if hooks.is_robin(h)
    }
    assert commands == {hooks.relay_command("claude", data), hooks.ask_command(data)}


def test_invalid_config_and_concurrent_edits_are_refused(configs):
    paths, data = configs
    paths["claude"].write_text("{ not json")
    with pytest.raises(hooks.InstallError, match="isn't valid JSON"):
        hooks.plan_install("claude", data)
    paths["claude"].write_text(json.dumps(EXISTING) + "\n")
    plan = hooks.plan_install("claude", data)
    paths["claude"].write_text(json.dumps({**EXISTING, "model": "sonnet"}) + "\n")  # edited meanwhile
    with pytest.raises(hooks.InstallError, match="changed while you were reviewing"):
        hooks.apply(plan, data)
    assert json.loads(paths["claude"].read_text())["model"] == "sonnet"


def test_pi_observer_file_is_tracked_and_never_removed_once_edited(configs):
    paths, data = configs
    hooks.apply(hooks.plan_install("pi", data), data)
    text = paths["pi"].read_text()
    assert (
        json.dumps([sys.executable, "-m", hooks.RELAY_MODULE, "--runtime", "pi", "--data", str(data)]) in text
    )
    assert "__ROBIN_RELAY_ARGV__" not in text and "block" not in text.split("export default")[1]
    assert hooks.status("pi", data)["installed"]
    paths["pi"].write_text(text + "\n// my tweak\n")
    assert hooks.status("pi", data)["drift"] == ["the observer file changed since Robin wrote it"]
    with pytest.raises(hooks.InstallError, match="changed since Robin wrote it"):
        hooks.plan_uninstall("pi", data)
    paths["pi"].write_text(text)
    hooks.apply(hooks.plan_uninstall("pi", data), data)
    assert not paths["pi"].exists()


# -- the relay --------------------------------------------------------------------------------------------


def relay(runtime, data, payload: bytes, timeout=10):
    started = time.monotonic()
    p = subprocess.run(
        [sys.executable, "-m", hooks.RELAY_MODULE, "--runtime", runtime, "--data", str(data)],
        input=payload,
        capture_output=True,
        timeout=timeout,
    )
    return p, time.monotonic() - started


def test_relay_with_robin_down_exits_zero_silently_and_notes_the_drop(tmp_path):
    data = tmp_path / "data"
    (data / "hooks").mkdir(parents=True)
    (data / "service.json").write_text(json.dumps({"url": f"http://127.0.0.1:{free_port()}"}))
    (data / "cli.token").write_text("x")
    p, took = relay("claude", data, json.dumps({"session_id": "n1", "hook_event_name": "Stop"}).encode())
    assert p.returncode == 0 and p.stdout == b"" and p.stderr == b"" and took < 3
    (drop,) = [json.loads(line) for line in (data / "hooks" / "dropped.jsonl").read_text().splitlines()]
    assert drop["session_id"] == "n1" and drop["event"] == "Stop"


def test_relay_does_nothing_inside_a_robin_launched_session(tmp_path, monkeypatch):
    data = tmp_path / "data"
    (data / "hooks").mkdir(parents=True)
    monkeypatch.setenv("ROBIN_LAUNCH_TOKEN", "set-by-the-runtime-host")
    p, _ = relay("claude", data, json.dumps({"session_id": "n1", "hook_event_name": "Stop"}).encode())
    assert p.returncode == 0 and not (data / "hooks" / "dropped.jsonl").exists()


def test_relay_survives_garbage_huge_input_and_missing_config(tmp_path):
    p, took = relay("claude", tmp_path / "nowhere", b"x" * (6 * 1024 * 1024))
    assert p.returncode == 0 and p.stdout == b"" and took < 5


# -- ingestion through a running service -----------------------------------------------------------------


@pytest.fixture
def served(supervised):
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


def session_by_native(store, native):
    with store.reader.connect() as c:
        return c.execute(select(SessionRow).where(SessionRow.native_session_id == native)).mappings().first()


def test_hook_events_become_observed_session_state(served):
    store = served
    cwd = str(store.data_dir.parent)
    for name in ("SessionStart", "UserPromptSubmit", "PreToolUse"):
        p, _ = relay(
            "claude",
            store.data_dir,
            json.dumps(
                {"session_id": "ext-1", "hook_event_name": name, "cwd": cwd, "tool_name": "Bash"}
            ).encode(),
        )
        assert p.returncode == 0 and p.stdout == b""
    row = session_by_native(store, "ext-1")
    assert row["mode"] == "observed" and row["runtime_state"] == "running" and row["execution_cwd"] == cwd
    relay("claude", store.data_dir, json.dumps({"session_id": "ext-1", "hook_event_name": "Stop"}).encode())
    assert session_by_native(store, "ext-1")["runtime_state"] == "idle"
    types = [e.event_type for e in read_events(store, session_id=row["session_id"])]
    assert types == ["session.started", "session.turn_started", "tool.started", "session.turn_completed"]
    assert not (store.data_dir / "hooks" / "dropped.jsonl").exists()


def test_repeated_delivery_is_stored_once_and_managed_sessions_are_skipped(served):
    store = served
    body = {
        "relay_id": "r1",
        "relayed_at": time.time(),
        "payload": {"session_id": "ext-2", "hook_event_name": "Stop"},
    }
    hook_ingest.record(store, "codex", body)
    hook_ingest.record(store, "codex", body)
    sid = session_by_native(store, "ext-2")["session_id"]
    assert len(read_events(store, session_id=sid)) == 1

    managed = ensure_session(store, runtime="claude", mode="managed", native_session_id="mine")
    out = hook_ingest.record(
        store, "claude", {"relay_id": "r2", "payload": {"session_id": "mine", "hook_event_name": "Stop"}}
    )
    assert out == {"recorded": False, "reason": "managed"} and read_events(store, session_id=managed) == []


def test_dropped_relays_become_coverage_gaps(served):
    store = served
    drops = store.data_dir / "hooks" / "dropped.jsonl"
    drops.write_text(
        json.dumps({"runtime": "pi", "session_id": "ext-3", "event": "turn_start", "at": 1.0}) + "\n"
    )
    assert hook_ingest.drain_drops(store) == 1
    sid = session_by_native(store, "ext-3")["session_id"]
    assert [e.event_type for e in read_events(store, session_id=sid)] == ["connection.gap"]
    assert hook_ingest.drain_drops(store) == 0 and not drops.exists()


def test_hooks_cli_shows_the_diff_and_needs_confirmation(configs, capsys, monkeypatch):
    from robin.cli import CliError, main

    paths, data = configs
    original = paths["claude"].read_text()
    monkeypatch.setattr(sys, "stdin", open("/dev/null"))
    with pytest.raises((CliError, SystemExit)):
        main(["hooks", "install", "claude", "--data", str(data)])
    out = capsys.readouterr().out
    assert "robin.hooks.relay" in out and "robin.hooks.ask" in out and "question hook" in out
    assert paths["claude"].read_text() == original  # nothing written without confirmation
    main(["hooks", "install", "claude", "--yes", "--data", str(data)])
    assert "installed" in capsys.readouterr().out.lower() or hooks.status("claude", data)["installed"]
    main(["hooks", "status", "--data", str(data)])
    assert "claude  installed" in capsys.readouterr().out


def test_concurrent_drains_lose_no_gaps_and_recover_a_crashed_claim(served):
    store = served
    hooks_dir = store.data_dir / "hooks"
    # a claim left behind by a drain that crashed before recording its gaps
    (hooks_dir / "dropped-crashed.draining").write_text(
        json.dumps({"runtime": "claude", "session_id": "left-over", "event": "Stop", "at": 1.0}) + "\n"
    )
    total = 0
    lock = threading.Lock()

    def drain_while_dropping(i):
        nonlocal total
        with (hooks_dir / "dropped.jsonl").open("a") as f:
            f.write(
                json.dumps({"runtime": "codex", "session_id": f"ext-{i}", "event": "Stop", "at": i}) + "\n"
            )
        n = hook_ingest.drain_drops(store)
        with lock:
            total += n

    threads = [threading.Thread(target=drain_while_dropping, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    total += hook_ingest.drain_drops(store)
    assert total == 9
    for native in ["left-over", *(f"ext-{i}" for i in range(8))]:
        sid = session_by_native(store, native)["session_id"]
        assert [e.event_type for e in read_events(store, session_id=sid)] == ["connection.gap"]
    assert not list(hooks_dir.glob("*.draining")) and not (hooks_dir / "dropped.jsonl").exists()


def test_uninstall_after_a_reinstall_still_removes_the_event_lists_robin_created(configs):
    paths, data = configs
    original = paths["claude"].read_text()
    hooks.apply(hooks.plan_install("claude", data), data)
    cfg = json.loads(paths["claude"].read_text())
    for g in cfg["hooks"]["Stop"]:
        for h in g["hooks"]:
            h["command"] = "/old/venv/bin/python -m robin.hooks.relay --runtime claude"
    paths["claude"].write_text(json.dumps(cfg, indent=2) + "\n")
    hooks.apply(hooks.plan_install("claude", data), data)  # repairs the moved relay
    hooks.apply(hooks.plan_uninstall("claude", data), data)
    assert paths["claude"].read_text() == original


def test_an_outside_sessions_question_waits_in_attention_until_answered(served):
    from robin.runtime import attention

    store = served
    ask = {
        "session_id": "ext-q",
        "hook_event_name": "PreToolUse",
        "tool_name": "AskUserQuestion",
        "tool_use_id": "toolu_1",
        "tool_input": {
            "questions": [
                {
                    "question": "How should a workflow appear?",
                    "header": "Workflows",
                    "multiSelect": False,
                    "options": [{"label": "Card + its agents", "description": "Agents under it"}],
                }
            ]
        },
    }
    body = {"relay_id": "q1", "relayed_at": time.time(), "payload": ask}
    hook_ingest.record(store, "claude", body)
    hook_ingest.record(store, "claude", body)  # a repeat opens nothing new
    sid = session_by_native(store, "ext-q")["session_id"]
    [item] = attention.list_items(store, session_id=sid)
    assert item["kind"] == "question" and item["title"] == "How should a workflow appear?"
    assert item["detail"]["questions"][0]["options"][0]["label"] == "Card + its agents"
    assert item["allowed_responses"] == []

    answered = {**ask, "hook_event_name": "PostToolUse"}
    hook_ingest.record(store, "claude", {"relay_id": "q2", "relayed_at": time.time(), "payload": answered})
    assert attention.list_items(store, session_id=sid) == []

    hook_ingest.record(
        store,
        "claude",
        {"relay_id": "q3", "relayed_at": time.time(), "payload": {**ask, "tool_use_id": "toolu_2"}},
    )
    hook_ingest.record(
        store, "claude", {"relay_id": "q4", "payload": {"session_id": "ext-q", "hook_event_name": "Stop"}}
    )
    assert attention.list_items(store, session_id=sid) == []  # an unanswered question ends with the turn


# -- Robin's question hook ----------------------------------------------------------------------------------


def ask_groups(path):
    return [
        g
        for g in json.loads(path.read_text())["hooks"].get("PreToolUse", [])
        if any(hooks.is_ask(h) for h in g["hooks"])
    ]


def test_install_adds_the_question_hook_for_ask_user_question_and_uninstall_removes_it(configs):
    paths, data = configs
    original = paths["claude"].read_text()
    hooks.apply(hooks.plan_install("claude", data), data)
    assert ask_groups(paths["claude"]) == [
        {
            "matcher": "AskUserQuestion",
            "hooks": [{"type": "command", "command": hooks.ask_command(data), "timeout": 600}],
        }
    ]
    assert hooks.status("claude", data)["drift"] == []
    hooks.apply(hooks.plan_uninstall("claude", data), data)
    assert paths["claude"].read_text() == original


def test_codex_gets_no_question_hook(configs):
    paths, data = configs
    hooks.apply(hooks.plan_install("codex", data), data)
    assert hooks.ASK_MODULE not in paths["codex"].read_text()
    assert hooks.status("codex", data)["drift"] == []


def test_an_install_from_before_the_question_hook_reports_it_missing_and_reinstall_adds_only_it(configs):
    paths, data = configs
    hooks.apply(hooks.plan_install("claude", data), data)
    # what a relay-only install left: no question hook in the config, none in the record
    cfg = json.loads(paths["claude"].read_text())
    cfg["hooks"]["PreToolUse"] = [
        g for g in cfg["hooks"]["PreToolUse"] if not any(hooks.is_ask(h) for h in g["hooks"])
    ]
    paths["claude"].write_text(json.dumps(cfg, indent=2) + "\n")
    rec_path = data / "hooks" / "installed.json"
    recs = json.loads(rec_path.read_text())
    del recs["claude"]["ask_command"]
    rec_path.write_text(json.dumps(recs, indent=2) + "\n")

    status = hooks.status("claude", data)
    assert status["installed"] and any("question hook is missing" in d for d in status["drift"])
    plan = hooks.plan_install("claude", data)
    assert plan.action == "install"
    before, after = json.loads(plan.before), json.loads(plan.after)
    assert after["hooks"]["PreToolUse"][:-1] == before["hooks"]["PreToolUse"]
    assert hooks.is_ask(after["hooks"]["PreToolUse"][-1]["hooks"][0])
    assert {k: v for k, v in after["hooks"].items() if k != "PreToolUse"} == {
        k: v for k, v in before["hooks"].items() if k != "PreToolUse"
    }
    assert {k: v for k, v in after.items() if k != "hooks"} == {
        k: v for k, v in before.items() if k != "hooks"
    }
    hooks.apply(plan, data)
    assert hooks.status("claude", data)["drift"] == []


def test_a_moved_or_retimed_question_hook_is_drift_and_reinstall_restores_it(configs):
    paths, data = configs
    hooks.apply(hooks.plan_install("claude", data), data)
    cfg = json.loads(paths["claude"].read_text())
    (group,) = [g for g in cfg["hooks"]["PreToolUse"] if any(hooks.is_ask(h) for h in g["hooks"])]
    group["hooks"][0]["timeout"] = 60
    paths["claude"].write_text(json.dumps(cfg, indent=2) + "\n")
    assert any("timeout isn't 600s" in d for d in hooks.status("claude", data)["drift"])

    group["matcher"] = "Bash"
    paths["claude"].write_text(json.dumps(cfg, indent=2) + "\n")
    assert any("isn't on PreToolUse for AskUserQuestion" in d for d in hooks.status("claude", data)["drift"])

    group["matcher"] = "AskUserQuestion"
    group["hooks"][0]["command"] = hooks.ask_command(data).replace(sys.executable, "/old/venv/bin/python")
    paths["claude"].write_text(json.dumps(cfg, indent=2) + "\n")
    assert any("different command" in d for d in hooks.status("claude", data)["drift"])

    group["matcher"] = "Bash"
    group["hooks"][0]["timeout"] = 60
    paths["claude"].write_text(json.dumps(cfg, indent=2) + "\n")
    hooks.apply(hooks.plan_install("claude", data), data)
    assert hooks.status("claude", data)["drift"] == []
    assert len(ask_groups(paths["claude"])) == 1


QUESTION = {
    "session_id": "ext-ask",
    "hook_event_name": "PreToolUse",
    "tool_name": "AskUserQuestion",
    "tool_use_id": "toolu_ask",
    "tool_input": {
        "questions": [
            {
                "question": "Which database?",
                "header": "DB",
                "multiSelect": False,
                "options": [{"label": "SQLite", "description": ""}, {"label": "Postgres", "description": ""}],
            },
            {
                "question": "Which features?",
                "header": "Features",
                "multiSelect": True,
                "options": [{"label": "Auth", "description": ""}, {"label": "Billing", "description": ""}],
            },
        ]
    },
}


def ask_cmd(data):
    return [sys.executable, "-m", hooks.ASK_MODULE, "--data", str(data)]


def run_ask(data, payload, env=None, timeout=10):
    started = time.monotonic()
    p = subprocess.run(
        ask_cmd(data), input=json.dumps(payload).encode(), capture_output=True, timeout=timeout, env=env
    )
    return p, time.monotonic() - started


def start_ask(data, payload):
    p = subprocess.Popen(ask_cmd(data), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    p.stdin.write(json.dumps(payload).encode())
    p.stdin.close()
    return p


def finish(p, timeout):
    p.wait(timeout)
    return p.stdout.read()


def waited_on(store, native="ext-ask", timeout=10):
    """The question the hook registered and now waits on."""
    from robin.runtime import attention

    deadline = time.time() + timeout
    while time.time() < deadline:
        row = session_by_native(store, native)
        items = attention.list_items(store, session_id=row["session_id"]) if row else []
        if items and items[0]["detail"].get("answer_in") == "hook":
            return items[0]
        time.sleep(0.05)
    raise AssertionError("the question hook never registered its question")


def robin_client(store):
    import httpx

    url = json.loads((store.data_dir / "service.json").read_text())["url"]
    token = (store.data_dir / "cli.token").read_text().strip()
    return httpx.Client(base_url=f"{url}/api/v1", headers={"authorization": f"Bearer {token}"}, timeout=10)


def respond(store, item, **body):
    import uuid

    with robin_client(store) as c:
        return c.post(
            f"/attention/{item['attention_id']}/responses", json={"action_id": uuid.uuid4().hex, **body}
        )


def test_question_hook_with_robin_down_exits_zero_silently_and_fast(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "service.json").write_text(json.dumps({"url": f"http://127.0.0.1:{free_port()}"}))
    (data / "cli.token").write_text("x")
    p, took = run_ask(data, QUESTION)
    assert p.returncode == 0 and p.stdout == b"" and p.stderr == b"" and took < 3


def test_question_hook_does_nothing_inside_a_robin_launched_session(served):
    import os

    store = served
    p, took = run_ask(store.data_dir, QUESTION, env={**os.environ, "ROBIN_LAUNCH_TOKEN": "x"})
    assert p.returncode == 0 and p.stdout == b"" and took < 3
    assert session_by_native(store, "ext-ask") is None  # nothing registered


def test_an_answer_in_robin_reaches_the_terminal_session_as_the_tools_answers(served):
    store = served
    p = start_ask(store.data_dir, QUESTION)
    item = waited_on(store)
    r = respond(
        store,
        item,
        decision="answer",
        answers={"Which database?": "Postgres", "Which features?": ["Auth", "Billing"]},
    )
    assert r.status_code == 202, r.text
    out = finish(p, 10)
    assert p.returncode == 0
    hso = json.loads(out)["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse" and hso["permissionDecision"] == "allow"
    assert hso["updatedInput"]["questions"] == QUESTION["tool_input"]["questions"]
    assert hso["updatedInput"]["answers"] == {
        "Which database?": "Postgres",
        "Which features?": "Auth, Billing",
    }


def test_declining_in_robin_denies_the_question_with_the_reason(served):
    store = served
    p = start_ask(store.data_dir, QUESTION)
    r = respond(store, waited_on(store), decision="deny", message="not now")
    assert r.status_code == 202, r.text
    out = finish(p, 10)
    hso = json.loads(out)["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny" and hso["permissionDecisionReason"] == "not now"


def test_answer_in_terminal_lets_the_terminal_ask(served):
    store = served
    p = start_ask(store.data_dir, QUESTION)
    r = respond(store, waited_on(store), decision="terminal")
    assert r.status_code == 202, r.text
    out = finish(p, 10)
    assert p.returncode == 0 and out == b""


def test_when_the_wait_runs_out_the_terminal_asks_and_robin_refuses_a_late_answer(served):
    from robin.runtime import attention

    store = served
    attention.set_question_wait_s(store, 1)
    started = time.monotonic()
    p = start_ask(store.data_dir, QUESTION)
    item = waited_on(store)
    out = finish(p, 15)
    assert p.returncode == 0 and out == b"" and time.monotonic() - started < 10
    r = respond(
        store, item, decision="answer", answers={"Which database?": "SQLite", "Which features?": "Auth"}
    )
    assert r.status_code == 409 and r.json()["error"]["code"] == "answer_in_terminal"


def test_a_zero_wait_turns_the_question_hook_off(served):
    from robin.runtime import attention

    store = served
    attention.set_question_wait_s(store, 0)
    p, took = run_ask(store.data_dir, QUESTION)
    assert p.returncode == 0 and p.stdout == b"" and took < 3
    row = session_by_native(store, "ext-ask")
    assert row is None or attention.list_items(store, session_id=row["session_id"]) == []


def test_stopping_the_question_hook_hands_the_question_back_to_the_terminal(served):
    import signal

    from robin.runtime import attention

    store = served
    p = start_ask(store.data_dir, QUESTION)
    item = waited_on(store)
    p.send_signal(signal.SIGTERM)
    out = finish(p, 10)
    assert p.returncode == 0 and out == b""
    after = attention.get(store, item["attention_id"])
    assert after["state"] == "open" and after["allowed_responses"] == []
    assert after["detail"]["answer_in"] == "terminal"
