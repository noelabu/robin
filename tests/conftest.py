import asyncio
import os
import signal
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from robin.store import Store, open_store
from robin.supervisor.client import ensure_supervisor
from robin.supervisor.server import supervisor_dir


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "robin-data"


@pytest.fixture
def store(data_dir: Path) -> Iterator[Store]:
    s = open_store(data_dir)
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def session_id(store: Store) -> str:
    from robin.events.ingest import ensure_session

    return ensure_session(store, runtime="claude", mode="observed")


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def _supervisor_pid(data_dir):
    return int((supervisor_dir(data_dir) / "supervisor.lock").read_text())


@pytest.fixture
def supervised(store, monkeypatch):
    monkeypatch.setenv("ROBIN_FAKE_TICK", "0.05")
    asyncio.run(ensure_supervisor(store.data_dir))
    yield store
    pid = _supervisor_pid(store.data_dir)  # whichever supervisor holds the lock now
    # Only this supervisor's own hosts: a broad pkill would hit other suites running at the same time.
    hosts = subprocess.run(["pgrep", "-P", str(pid)], capture_output=True, text=True).stdout.split()
    if _alive(pid):
        os.kill(pid, signal.SIGTERM)  # the supervisor terminates its hosts on the way out
        for _ in range(50):
            if not _alive(pid):
                break
            time.sleep(0.05)
        else:
            os.kill(pid, signal.SIGKILL)
    for host in map(int, hosts):
        if _alive(host):
            os.kill(host, signal.SIGKILL)


@pytest.fixture
def api(supervised):
    """A running service with a CLI-token client, a registered project and one task."""
    import asyncio
    import threading
    import time
    import uuid

    import httpx

    from robin.service.main import build, free_port

    store = supervised
    port = free_port()
    server, auth = build(store, port)
    thread = threading.Thread(target=lambda: asyncio.run(server.serve()), daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.02)
    c = httpx.Client(
        base_url=f"http://127.0.0.1:{port}/api/v1",
        headers={"authorization": f"Bearer {auth.cli_token}"},
        timeout=10,
    )
    project = store.data_dir.parent / "review-project"
    project.mkdir()
    pid = c.post("/projects", json={"path": str(project)}).json()["project_id"]
    task = c.post("/tasks", json={"project_id": pid, "title": "Review auth", "objective": "Find bugs"}).json()
    yield store, c, task["task_id"], project
    for s in c.get("/workspace").json()["sessions"]:
        c.post(
            f"/sessions/{s['session_id']}/actions",
            json={"type": "stop", "action_id": f"t-{uuid.uuid4().hex}"},
        )
    server.should_exit = True
    thread.join(10)


@pytest.fixture(autouse=True)
def _no_real_harness_configs(tmp_path_factory, monkeypatch):
    """Tests never read or write the user's real Claude, Codex or Pi hook configs."""
    root = tmp_path_factory.mktemp("harness")
    for runtime, name in (("claude", "settings.json"), ("codex", "hooks.json"), ("pi", "robin-observer.ts")):
        monkeypatch.setenv(f"ROBIN_HOOK_CONFIG_{runtime.upper()}", str(root / runtime / name))
