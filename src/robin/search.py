"""Full-text search over Robin's history (FR-040, TR-081, TR-082).

One FTS5 index holds messages, tool calls and output, file references, tasks, findings, syntheses and
session metadata. Text is secret-scanned and masked before it is indexed, so search results and snippets
never carry a secret (TR-070). Events are indexed incrementally behind a watermark, with their global
cursor as the row ID; other documents are re-indexed when they change. `<data dir>/index.toml` can exclude
working folders, event types and tool output from the index.
"""

import fnmatch
import json
import re
import tomllib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, func, insert, select, text, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from robin.context.packets import _event_text
from robin.events.ingest import read_payload_json
from robin.ids import utc_now
from robin.redact.scanner import for_data_dir
from robin.store import Store
from robin.store.models import Artifact, ArtifactVersion, Event, Meta, Project, SearchDoc, Task
from robin.store.models import Session as SessionRow

INDEXED_EVENTS = (
    "message.user",
    "message.completed",
    "tool.started",
    "tool.result",
    "tool.error",
    "tool.output",
    "file.read",
    "file.write",
    "file.change",
)
CURSOR_KEY = "search_cursor"
DOCS_KEY = "search_docs_at"
BATCH = 500
PAGE_LIMIT = 100  # TR-062: results per page
BODY_LIMIT = 64 * 1024  # indexed text per row; the full event stays in the store


@dataclass
class Exclusions:
    paths: list[str] = field(default_factory=list)  # globs for a session's working folder
    event_types: set[str] = field(default_factory=set)
    tool_output: bool = True  # index tool output (results, errors, output)

    @classmethod
    def load(cls, data_dir: Path) -> "Exclusions":
        path = data_dir / "index.toml"
        if not path.is_file():
            return cls()
        raw = tomllib.loads(path.read_text())
        return cls(
            paths=[str(Path(p).expanduser()) for p in raw.get("exclude_paths", [])],
            event_types=set(raw.get("exclude_event_types", [])),
            tool_output=bool(raw.get("index_tool_output", True)),
        )

    def skips_folder(self, cwd: str | None) -> bool:
        return bool(cwd) and any(
            fnmatch.fnmatch(str(cwd), p) or str(cwd).startswith(p.rstrip("*")) for p in self.paths
        )

    def skips_event(self, event_type: str) -> bool:
        if event_type in self.event_types:
            return True
        return not self.tool_output and event_type in ("tool.result", "tool.error", "tool.output")


def _mask(store: Store, value: str) -> str:
    return for_data_dir(store.data_dir).redact(value[:BODY_LIMIT])[0]


def _insert(conn: Connection, rowid: int, **cols: Any) -> None:
    conn.execute(
        text(
            "INSERT INTO search_index "
            "(rowid, title, body, kind, ref, session_id, task_id, project_id, runtime, at) "
            "VALUES (:rowid, :title, :body, :kind, :ref, :session_id, :task_id, :project_id, :runtime, :at)"
        ),
        {"rowid": rowid, **cols},
    )


def _delete(conn: Connection, rowid: int) -> None:
    conn.execute(text("DELETE FROM search_index WHERE rowid = :r"), {"r": rowid})


# -- indexing ------------------------------------------------------------------------------------------------


