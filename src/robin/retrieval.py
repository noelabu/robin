"""Agent retrieval through Robin (FR-022, FR-023, TR-066, TR-068).

A managed session's tools authenticate with a per-session credential minted at launch; Robin keeps only its
hash. The credential identifies the session and nothing more: an agent can read its task, its own artifacts,
and what the user granted it (exact artifact versions or exact excerpt ranges), never another session's work
by default. Grants never widen and can be revoked. Every read records the exact content returned.
"""

import hashlib
import json
import secrets
from typing import Any

from sqlalchemy import Connection, insert, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from robin.context import lineage
from robin.context.packets import EXCERPT_LIMIT, EXCERPT_TYPES, _event_text
from robin.events.ingest import read_payload_json
from robin.findings.store import _put, canonical
from robin.ids import new_id, utc_now
from robin.redact.scanner import for_data_dir
from robin.store import Store
from robin.store.models import (
    AgentCredential,
    Artifact,
    ArtifactVersion,
    AuditEntry,
    Event,
    LineageEdge,
    RetrievalGrant,
    RetrievalRead,
    Task,
    TaskAttachment,
)
from robin.store.models import Session as SessionRow

TOKEN_PREFIX = "robin_agent_"
SEARCH_LIMIT = 100  # TR-062: results per page


class RetrievalError(Exception):
    def __init__(self, code: str, message: str, status: int = 403):
        super().__init__(message)
        self.code, self.status = code, status


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# -- credentials ------------------------------------------------------------------------------------------


def mint(store: Store, session_id: str) -> str:
    """A fresh credential for a session's tools; any earlier one stops working."""
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    now = utc_now()
    store.writer.call(
        lambda c: c.execute(
            sqlite_insert(AgentCredential)
            .values(session_id=session_id, token_hash=_hash(token), created_at=now)
            .on_conflict_do_update(
                index_elements=["session_id"], set_={"token_hash": _hash(token), "created_at": now}
            )
        )
    )
    return token


def session_for(store: Store, token: str) -> str | None:
    if not token.startswith(TOKEN_PREFIX):
        return None
    with store.reader.connect() as conn:
        sid = conn.execute(
            select(AgentCredential.session_id).where(AgentCredential.token_hash == _hash(token))
        ).scalar()
    return str(sid) if sid else None


# -- grants (the user) ------------------------------------------------------------------------------------


