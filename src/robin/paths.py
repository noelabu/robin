"""Robin's data directory and the path types that must never be mixed up (TR-021, TR-070)."""

import os
from dataclasses import dataclass
from pathlib import Path

DIR_MODE = 0o700
FILE_MODE = 0o600


def default_data_dir() -> Path:
    override = os.environ.get("ROBIN_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return Path.home() / "Library" / "Application Support" / "Robin"


def secure_mkdir(path: Path) -> Path:
    """Create a directory (and parents) readable only by the current user; repair a looser mode."""
    path.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
    if path.stat().st_mode & 0o777 != DIR_MODE:
        path.chmod(DIR_MODE)
    return path


def secure_touch(path: Path) -> Path:
    """Create a file with mode 0600 if it doesn't exist; repair a looser mode if it does."""
    fd = os.open(path, os.O_CREAT | os.O_WRONLY, FILE_MODE)
    os.close(fd)
    if path.stat().st_mode & 0o777 != FILE_MODE:
        path.chmod(FILE_MODE)
    return path


@dataclass(frozen=True)
class ProjectRoot:
    """A registered project's directory."""

    path: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", self.path.expanduser().resolve())


@dataclass(frozen=True)
class ArtifactStoreRoot:
    """Robin's own store for task artifacts and payloads. Never an execution directory."""

    path: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", self.path.expanduser().resolve())


@dataclass(frozen=True)
class ExecutionCwd:
    """Where a runtime runs: the project root or a selected worktree (TR-021)."""

    path: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", self.path.expanduser().resolve())

    @classmethod
    def checked(cls, path: Path, artifact_store: ArtifactStoreRoot) -> "ExecutionCwd":
        cwd = cls(path)
        if cwd.path.is_relative_to(artifact_store.path):
            raise ValueError(f"execution directory {cwd.path} is inside Robin's artifact store")
        if artifact_store.path.is_relative_to(cwd.path):
            raise ValueError(
                f"Robin's artifact store {artifact_store.path} is inside execution directory {cwd.path}"
            )
        return cwd
