"""Live: Robin's hook installers fire in the real harnesses. Runs only with ROBIN_LIVE=1.

The user's own configs are never changed. Claude gets Robin's hooks from a temporary settings file passed
with `--settings`; Pi loads the observer extension with `-e` and keeps its session in a temporary folder.
Codex has no per-run hooks file (it reads ~/.codex/hooks.json), so it isn't exercised here.
"""

import asyncio
import json
import os
import subprocess
import threading
import time

import pytest
from sqlalchemy import select

from robin.events.ingest import read_events
from robin.hooks import install as hooks
from robin.service.main import build, free_port, write_endpoint
from robin.store.models import Session as SessionRow

pytestmark = pytest.mark.skipif(os.environ.get("ROBIN_LIVE") != "1", reason="set ROBIN_LIVE=1 to run")


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


def observed(store, runtime, deadline_s=60.0):
    deadline = time.time() + deadline_s
    while time.time() < deadline:
        with store.reader.connect() as c:
            rows = list(
                c.execute(
                    select(SessionRow).where(SessionRow.runtime == runtime, SessionRow.mode == "observed")
                ).mappings()
            )
        for r in rows:
            types = [e.event_type for e in read_events(store, session_id=r["session_id"])]
            if "session.turn_completed" in types:
                return r, types
        time.sleep(0.5)
    raise AssertionError(f"no hook events from {runtime}")


def child_env():
    env = {k: v for k, v in os.environ.items() if k != "ROBIN_LAUNCH_TOKEN"}
    return env


def test_claude_hooks_fire(served, tmp_path, monkeypatch):
    store = served
    settings = tmp_path / "robin-hooks.json"
    monkeypatch.setenv("ROBIN_HOOK_CONFIG_CLAUDE", str(settings))
    hooks.apply(hooks.plan_install("claude", store.data_dir), store.data_dir)
    work = tmp_path / "work"
    work.mkdir()
    p = subprocess.run(
        ["claude", "-p", "Reply with the single word OK.", "--settings", str(settings)],
        cwd=work,
        env=child_env(),
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert p.returncode == 0, p.stderr[-2000:]
    row, types = observed(store, "claude")
    assert row["execution_cwd"] == str(work.resolve()) or row["execution_cwd"] == str(work)
    assert {"session.started", "session.turn_started", "session.turn_completed"} <= set(types), types
    assert row["runtime_state"] in ("idle", "stopped")


def test_pi_observer_fires(served, tmp_path, monkeypatch):
    store = served
    observer = tmp_path / "ext" / "robin-observer.ts"
    monkeypatch.setenv("ROBIN_HOOK_CONFIG_PI", str(observer))
    hooks.apply(hooks.plan_install("pi", store.data_dir), store.data_dir)
    work = tmp_path / "work"
    work.mkdir()
    p = subprocess.run(
        [
            "pi",
            "-p",
            "Reply with the single word OK.",
            "-e",
            str(observer),
            "--no-tools",
            "--session-dir",
            str(tmp_path / "pi-sessions"),
        ],
        cwd=work,
        env=child_env(),
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert p.returncode == 0, p.stderr[-2000:]
    row, types = observed(store, "pi")
    assert {"session.started", "session.turn_started", "session.turn_completed"} <= set(types), types
    payload = json.loads(next(e for e in read_events(store, session_id=row["session_id"])).model_dump_json())
    assert payload["runtime"] == "pi"