def grant(
    store: Store,
    session_id: str,
    *,
    artifacts: tuple[tuple[str, int | None], ...] = (),
    excerpts: tuple[tuple[str, int, int], ...] = (),
    allow_loops: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    """Let one session read exact artifact versions and excerpt ranges of its task. An audited user action.

    A source built on the session's own output, or its own transcript, is refused unless named in
    `allow_loops` (FR-025): handing an agent its own work back reads to it as independent confirmation.
    The session's own artifacts themselves need no grant; it can already read them.
    """
    now = utc_now()

    def work(conn: Connection) -> list[str]:
        s = conn.execute(select(SessionRow).where(SessionRow.session_id == session_id)).mappings().first()
        if s is None:
            raise RetrievalError("not_found", f"no session {session_id}", 404)
        task_id = s["current_task_id"]
        if task_id is None:
            raise RetrievalError("no_task", "grants belong to a task; this session isn't working on one", 409)
        audit_id = new_id()
        rows = []
        for aid, version in artifacts:
            a = conn.execute(select(Artifact).where(Artifact.artifact_id == aid)).mappings().first()
            if a is None or a["task_id"] != task_id:
                raise RetrievalError("not_found", f"task {task_id} has no artifact {aid}", 404)
            v = version or a["latest_version"]  # pinned: a grant never moves to a newer version
            if (
                conn.execute(
                    select(ArtifactVersion.version).where(
                        ArtifactVersion.artifact_id == aid, ArtifactVersion.version == v
                    )
                ).first()
                is None
            ):
                raise RetrievalError("not_found", f"artifact {aid} has no version {v}", 404)
            found = lineage.own_output(conn, aid, v, session_id)
            if found and not found["direct"] and aid not in allow_loops:
                raise RetrievalError(
                    "context_loop",
                    f"artifact {aid} is {lineage.reason(found).replace('the recipient', 'this session')}; "
                    "allow it explicitly if you want the session to read it anyway",
                    409,
                )
            rows.append({"kind": "artifact", "artifact_id": aid, "version": v})
        for sid, after, through in excerpts:
            if through <= after:
                raise RetrievalError("invalid_request", "an excerpt range needs through > after", 422)
            if (
                conn.execute(select(SessionRow.session_id).where(SessionRow.session_id == sid)).first()
                is None
            ):
                raise RetrievalError("not_found", f"no session {sid}", 404)
            if sid == session_id and sid not in allow_loops:
                raise RetrievalError(
                    "context_loop", "that excerpt is this session's own transcript; allow it explicitly", 409
                )
            rows.append(
                {
                    "kind": "excerpt",
                    "excerpt_session_id": sid,
                    "after_cursor": after,
                    "through_cursor": through,
                }
            )
        if not rows:
            raise RetrievalError("invalid_request", "grant at least one artifact or excerpt", 422)
        conn.execute(
            insert(AuditEntry).values(
                audit_id=audit_id,
                at=now,
                actor="user",
                action="retrieval.grant",
                affected_ids=json.dumps(
                    [session_id, *[r.get("artifact_id") or r.get("excerpt_session_id") for r in rows]]
                ),
                outcome="ok",
            )
        )
        ids = []
        for r in rows:
            gid = new_id()
            conn.execute(
                insert(RetrievalGrant).values(
                    grant_id=gid,
                    session_id=session_id,
                    task_id=task_id,
                    approved_by=audit_id,
                    created_at=now,
                    **r,
                )
            )
            ids.append(gid)
        return ids

    ids = store.writer.call(work)
    return [g for g in list_grants(store, session_id) if g["grant_id"] in ids]


def revoke(store: Store, grant_id: str) -> None:
    now = utc_now()

    def work(conn: Connection) -> None:
        n = conn.execute(
            update(RetrievalGrant)
            .where(RetrievalGrant.grant_id == grant_id, RetrievalGrant.revoked_at.is_(None))
            .values(revoked_at=now)
        ).rowcount
        if not n:
            raise RetrievalError("not_found", f"no active grant {grant_id}", 404)
        conn.execute(
            insert(AuditEntry).values(
                audit_id=new_id(),
                at=now,
                actor="user",
                action="retrieval.revoke",
                affected_ids=json.dumps([grant_id]),
                outcome="ok",
            )
        )

    store.writer.call(work)


def list_grants(store: Store, session_id: str, include_revoked: bool = False) -> list[dict[str, Any]]:
    q = (
        select(RetrievalGrant)
        .where(RetrievalGrant.session_id == session_id)
        .order_by(RetrievalGrant.created_at)
    )
    if not include_revoked:
        q = q.where(RetrievalGrant.revoked_at.is_(None))
    with store.reader.connect() as conn:
        return [
            {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in dict(r).items()}
            for r in conn.execute(q).mappings()
        ]


def reads(store: Store, session_id: str) -> list[dict[str, Any]]:
    with store.reader.connect() as conn:
        rows = conn.execute(
            select(RetrievalRead).where(RetrievalRead.session_id == session_id).order_by(RetrievalRead.at)
        ).mappings()
        return [
            {
                "read_id": r["read_id"],
                "tool": r["tool"],
                "request": json.loads(r["request"]),
                "grant_ids": json.loads(r["grant_ids"]),
                "content_hash": f"sha256:{r['content_blob']}",
                "bytes": r["size"],
                "at": r["at"].isoformat(),
            }
            for r in rows
        ]


# -- agent reads ------------------------------------------------------------------------------------------


def _session(conn: Connection, session_id: str) -> Any:
    s = conn.execute(select(SessionRow).where(SessionRow.session_id == session_id)).mappings().first()
    if s is None or s["current_task_id"] is None:
        raise RetrievalError("no_task", "this session isn't working on a task", 409)
    return s


def _active(conn: Connection, session_id: str) -> list[Any]:
    return list(
        conn.execute(
            select(RetrievalGrant).where(
                RetrievalGrant.session_id == session_id, RetrievalGrant.revoked_at.is_(None)
            )
        ).mappings()
    )


def _redact(store: Store, value: Any, found: list[str]) -> Any:
    """Every string an agent gets passes the same secret scanner as a context packet (TR-070)."""
    if isinstance(value, str):
        clean, matches = for_data_dir(store.data_dir).redact(value)
        found += [m.rule_id for m in matches]
        return clean
    if isinstance(value, dict):
        return {k: _redact(store, v, found) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(store, v, found) for v in value]
    return value


def _record(
    store: Store, session_id: str, tool: str, request: dict[str, Any], grants: list[str], result: Any
) -> Any:
    """Every read is redacted, then recorded with the exact content it returned (TR-068)."""
    found: list[str] = []
    result = _redact(store, result, found)
    if isinstance(result, dict):
        result = {**result, "redactions": sorted(set(found))}
    data = canonical(result if isinstance(result, dict) else {"result": result})

    def work(conn: Connection) -> None:
        sha = _put(conn, store.blobs, data, "application/json")
        conn.execute(
            insert(RetrievalRead).values(
                read_id=new_id(),
                session_id=session_id,
                tool=tool,
                request=json.dumps(request),
                grant_ids=json.dumps(sorted(set(grants))),
                content_blob=sha,
                size=len(data),
                at=utc_now(),
            )
        )

    store.writer.call(work)
    return result


def task_get(store: Store, session_id: str) -> dict[str, Any]:
    with store.reader.connect() as conn:
        s = _session(conn, session_id)
        t = conn.execute(select(Task).where(Task.task_id == s["current_task_id"])).mappings().one()
        notes = conn.execute(
            select(TaskAttachment.kind, TaskAttachment.text_value).where(
                TaskAttachment.task_id == t["task_id"], TaskAttachment.kind != "file"
            )
        ).all()
    result = {
        "task_id": t["task_id"],
        "title": t["title"],
        "objective": t["objective"],
        "status": t["status"],
        "instructions": [n.text_value for n in notes if n.kind == "instruction"],
        "expected_outputs": [n.text_value for n in notes if n.kind == "expected_output"],
    }
    return dict(_record(store, session_id, "task_get", {}, [], result))


def _visible(conn: Connection, session_id: str) -> dict[str, tuple[int, str | None]]:
    """artifact id -> (readable version, grant id or None for the session's own work)."""
    out: dict[str, tuple[int, str | None]] = {}
    own = conn.execute(
        select(Artifact.artifact_id, Artifact.latest_version).where(Artifact.author_session_id == session_id)
    ).all()
    for aid, v in own:
        out[aid] = (int(v), None)
    for g in _active(conn, session_id):
        if g["kind"] == "artifact" and g["artifact_id"] not in out:
            out[g["artifact_id"]] = (int(g["version"]), g["grant_id"])
    return out


def finding_list(store: Store, session_id: str) -> list[dict[str, Any]]:
    with store.reader.connect() as conn:
        _session(conn, session_id)
        visible = _visible(conn, session_id)
        rows = conn.execute(
            select(Artifact.artifact_id, Artifact.kind, ArtifactVersion.version, ArtifactVersion.content_blob)
            .join(ArtifactVersion, ArtifactVersion.artifact_id == Artifact.artifact_id)
            .where(Artifact.artifact_id.in_(list(visible)))
        ).all()
    items = []
    for r in rows:
        if visible[r.artifact_id][0] != r.version or r.kind != "finding":
            continue
        doc = json.loads(store.blobs.read(r.content_blob))
        items.append(
            {
                "finding_id": r.artifact_id,
                "version": r.version,
                "summary": doc["summary"],
                "own": visible[r.artifact_id][1] is None,
            }
        )
    grants = [g for _, g in visible.values() if g]
    return list(_record(store, session_id, "finding_list", {}, grants, {"findings": items})["findings"])


def finding_read(store: Store, session_id: str, finding_id: str) -> dict[str, Any]:
    with store.reader.connect() as conn:
        _session(conn, session_id)
        visible = _visible(conn, session_id)
        if finding_id not in visible:
            raise RetrievalError("not_granted", "this finding hasn't been shared with this session")
        version, grant_id = visible[finding_id]
        row = conn.execute(
            select(ArtifactVersion.content_blob).where(
                ArtifactVersion.artifact_id == finding_id, ArtifactVersion.version == version
            )
        ).scalar_one()
    doc = json.loads(store.blobs.read(row))
    return dict(
        _record(
            store, session_id, "finding_read", {"finding_id": finding_id}, [grant_id] if grant_id else [], doc
        )
    )


def _excerpt_grants(conn: Connection, session_id: str, target: str | None = None) -> list[Any]:
    return [
        g
        for g in _active(conn, session_id)
        if g["kind"] == "excerpt" and (target is None or g["excerpt_session_id"] == target)
    ]


def read_excerpt(
    store: Store, session_id: str, source_session: str, after: int, through: int
) -> dict[str, Any]:
    """Messages in a range, only inside a granted range (never wider)."""
    with store.reader.connect() as conn:
        _session(conn, session_id)
        covering = [
            g
            for g in _excerpt_grants(conn, session_id, source_session)
            if g["after_cursor"] <= after and through <= g["through_cursor"]
        ]
        if not covering:
            raise RetrievalError(
                "not_granted", "that range of that session hasn't been shared with this session"
            )
        rows = list(
            conn.execute(
                select(Event.global_cursor, Event.event_type)
                .where(
                    Event.session_id == source_session,
                    Event.global_cursor > after,
                    Event.global_cursor <= through,
                    Event.event_type.in_(EXCERPT_TYPES),
                )
                .order_by(Event.global_cursor)
                .limit(EXCERPT_LIMIT + 1)
            )
        )
    if len(rows) > EXCERPT_LIMIT:
        raise RetrievalError(
            "too_large", f"more than {EXCERPT_LIMIT} messages; ask for a narrower range", 413
        )
    messages = [
        {
            "cursor": r.global_cursor,
            "type": r.event_type,
            "text": _event_text(read_payload_json(store, r.global_cursor)),
        }
        for r in rows
    ]
    result = {"session_id": source_session, "after": after, "through": through, "messages": messages}
    return dict(
        _record(
            store,
            session_id,
            "session_read_excerpt",
            {"session_id": source_session, "after": after, "through": through},
            [covering[0]["grant_id"]],
            result,
        )
    )


def search(store: Store, session_id: str, query: str) -> dict[str, Any]:
    """Substring search over the excerpt ranges granted to this session only (full-text search: Phase 4)."""
    q = query.strip().lower()
    if not q:
        raise RetrievalError("invalid_request", "search for something", 422)
    hits: list[dict[str, Any]] = []
    used: set[str] = set()
    with store.reader.connect() as conn:
        _session(conn, session_id)
        grants = _excerpt_grants(conn, session_id)
        for g in grants:
            rows = conn.execute(
                select(Event.global_cursor, Event.event_type)
                .where(
                    Event.session_id == g["excerpt_session_id"],
                    Event.global_cursor > g["after_cursor"],
                    Event.global_cursor <= g["through_cursor"],
                    Event.event_type.in_(EXCERPT_TYPES),
                )
                .order_by(Event.global_cursor)
            ).all()
            for r in rows:
                # Searched after redaction, so a query can't probe for a secret.
                text = _redact(store, _event_text(read_payload_json(store, r.global_cursor)), [])
                at = text.lower().find(q)
                if at < 0:
                    continue
                used.add(g["grant_id"])
                hits.append(
                    {
                        "session_id": g["excerpt_session_id"],
                        "cursor": r.global_cursor,
                        "type": r.event_type,
                        "snippet": text[max(0, at - 80) : at + len(q) + 80],
                    }
                )
                if len(hits) >= SEARCH_LIMIT:
                    break
            if len(hits) >= SEARCH_LIMIT:
                break
    result = {"query": query, "results": hits, "truncated": len(hits) >= SEARCH_LIMIT}
    return dict(_record(store, session_id, "session_search", {"query": query}, sorted(used), result))


def artifact_publish(
    store: Store, session_id: str, title: str, text: str, inputs: list[str]
) -> dict[str, Any]:
    """A synthesis or other written result from this session, linked to the inputs it used (TR-064).

    Inputs must be artifacts the session can read; new claims in it need their own evidence (TR-065)."""
    if not title.strip() or not text.strip():
        raise RetrievalError("invalid_request", "an artifact needs a title and text", 422)
    now = utc_now()

    def work(conn: Connection) -> str:
        s = _session(conn, session_id)
        visible = _visible(conn, session_id)
        unknown = [i for i in inputs if i not in visible]
        if unknown:
            raise RetrievalError("not_granted", f"inputs not shared with this session: {', '.join(unknown)}")
        doc = {
            "schema_version": 1,
            "kind": "synthesis",
            "title": title,
            "text": text,
            "task_id": s["current_task_id"],
            "author": {
                "kind": "agent",
                "runtime": s["runtime"],
                "native_session_id": s["native_session_id"],
                "run_id": s["active_run_id"],
            },
            "inputs": [{"artifact_id": i, "version": visible[i][0]} for i in inputs],
            "created_at": now.isoformat(),
            "label": "synthesis: derived from its inputs",
        }
        data = canonical(doc)
        sha = _put(conn, store.blobs, data)
        aid = new_id()
        conn.execute(
            insert(Artifact).values(
                artifact_id=aid,
                kind="synthesis",
                task_id=s["current_task_id"],
                author_kind="agent",
                author_session_id=session_id,
                author_runtime=s["runtime"],
                author_native_session_id=s["native_session_id"],
                author_run_id=s["active_run_id"],
                latest_version=1,
                created_at=now,
                updated_at=now,
            )
        )
        conn.execute(
            insert(ArtifactVersion).values(
                artifact_id=aid, version=1, schema_version=1, content_blob=sha, size=len(data), created_at=now
            )
        )
        for i in dict.fromkeys(inputs):
            conn.execute(
                insert(LineageEdge).values(
                    child_artifact_id=aid,
                    child_version=1,
                    parent_artifact_id=i,
                    parent_version=visible[i][0],
                    relation="input",
                )
            )
        return aid

    return {"artifact_id": store.writer.call(work), "version": 1}