def index_events(store: Store, batch: int = BATCH) -> int:
    """Index the next batch of events after the watermark. Returns how many events it looked at."""
    rules = Exclusions.load(store.data_dir)
    with store.reader.connect() as conn:
        after = int(conn.execute(select(Meta.value).where(Meta.key == CURSOR_KEY)).scalar() or 0)
        rows = conn.execute(
            select(
                Event.global_cursor,
                Event.event_type,
                Event.session_id,
                Event.task_id,
                Event.runtime,
                Event.source_time,
                Event.ingested_at,
                SessionRow.execution_cwd,
                SessionRow.project_id,
            )
            .join(SessionRow, SessionRow.session_id == Event.session_id)
            .where(Event.global_cursor > after)
            .order_by(Event.global_cursor)
            .limit(batch)
        ).all()
    if not rows:
        return 0
    docs = []
    for r in rows:
        if (
            r.event_type not in INDEXED_EVENTS
            or rules.skips_event(r.event_type)
            or rules.skips_folder(r.execution_cwd)
        ):
            continue
        try:
            ev = read_payload_json(store, r.global_cursor)
        except (ValueError, OSError):
            continue
        if ev.get("_robin_pruned"):
            continue  # pruned before it was indexed: nothing to search
        body = _event_text(ev)
        docs.append((r, _mask(store, body)))
    last = rows[-1].global_cursor

    def work(conn: Connection) -> None:
        for r, body in docs:
            _delete(conn, r.global_cursor)  # idempotent if a batch is replayed
            _insert(
                conn,
                r.global_cursor,
                title=f"{r.runtime} {r.event_type}",
                body=body,
                kind="event",
                ref=str(r.global_cursor),
                session_id=r.session_id,
                task_id=r.task_id,
                project_id=r.project_id,
                runtime=r.runtime,
                at=(r.source_time or r.ingested_at).isoformat(),
            )
        conn.execute(
            sqlite_insert(Meta)
            .values(key=CURSOR_KEY, value=str(last))
            .on_conflict_do_update(index_elements=["key"], set_={"value": str(last)})
        )

    store.writer.call(work)
    return len(rows)


def _doc_rowid(conn: Connection, key: str, now: datetime) -> int:
    existing = conn.execute(select(SearchDoc.fts_rowid).where(SearchDoc.doc_key == key)).scalar()
    if existing is not None:
        _delete(conn, int(existing))
        conn.execute(update(SearchDoc).where(SearchDoc.doc_key == key).values(indexed_at=now))
        return int(existing)
    lowest = conn.execute(select(func.min(SearchDoc.fts_rowid))).scalar()
    rowid = min(int(lowest or 0), 0) - 1
    conn.execute(insert(SearchDoc).values(doc_key=key, fts_rowid=rowid, indexed_at=now))
    return rowid


def index_docs(store: Store) -> int:
    """Re-index tasks, findings, syntheses and sessions changed since the last pass."""
    rules = Exclusions.load(store.data_dir)
    now = utc_now()
    with store.reader.connect() as conn:
        since_raw = conn.execute(select(Meta.value).where(Meta.key == DOCS_KEY)).scalar()
        since = datetime.fromisoformat(since_raw) if since_raw else datetime.min.replace(tzinfo=now.tzinfo)
        tasks = list(conn.execute(select(Task).where(Task.updated_at > since)).mappings())
        artifacts = list(
            conn.execute(
                select(Artifact, ArtifactVersion.content_blob)
                .join(
                    ArtifactVersion,
                    (ArtifactVersion.artifact_id == Artifact.artifact_id)
                    & (ArtifactVersion.version == Artifact.latest_version),
                )
                .where(Artifact.updated_at > since, Artifact.kind.in_(("finding", "synthesis")))
            ).mappings()
        )
        sessions = list(
            conn.execute(
                select(SessionRow, Project.name.label("project_name"))
                .outerjoin(Project, Project.project_id == SessionRow.project_id)
                .where(SessionRow.updated_at > since)
            ).mappings()
        )
    docs: list[tuple[str, dict[str, Any]]] = []
    for t in tasks:
        docs.append(
            (
                f"task:{t['task_id']}",
                dict(
                    title=t["title"],
                    body=f"{t['title']}\n{t['objective']}\n{t['completion_evidence'] or ''}",
                    kind="task",
                    ref=t["task_id"],
                    session_id=None,
                    task_id=t["task_id"],
                    project_id=t["project_id"],
                    runtime=None,
                    at=t["updated_at"].isoformat(),
                ),
            )
        )
    for a in artifacts:
        doc = json.loads(store.blobs.read(a["content_blob"]))
        if a["kind"] == "finding":
            lines = [doc["summary"]]
            for c in doc["claims"]:
                refs = " ".join(str(e.get("path") or e.get("artifact_id")) for e in c["evidence"])
                lines.append(f"{c['text']} {refs}")
            lines += doc.get("recommendations", []) + doc.get("questions", [])
            title = doc["summary"][:200]
        else:
            lines, title = [doc.get("title", ""), doc.get("text", "")], doc.get("title", "synthesis")
        docs.append(
            (
                f"{a['kind']}:{a['artifact_id']}",
                dict(
                    title=title,
                    body="\n".join(lines),
                    kind=a["kind"],
                    ref=a["artifact_id"],
                    session_id=a["author_session_id"],
                    task_id=a["task_id"],
                    project_id=None,
                    runtime=a["author_runtime"],
                    at=a["updated_at"].isoformat(),
                ),
            )
        )
    for s in sessions:
        if rules.skips_folder(s["execution_cwd"]):
            continue
        body = " ".join(
            str(x)
            for x in (s["runtime"], s["native_session_id"], s["execution_cwd"], s["project_name"], s["mode"])
            if x
        )
        docs.append(
            (
                f"session:{s['session_id']}",
                dict(
                    title=f"{s['runtime']} session {s['session_id'][:8]}",
                    body=body,
                    kind="session",
                    ref=s["session_id"],
                    session_id=s["session_id"],
                    task_id=s["current_task_id"],
                    project_id=s["project_id"],
                    runtime=s["runtime"],
                    at=s["updated_at"].isoformat(),
                ),
            )
        )

    masked = [
        (key, {**cols, "body": _mask(store, cols["body"]), "title": _mask(store, cols["title"])})
        for key, cols in docs
    ]

    def work(conn: Connection) -> None:
        for key, cols in masked:
            _insert(conn, _doc_rowid(conn, key, now), **cols)
        conn.execute(
            sqlite_insert(Meta)
            .values(key=DOCS_KEY, value=now.isoformat())
            .on_conflict_do_update(index_elements=["key"], set_={"value": now.isoformat()})
        )

    store.writer.call(work)
    return len(masked)


