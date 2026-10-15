import os
from pathlib import Path

import pytest

from robin.paths import ArtifactStoreRoot, ExecutionCwd, ProjectRoot, default_data_dir, secure_mkdir
from robin.store import Store


def test_data_dir_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("ROBIN_DATA_DIR", str(tmp_path / "custom"))
    assert default_data_dir() == (tmp_path / "custom").resolve()


def test_data_dir_default_is_app_support(monkeypatch):
    monkeypatch.delenv("ROBIN_DATA_DIR", raising=False)
    assert default_data_dir() == Path.home() / "Library" / "Application Support" / "Robin"


def test_dir_0700_files_0600(store: Store):
    assert store.data_dir.stat().st_mode & 0o777 == 0o700
    for name in ("robin.db", "robin.db-wal", "robin.db-shm", "robin.lock"):
        assert (store.data_dir / name).stat().st_mode & 0o777 == 0o600, name
    sha = store.blobs.put(b"x" * 10)
    assert store.blobs.path_for(sha).stat().st_mode & 0o777 == 0o600


def test_secure_mkdir_repairs_loose_mode(tmp_path):
    d = tmp_path / "loose"
    d.mkdir(mode=0o755)
    os.chmod(d, 0o755)
    assert secure_mkdir(d).stat().st_mode & 0o777 == 0o700


def test_path_types_distinct(tmp_path):
    p = tmp_path
    assert ProjectRoot(p) != ExecutionCwd(p)  # same path, different kinds are never equal
    assert ExecutionCwd(p) != ArtifactStoreRoot(p)


def test_execution_cwd_rejects_artifact_store_overlap(tmp_path):
    store = ArtifactStoreRoot(tmp_path / "data" / "artifacts")
    with pytest.raises(ValueError, match="inside Robin's artifact store"):
        ExecutionCwd.checked(tmp_path / "data" / "artifacts" / "x", store)
    with pytest.raises(ValueError, match="is inside execution directory"):
        ExecutionCwd.checked(tmp_path, store)
    assert ExecutionCwd.checked(tmp_path / "project", store).path == (tmp_path / "project").resolve()
