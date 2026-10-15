"""Findings in the artifact store (FR-020, TR-060, TR-064, TR-067).

Agent submissions arrive as committed `finding_submission` events and are projected in the same transaction
as the rest of the session's state. The author is the publishing session's launch identity: runtime, native
session ID and the run in progress, read from Robin's own session row. A repeated submission ID from the
same author returns the stored result. An invalid submission is recorded as rejected and raises a
finding_review attention item. Content is canonical JSON in a blob: an immutable version, never written to a
project or execution folder.
"""

import hashlib
import json
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, func, insert, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from robin.findings import schema
from robin.ids import new_id, utc_now
from robin.store import Store
from robin.store.blobs import BlobStore
from robin.store.models import (
    Artifact,
    ArtifactVersion,
    AttentionItem,
    Blob,
    FindingSubmission,
    LineageEdge,
    Task,
)
from robin.store.models import Session as SessionRow

MEDIA_TYPE = "application/vnd.robin.finding+json"


class FindingError(Exception):
    def __init__(self, code: str, message: str, status: int = 422, errors: list[str] | None = None):
        super().__init__(message)
        self.code, self.status, self.errors = code, status, errors or []


def canonical(doc: dict[str, Any]) -> bytes:
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _put(conn: Connection, blobs: BlobStore, data: bytes, media_type: str = MEDIA_TYPE) -> str:
    sha = blobs.put(data)
    conn.execute(
        sqlite_insert(Blob)
        .values(sha256=sha, size=len(data), media_type=media_type, state="present", created_at=utc_now())
        .on_conflict_do_nothing()
    )
    return sha


# -- agent submissions (projection) ----------------------------------------------------------------------


def project(
    conn: Connection, blobs: BlobStore, session_id: str, cursor: int, at: datetime, ev: dict[str, Any]
) -> None:
    """Turn one committed submission event into a finding or a recorded rejection. Idempotent."""
    path = str(ev["path"]) if ev.get("path") in ("tool", "message") else "message"
    payload = ev.get("payload")
    s = conn.execute(select(SessionRow).where(SessionRow.session_id == session_id)).mappings().first()
    if s is None:
        return
    native_ref = str(ev.get("native_ref") or cursor)
    sub_id = (
        payload["submission_id"]
        if isinstance(payload, dict)
        and isinstance(payload.get("submission_id"), str)
        and payload["submission_id"]
        else "auto-" + hashlib.sha256(f"{session_id}:{native_ref}".encode()).hexdigest()[:16]
    )[:128]
    author_key = f"session:{session_id}"
    if conn.execute(
        select(FindingSubmission.submission_row_id).where(
            FindingSubmission.author_key == author_key, FindingSubmission.submission_id == sub_id
        )
    ).first():
        return  # a retry: the stored result stands
    task_id = s["current_task_id"]
    errors = schema.validate(payload)
    if task_id is None:
        errors.append("this session isn't working on a task; findings belong to a task")
    parents = _parents(conn, payload, task_id, errors) if not errors else []
    if errors:
        conn.execute(
            insert(FindingSubmission).values(
                submission_row_id=new_id(),
                author_key=author_key,
                submission_id=sub_id,
                session_id=session_id,
                task_id=task_id,
                path=path,
                state="rejected",
                errors=json.dumps(errors),
                event_cursor=cursor,
                created_at=at,
            )
        )
        conn.execute(
            insert(AttentionItem).values(
                attention_id=new_id(),
                kind="finding_review",
                session_id=session_id,
                task_id=task_id,
                state="open",
                title=f"A finding from {s['runtime']} was rejected",
                detail=json.dumps({"errors": errors, "submission_id": sub_id, "path": path}),
                allowed_responses=json.dumps([]),
                originating_cursor=cursor,
                created_at=at,
                updated_at=at,
            )
        )
        return
    assert isinstance(payload, dict) and task_id is not None
    author = {
        "kind": "agent",
        "runtime": s["runtime"],
        "native_session_id": s["native_session_id"],
        "run_id": s["active_run_id"],
    }
    artifact_id = new_id()
    _create(
        conn,
        blobs,
        artifact_id=artifact_id,
        task_id=task_id,
        author=author,
        session_id=session_id,
        submission_id=sub_id,
        body=schema.content(payload),
        parents=parents,
        path=path,
        ignored=schema.ignored_fields(payload),
        at=at,
    )
    conn.execute(
        insert(FindingSubmission).values(
            submission_row_id=new_id(),
            author_key=author_key,
            submission_id=sub_id,
            session_id=session_id,
            task_id=task_id,
            path=path,
            state="accepted",
            errors="[]",
            artifact_id=artifact_id,
            version=1,
            event_cursor=cursor,
            created_at=at,
        )
    )


