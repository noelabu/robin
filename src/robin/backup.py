"""Backup, export and restore (FR-043, TR-073, TR-074).

A backup is a consistent database copy (SQLite's online backup API, so it can run while Robin is in use)
plus every blob that copy references, each verified against its hash, and a manifest. Credentials never go
in: not the CLI token, not the service endpoint, not session credentials (only their hashes are stored).
Restore only goes into an empty data directory, checks every file against the manifest, opens the store
(running any migrations), and then checks every finding, synthesis and packet against its content hash.

An export is readable output for people: tasks, findings, syntheses and approved packets as JSON and
Markdown, with relative paths and hashes. Native transcripts are listed as references, never copied.
"""

import hashlib
import json
import os
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from robin import __version__
from robin.paths import secure_mkdir
from robin.store.blobs import BlobStore
from robin.store.migrate import backup as sqlite_backup
from robin.store.migrate import current_revision

FORMAT = 1
NEVER_COPIED = ("cli.token", "service.json", "auth.json", "supervisor.sock")


class BackupError(Exception):
    pass


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _blob_store(data_dir: Path) -> BlobStore:
    return BlobStore((data_dir / "artifacts").resolve())


def create(data_dir: Path, dest_parent: Path) -> Path:
    """Write `<dest_parent>/robin-backup-<time>/`: robin.db, the blobs it references, manifest.json."""
    db = data_dir / "robin.db"
    if not db.exists():
        raise BackupError(f"no Robin database in {data_dir}")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = secure_mkdir(dest_parent / f"robin-backup-{stamp}")
    snapshot = sqlite_backup(db, out, "snapshot")  # verified with quick_check and fsynced
    final_db = out / "robin.db"
    os.replace(snapshot, final_db)
    with sqlite3.connect(f"file:{final_db}?mode=ro", uri=True) as conn:
        shas = [r[0] for r in conn.execute("SELECT sha256 FROM blob WHERE state = 'present'")]
    blobs = _blob_store(data_dir)
    copied, missing = [], []
    for sha in shas:
        src = blobs.path_for(sha)
        if not src.exists() or _sha(src) != sha:
            missing.append(sha)  # recorded, never silently dropped
            continue
        dst = out / "blobs" / sha[:2] / sha[2:4] / sha
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        copied.append(sha)
    manifest = {
        "format": FORMAT,
        "robin_version": __version__,
        "schema_revision": current_revision(final_db),
        "created_at": datetime.now(UTC).isoformat(),
        "database": {"file": "robin.db", "sha256": _sha(final_db)},
        "blobs": {"count": len(copied), "missing": missing},
        "excluded": list(NEVER_COPIED),
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return out


def restore(backup_dir: Path, data_dir: Path) -> dict[str, Any]:
    """Restore into a new, empty data directory and verify what came back.

    Everything is checked before anything is written, and the restore is built in a staging folder next to
    the target and moved into place only once it opens and verifies, so a failure leaves nothing behind.
    """
    manifest_path = backup_dir / "manifest.json"
    if not manifest_path.exists():
        raise BackupError(f"{backup_dir} isn't a Robin backup (no manifest.json)")
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("format") != FORMAT:
        raise BackupError(f"unsupported backup format {manifest.get('format')}")
    if data_dir.exists() and any(data_dir.iterdir()):
        raise BackupError(f"{data_dir} isn't empty; restore only into a new data directory")
    src_db = backup_dir / "robin.db"
    if not src_db.is_file():
        raise BackupError(f"{backup_dir} has no robin.db")
    if _sha(src_db) != manifest["database"]["sha256"]:
        raise BackupError("the backup's database doesn't match its manifest")
    blob_files = (
        [p for p in (backup_dir / "blobs").rglob("*") if p.is_file()]
        if (backup_dir / "blobs").exists()
        else []
    )
    for path in blob_files:
        if _sha(path) != path.name:
            raise BackupError(f"blob {path.name} doesn't match its hash")

    data_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = data_dir.parent / f".{data_dir.name}.restoring-{os.getpid()}"
    try:
        secure_mkdir(staging)
        shutil.copyfile(src_db, staging / "robin.db")
        os.chmod(staging / "robin.db", 0o600)
        blobs = _blob_store(staging)
        for path in blob_files:
            dst = blobs.path_for(path.name)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, dst)

        from robin.store import open_store

        store = open_store(staging)  # migrations, if the backup is older than this build
        try:
            checked = verify(store)
        finally:
            store.close()
        if data_dir.exists():
            data_dir.rmdir()  # empty, checked above
        os.replace(staging, data_dir)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return {"restored_blobs": len(blob_files), "missing_in_backup": manifest["blobs"]["missing"], **checked}


def verify(store: Any) -> dict[str, Any]:
    """Every artifact version and approved packet still matches its content hash."""
    from sqlalchemy import select

    from robin.store.models import ArtifactVersion, ContextPacket

    bad: list[str] = []
    with store.reader.connect() as conn:
        versions = conn.execute(
            select(ArtifactVersion.artifact_id, ArtifactVersion.version, ArtifactVersion.content_blob)
        ).all()
        packets = conn.execute(
            select(ContextPacket.packet_id, ContextPacket.payload_blob, ContextPacket.payload_hash)
        ).all()
    for aid, v, sha in versions:
        path = store.blobs.path_for(sha)
        if not path.exists() or _sha(path) != sha:
            bad.append(f"{aid} v{v}")
    for pid, sha, payload_hash in packets:
        path = store.blobs.path_for(sha)
        if not path.exists() or "sha256:" + _sha(path) != payload_hash:
            bad.append(f"packet {pid}")
    return {"artifacts_checked": len(versions), "packets_checked": len(packets), "mismatches": bad}


