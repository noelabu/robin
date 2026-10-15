"""Content-addressed files in Robin's artifact store (TR-022)."""

import hashlib
import os
import re
import secrets
from pathlib import Path

from robin.paths import FILE_MODE, secure_mkdir

SHA256 = re.compile(r"[0-9a-f]{64}")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class BlobStore:
    """Files named by their sha256 under root/sha256/ab/cd/. Writes are atomic and idempotent."""

    def __init__(self, root: Path):
        self.root = secure_mkdir(root)

    def path_for(self, sha: str) -> Path:
        # A hash read back from a database (a restored backup, say) must never name a path outside the store.
        if not SHA256.fullmatch(sha):
            raise ValueError("not a sha256 blob hash")
        return self.root / "sha256" / sha[:2] / sha[2:4] / sha

    def put(self, data: bytes) -> str:
        """Write to a temp sibling, fsync, rename, fsync the directory. Metadata is committed afterwards."""
        sha = sha256_hex(data)
        final = self.path_for(sha)
        if final.exists():
            return sha
        secure_mkdir(final.parent)
        tmp = final.parent / f".{sha}.tmp-{secrets.token_hex(4)}"
        fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, FILE_MODE)
        try:
            _write_all(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, final)
        _fsync_dir(final.parent)
        return sha

    def read(self, sha: str) -> bytes:
        return self.path_for(sha).read_bytes()

    def exists(self, sha: str) -> bool:
        return self.path_for(sha).exists()

    def files(self) -> list[Path]:
        base = self.root / "sha256"
        return [p for p in base.rglob("*") if p.is_file()] if base.exists() else []

    def reconcile(self, referenced: set[str]) -> tuple[int, list[str]]:
        """Remove temp files and unreferenced blobs. Returns (files removed, referenced shas missing on disk).

        An unreferenced complete file comes from a crash between rename and commit; nothing points at it,
        and the source checkpoint never advanced, so re-ingestion recreates it.
        """
        removed = 0
        on_disk = set()
        for path in self.files():
            if path.name.startswith(".") and ".tmp-" in path.name:
                path.unlink(missing_ok=True)
                removed += 1
            elif path.name in referenced:
                on_disk.add(path.name)
            else:
                path.unlink(missing_ok=True)
                removed += 1
        return removed, sorted(referenced - on_disk)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]