def catch_up(store: Store, max_batches: int = 20) -> int:
    """Index whatever is pending, in bounded batches. Returns events looked at."""
    n = 0
    for _ in range(max_batches):
        got = index_events(store)
        n += got
        if got < BATCH:
            break
    index_docs(store)
    return n


def lag(store: Store) -> int:
    with store.reader.connect() as conn:
        done = int(conn.execute(select(Meta.value).where(Meta.key == CURSOR_KEY)).scalar() or 0)
        head = int(conn.execute(select(func.coalesce(func.max(Event.global_cursor), 0))).scalar_one())
    return head - done


# -- searching -----------------------------------------------------------------------------------------------


def fts_query(query: str) -> str:
    """Words only, each quoted (no FTS syntax from the user), all required; the last matches as a prefix."""
    words = re.findall(r"\w+", query, flags=re.UNICODE)[:20]
    if not words:
        raise ValueError("search for at least one word")
    quoted = [f'"{w}"' for w in words]
    quoted[-1] += "*"
    return " ".join(quoted)


def search(
    store: Store,
    query: str,
    *,
    kind: str | None = None,
    session_id: str | None = None,
    task_id: str | None = None,
    project_id: str | None = None,
    runtime: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    limit = max(1, min(limit, PAGE_LIMIT))
    where = ["search_index MATCH :q"]
    params: dict[str, Any] = {"q": fts_query(query), "limit": limit + 1, "offset": max(0, offset)}
    for col, value in (
        ("kind", kind),
        ("session_id", session_id),
        ("task_id", task_id),
        ("project_id", project_id),
        ("runtime", runtime),
    ):
        if value:
            where.append(f"{col} = :{col}")
            params[col] = value
    sql = (
        "SELECT kind, ref, session_id, task_id, project_id, runtime, at, title, "
        "snippet(search_index, 1, '[', ']', '…', 16) AS snippet, bm25(search_index) AS rank "
        f"FROM search_index WHERE {' AND '.join(where)} ORDER BY rank LIMIT :limit OFFSET :offset"
    )
    with store.reader.connect() as conn:
        rows = [dict(r) for r in conn.execute(text(sql), params).mappings()]
    return {
        "query": query,
        "results": [{k: v for k, v in r.items() if k != "rank"} for r in rows[:limit]],
        "more": len(rows) > limit,
        "next_offset": offset + limit if len(rows) > limit else None,
        "pending_events": lag(store),  # newest events may not be searchable yet
    }
