from collections.abc import Iterator
from pathlib import Path

import pytest

from robin.store import Store, open_store


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