def _parents(conn: Connection, payload: Any, task_id: str | None, errors: list[str]) -> list[tuple[str, int]]:
    out = []
    for pid in payload.get("parents") or []:
        row = conn.execute(
            select(Artifact.latest_version, Artifact.task_id).where(Artifact.artifact_id == pid)
        ).first()
        if row is None or row.task_id != task_id:
            errors.append(f"parent {pid} isn't an artifact of this task")
        else:
            out.append((pid, int(row.latest_version)))
    return out


def _create(
    conn: Connection,
    blobs: BlobStore,
    *,
    artifact_id: str,
    task_id: str,
    author: dict[str, Any],
    session_id: str | None,
    submission_id: str,
    body: dict[str, Any],
    parents: list[tuple[str, int]],
    path: str,
    ignored: list[str],
    at: datetime,
    version: int = 1,
) -> dict[str, Any]:
    doc = {
        "schema_version": schema.SCHEMA_VERSION,
        "finding_id": artifact_id,
        "version": version,
        "submission_id": submission_id,
        "task_id": task_id,
        "author": author,
        "created_at": at.isoformat(),
        **body,
        "parents": [{"artifact_id": p, "version": v} for p, v in parents],
        "publication_path": path,
        "ignored_fields": ignored,
    }
    data = canonical(doc)
    sha = _put(conn, blobs, data)
    if version == 1:
        conn.execute(
            insert(Artifact).values(
                artifact_id=artifact_id,
                kind="finding",
                task_id=task_id,
                author_kind=author["kind"],
                author_session_id=session_id,
                author_runtime=author.get("runtime"),
                author_native_session_id=author.get("native_session_id"),
                author_run_id=author.get("run_id"),
                latest_version=1,
                created_at=at,
                updated_at=at,
            )
        )
    else:
        conn.execute(
            update(Artifact)
            .where(Artifact.artifact_id == artifact_id)
            .values(latest_version=version, updated_at=at)
        )
    conn.execute(
        insert(ArtifactVersion).values(
            artifact_id=artifact_id,
            version=version,
            schema_version=schema.SCHEMA_VERSION,
            content_blob=sha,
            size=len(data),
            submission_id=submission_id,
            created_at=at,
        )
    )
    for pid, pver in parents:
        conn.execute(
            insert(LineageEdge).values(
                child_artifact_id=artifact_id,
                child_version=version,
                parent_artifact_id=pid,
                parent_version=pver,
                relation="parent",
            )
        )
    return doc


# -- user findings -------------------------------------------------------------------------------------------


