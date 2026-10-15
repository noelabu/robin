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
    assert commands == {hooks.relay_command("claude", data)}


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
    assert "robin.hooks.relay" in out and "never approve" in out
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
