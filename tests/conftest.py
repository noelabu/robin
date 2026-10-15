import asyncio
import os
import signal
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
    if _alive(pid):
        os.kill(pid, signal.SIGTERM)
        for _ in range(50):
            if not _alive(pid):
                break
            time.sleep(0.05)
        else:
            os.kill(pid, signal.SIGKILL)
    os.system("pkill -f 'robin.runtime.host --runtime fake' >/dev/null 2>&1")
