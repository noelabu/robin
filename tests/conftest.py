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