def publish_user(store: Store, task_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """A finding the user writes. Invalid input is refused outright (the user is right here to fix it)."""
    errors = schema.validate(payload)
    if errors:
        raise FindingError("invalid_finding", "; ".join(errors), errors=errors)
    sub_id = payload.get("submission_id") or "user-" + hashlib.sha256(canonical(payload)).hexdigest()[:16]

    def work(conn: Connection) -> str:
        done = conn.execute(
            select(FindingSubmission.artifact_id).where(
                FindingSubmission.author_key == "user", FindingSubmission.submission_id == sub_id
            )
        ).scalar()
        if done:
            return str(done)  # the same submission again: the stored finding
        if conn.execute(select(Task.task_id).where(Task.task_id == task_id)).first() is None:
            raise FindingError("not_found", f"no task {task_id}", 404)
        parent_errors: list[str] = []
        parents = _parents(conn, payload, task_id, parent_errors)
        if parent_errors:
            raise FindingError("invalid_finding", "; ".join(parent_errors), errors=parent_errors)
        artifact_id = new_id()
        now = utc_now()
        _create(
            conn,
            store.blobs,
            artifact_id=artifact_id,
            task_id=task_id,
            author={"kind": "user"},
            session_id=None,
            submission_id=sub_id,
            body=schema.content(payload),
            parents=parents,
            path="user",
            ignored=schema.ignored_fields(payload),
            at=now,
        )
        conn.execute(
            insert(FindingSubmission).values(
                submission_row_id=new_id(),
                author_key="user",
                submission_id=sub_id,
                task_id=task_id,
                path="user",
                state="accepted",
                errors="[]",
                artifact_id=artifact_id,
                version=1,
                created_at=now,
            )
        )
        return artifact_id

    return get(store, store.writer.call(work))


def revise(store: Store, artifact_id: str, payload: dict[str, Any], expected_version: int) -> dict[str, Any]:
    """A new immutable version of a user-authored finding. Agent findings are revised by publishing anew."""
    errors = schema.validate(payload)
    if errors:
        raise FindingError("invalid_finding", "; ".join(errors), errors=errors)

    def work(conn: Connection) -> None:
        a = conn.execute(select(Artifact).where(Artifact.artifact_id == artifact_id)).mappings().first()
        if a is None or a["kind"] != "finding":
            raise FindingError("not_found", f"no finding {artifact_id}", 404)
        if a["author_kind"] != "user":
            raise FindingError(
                "not_editable", "an agent's finding can't be edited; it stays as the agent wrote it", 409
            )
        if a["latest_version"] != expected_version:
            raise FindingError("stale_version", f"the finding is at version {a['latest_version']}", 409)
        parent_errors: list[str] = []
        parents = _parents(conn, payload, a["task_id"], parent_errors)
        if parent_errors:
            raise FindingError("invalid_finding", "; ".join(parent_errors), errors=parent_errors)
        _create(
            conn,
            store.blobs,
            artifact_id=artifact_id,
            task_id=a["task_id"],
            author={"kind": "user"},
            session_id=None,
            submission_id=str(payload.get("submission_id") or f"user-v{expected_version + 1}"),
            body=schema.content(payload),
            parents=parents,
            path="user",
            ignored=schema.ignored_fields(payload),
            at=utc_now(),
            version=expected_version + 1,
        )

    store.writer.call(work)
    return get(store, artifact_id)


# -- reads ---------------------------------------------------------------------------------------------------


def get(store: Store, artifact_id: str, version: int | None = None) -> dict[str, Any]:
    with store.reader.connect() as conn:
        a = conn.execute(select(Artifact).where(Artifact.artifact_id == artifact_id)).mappings().first()
        if a is None or a["kind"] != "finding":
            raise FindingError("not_found", f"no finding {artifact_id}", 404)
        v = version or a["latest_version"]
        row = (
            conn.execute(
                select(ArtifactVersion).where(
                    ArtifactVersion.artifact_id == artifact_id, ArtifactVersion.version == v
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise FindingError("not_found", f"finding {artifact_id} has no version {v}", 404)
        versions = list(
            conn.execute(
                select(ArtifactVersion.version)
                .where(ArtifactVersion.artifact_id == artifact_id)
                .order_by(ArtifactVersion.version)
            ).scalars()
        )
        children = list(
            conn.execute(
                select(LineageEdge.child_artifact_id, LineageEdge.child_version, LineageEdge.relation).where(
                    LineageEdge.parent_artifact_id == artifact_id
                )
            ).mappings()
        )
    doc = json.loads(store.blobs.read(row["content_blob"]))
    return {
        **doc,
        "content_hash": f"sha256:{row['content_blob']}",
        "latest_version": a["latest_version"],
        "versions": versions,
        "derived": [dict(c) for c in children],
    }


def list_findings(store: Store, task_id: str | None = None) -> list[dict[str, Any]]:
    q = (
        select(Artifact, ArtifactVersion.content_blob)
        .join(
            ArtifactVersion,
            (ArtifactVersion.artifact_id == Artifact.artifact_id)
            & (ArtifactVersion.version == Artifact.latest_version),
        )
        .where(Artifact.kind == "finding")
        .order_by(Artifact.created_at)
    )
    if task_id:
        q = q.where(Artifact.task_id == task_id)
    with store.reader.connect() as conn:
        rows = list(conn.execute(q).mappings())
    out = []
    for r in rows:
        doc = json.loads(store.blobs.read(r["content_blob"]))
        out.append(
            {
                "finding_id": r["artifact_id"],
                "version": r["latest_version"],
                "task_id": r["task_id"],
                "author": doc["author"],
                "summary": doc["summary"],
                "claims": len(doc["claims"]),
                "unsupported_claims": sum(1 for c in doc["claims"] if c.get("unsupported")),
                "publication_path": doc["publication_path"],
                "created_at": doc["created_at"],
                "content_hash": f"sha256:{r['content_blob']}",
            }
        )
    return out


def list_rejections(store: Store, task_id: str | None = None) -> list[dict[str, Any]]:
    q = (
        select(FindingSubmission)
        .where(FindingSubmission.state == "rejected")
        .order_by(FindingSubmission.created_at)
    )
    if task_id:
        q = q.where(FindingSubmission.task_id == task_id)
    with store.reader.connect() as conn:
        return [
            {
                "submission_id": r["submission_id"],
                "session_id": r["session_id"],
                "task_id": r["task_id"],
                "path": r["path"],
                "errors": json.loads(r["errors"]),
                "created_at": r["created_at"].isoformat(),
            }
            for r in conn.execute(q).mappings()
        ]


def count(conn: Connection, task_id: str) -> int:
    return int(
        conn.execute(
            select(func.count())
            .select_from(Artifact)
            .where(Artifact.task_id == task_id, Artifact.kind == "finding")
        ).scalar()
        or 0
    )