# -- export -------------------------------------------------------------------------------------------------


def export(store: Any, out: Path, task_id: str | None = None) -> dict[str, Any]:
    """Readable export with relative paths and hashes. No credentials; transcripts are references only."""
    from sqlalchemy import select

    from robin.store.models import Artifact, ArtifactVersion, ContextPacket, Project, Task
    from robin.store.models import Session as SessionRow

    secure_mkdir(out)
    with store.reader.connect() as conn:
        tq = select(Task)
        if task_id:
            tq = tq.where(Task.task_id == task_id)
        tasks = [dict(t) for t in conn.execute(tq).mappings()]
        if task_id and not tasks:
            raise BackupError(f"no task {task_id}")
        ids = [t["task_id"] for t in tasks]
        projects = {p["project_id"]: dict(p) for p in conn.execute(select(Project)).mappings()}
        artifacts = [
            dict(a)
            for a in conn.execute(
                select(Artifact, ArtifactVersion.content_blob, ArtifactVersion.version)
                .join(ArtifactVersion, ArtifactVersion.artifact_id == Artifact.artifact_id)
                .where(Artifact.task_id.in_(ids))
                .order_by(Artifact.created_at, ArtifactVersion.version)
            ).mappings()
        ]
        packets = [
            dict(p)
            for p in conn.execute(
                select(ContextPacket).where(
                    ContextPacket.task_id.in_(ids), ContextPacket.state.in_(("confirmed", "delivered"))
                )
            ).mappings()
        ]
        sessions = [
            dict(s)
            for s in conn.execute(
                select(
                    SessionRow.session_id,
                    SessionRow.runtime,
                    SessionRow.native_session_id,
                    SessionRow.mode,
                    SessionRow.current_task_id,
                ).where(SessionRow.current_task_id.in_(ids))
            ).mappings()
        ]
    index: dict[str, Any] = {"format": FORMAT, "exported_at": datetime.now(UTC).isoformat(), "tasks": []}
    for t in tasks:
        tdir = out / "tasks" / t["task_id"]
        (tdir / "artifacts").mkdir(parents=True, exist_ok=True)
        project = projects.get(t["project_id"], {})
        entry: dict[str, Any] = {
            "task_id": t["task_id"],
            "title": t["title"],
            "objective": t["objective"],
            "status": t["status"],
            "project": project.get("name"),
            "sessions": [  # references to native transcripts, never copies
                {"runtime": s["runtime"], "native_session_id": s["native_session_id"], "mode": s["mode"]}
                for s in sessions
                if s["current_task_id"] == t["task_id"]
            ],
            "artifacts": [],
            "packets": [],
        }
        md = [f"# {t['title']}", "", t["objective"], ""]
        # Approved packets are plain text and exported with their manifests below.
        for a in (a for a in artifacts if a["task_id"] == t["task_id"] and a["kind"] != "packet"):
            raw = store.blobs.read(a["content_blob"])
            name = f"{a['kind']}-{a['artifact_id']}-v{a['version']}.json"
            (tdir / "artifacts" / name).write_bytes(raw)
            entry["artifacts"].append(
                {
                    "kind": a["kind"],
                    "artifact_id": a["artifact_id"],
                    "version": a["version"],
                    "file": f"artifacts/{name}",
                    "sha256": a["content_blob"],
                }
            )
            doc = json.loads(raw)
            if a["kind"] == "finding":
                md += [f"## Finding {a['artifact_id']} v{a['version']}", "", doc["summary"], ""]
                md += [
                    f"- {c['text']}" + (" _(unsupported)_" if c.get("unsupported") else "")
                    for c in doc["claims"]
                ]
                md.append("")
            elif a["kind"] == "synthesis":
                md += [
                    f"## {doc.get('title', 'Synthesis')}",
                    "",
                    f"_{doc.get('label', '')}_",
                    "",
                    doc.get("text", ""),
                    "",
                ]
        for p in (p for p in packets if p["task_id"] == t["task_id"]):
            name = f"packet-{p['packet_id']}.txt"
            (tdir / "artifacts" / name).write_bytes(store.blobs.read(p["payload_blob"]))
            entry["packets"].append(
                {
                    "packet_id": p["packet_id"],
                    "file": f"artifacts/{name}",
                    "payload_hash": p["payload_hash"],
                    "manifest": json.loads(p["manifest"]),
                }
            )
        (tdir / "task.json").write_text(json.dumps(entry, indent=2, default=str) + "\n")
        (tdir / "README.md").write_text("\n".join(md) + "\n")
        index["tasks"].append({"task_id": t["task_id"], "dir": f"tasks/{t['task_id']}"})
    (out / "index.json").write_text(json.dumps(index, indent=2) + "\n")
    return {"tasks": len(tasks), "artifacts": len(artifacts), "packets": len(packets), "out": str(out)}


class ReadOnlyStore:
    """Reads a data directory without taking the store's lock, so export works while Robin is running."""

    def __init__(self, data_dir: Path):
        from sqlalchemy import create_engine
        from sqlalchemy.pool import NullPool

        db = data_dir / "robin.db"
        if not db.exists():
            raise BackupError(f"no Robin database in {data_dir}")
        self.data_dir = data_dir
        self.blobs = _blob_store(data_dir)
        self.reader = create_engine(f"sqlite:///file:{db}?mode=ro&uri=true", poolclass=NullPool)

    def close(self) -> None:
        self.reader.dispose()
