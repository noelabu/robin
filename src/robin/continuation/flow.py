"""Preview and continue: a session carried into another runtime, then taken over there.

`preview` reads and converts the source without writing anything and says what would cross, what wouldn't,
whether it fits the target model, and why not if it can't go. `continue_` checks that nothing changed since
that preview, writes the converted session into the target's store, indexes it as an observed session
linked to its source, and hands it to takeover with the person's first message. The source is never
modified. If the target's host never starts, takeover gives the session back: it stays observed, linked
and can be continued again.
"""

import asyncio
import hashlib
import json
import logging
import os
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import delete as sql_delete
from sqlalchemy import insert, select

from robin.adapters.history import HISTORY
from robin.continuation import CONVERTER_VERSION, RUNTIMES, paths, size
from robin.continuation.canonical import Transcript
from robin.continuation.readers import read
from robin.continuation.writers import Rendered, render, stage
from robin.ids import new_id, utc_now
from robin.paths import ExecutionCwd
from robin.runtime.capabilities import capabilities_for
from robin.store import Store
from robin.store.models import AuditEntry, SessionLink, TaskSession
from robin.store.models import Session as SessionRow

log = logging.getLogger(__name__)

DEFAULT_TEXT = "Continue from where the previous session left off."
REFUSAL_STATUS = {
    "same_runtime": 422,
    "unsupported": 422,
    "no_transcript": 422,
    "no_cwd": 422,
    "source_busy": 409,
    "too_large": 422,
}


class ContinueError(Exception):
    def __init__(self, code: str, message: str, status: int = 422):
        super().__init__(message)
        self.code, self.status = code, status


@dataclass
class Built:
    """A preview and what it was built from, so continue writes exactly what was previewed."""

    view: dict[str, Any]
    transcript: Transcript | None = None
    cwd: str | None = None
    source_path: Path | None = None


def _row(store: Store, session_id: str) -> Any:
    with store.reader.connect() as conn:
        row = conn.execute(select(SessionRow).where(SessionRow.session_id == session_id)).mappings().first()
    if row is None:
        raise ContinueError("not_found", f"no session {session_id}", 404)
    return row


def _checked_cwd(store: Store, raw: str | None) -> str | None:
    if not raw or not os.path.isdir(raw):
        return None
    try:
        cwd = ExecutionCwd.checked(Path(raw), store.artifacts)
    except ValueError:
        return None
    data_dir = store.data_dir.resolve()
    if cwd.path.is_relative_to(data_dir) or data_dir.is_relative_to(cwd.path):
        return None
    return str(cwd.path)


def _busy(store: Store, row: Any) -> str | None:
    from robin.runtime.manager import _recently_written
    from robin.runtime.terminal import holders

    if row["mode"] == "managed" and row["control_owner"] != "terminal":
        if row["runtime_state"] in ("running", "waiting_for_input"):
            return "a turn is running in this session; wait for it to finish or interrupt it first"
        return None
    busy = holders(row["native_session_id"])
    if busy:
        return f"it's still open in a terminal (process {busy[0]}); close it there first"
    if row["mode"] == "observed" and _recently_written(store, row["session_id"]):
        return (
            "it looks like it's still running in a terminal; try again once it has been quiet for two minutes"
        )
    return None


def _hash(row: Any, source_path: Path, target: str, window: size.Window) -> str:
    st = source_path.stat()
    basis = [
        row["session_id"],
        str(source_path),
        st.st_size,
        st.st_mtime_ns,
        target,
        window.model,
        window.tokens,
        CONVERTER_VERSION,
    ]
    return "sha256:" + hashlib.sha256(json.dumps(basis).encode()).hexdigest()


def build(store: Store, source_id: str, target: str) -> Built:
    row = _row(store, source_id)
    caps = capabilities_for(target)
    view: dict[str, Any] = {
        "source": {
            "session_id": row["session_id"],
            "runtime": row["runtime"],
            "native_session_id": row["native_session_id"],
            "mode": row["mode"],
            "cwd": row["execution_cwd"],
        },
        "target": {"runtime": target},
        "warnings": [],
        "refusal": None,
        "default_text": DEFAULT_TEXT,
    }

    def refused(code: str, message: str) -> Built:
        view["refusal"] = {"code": code, "message": message}
        return Built(view)

    if target not in RUNTIMES or row["runtime"] not in RUNTIMES:
        return refused("unsupported", f"continuing between {row['runtime']} and {target} isn't supported")
    if target == row["runtime"]:
        return refused("same_runtime", f"this is already a {target} session")
    ok, reason = caps.allows("takeover")
    if not ok:
        return refused("unsupported", reason or f"{target} can't resume sessions under Robin")
    source_path = paths.locate(row["runtime"], row["native_session_id"]) if row["native_session_id"] else None
    if source_path is None:
        return refused("no_transcript", f"Robin can't find this session's {row['runtime']} transcript")
    cwd = _checked_cwd(store, row["execution_cwd"])
    if cwd is None:
        return refused("no_cwd", "the session's folder isn't available")
    why = paths.unwritable(target, cwd)
    if why:
        return refused("no_cwd", why)
    busy = _busy(store, row)
    if busy:
        return refused("source_busy", f"can't continue now: {busy}")

    transcript = read(row["runtime"], source_path)
    rendered = render(transcript, target, cwd)
    window = size.window(target)
    tokens, allowed = size.estimate(rendered.data), size.limit(window)
    counts = transcript.counts()
    skip = bool(row["skip_permissions"])
    if skip and not caps.skip_permissions:
        skip = False
        view["warnings"].append(f"{target} will ask for permissions: it can't skip permission prompts")
    view.update(
        target={
            "runtime": target,
            "model": window.model,
            "context_window": window.tokens,
            "window_source": window.source,
        },
        messages={k: counts[k] for k in ("total", "user", "assistant", "tool_calls", "tool_results")},
        dropped={
            **dict(transcript.dropped),
            "thinking_as_text": counts["thinking"],
            "tool_calls_as_text": counts["tool_calls"],
        },
        compaction={"source_summary_used": transcript.compacted},
        estimate={
            "bytes": len(rendered.data),
            "tokens": tokens,
            "limit_tokens": allowed,
            "fits": tokens <= allowed,
        },
        inherits={
            "task_id": row["current_task_id"],
            "project_id": row["project_id"],
            "skip_permissions": skip,
            "independence": row["independence"],
        },
        preview_hash=_hash(row, source_path, target, window),
    )
    if tokens > allowed:
        share = int(size.SHARE * 100)
        return Built(
            {
                **view,
                "refusal": {
                    "code": "too_large",
                    "message": (
                        f"~{tokens // 1000}k tokens is over the {allowed // 1000}k limit ({share}% of "
                        f"{window.model or target}'s {window.tokens // 1000}k window). "
                        "Compaction isn't supported yet."
                    ),
                },
            },
        )
    return Built(view, transcript, cwd, source_path)


def preview(store: Store, source_id: str, target: str) -> dict[str, Any]:
    return build(store, source_id, target).view


def links_for(conn: Any, session_ids: list[str]) -> dict[str, dict[str, Any]]:
    """continued_from / continued_in for each session, for the session view."""
    out: dict[str, dict[str, Any]] = {
        sid: {"continued_from": None, "continued_in": []} for sid in session_ids
    }
    if not session_ids:
        return out
    rows = (
        conn.execute(
            select(SessionLink)
            .where(
                SessionLink.source_session_id.in_(session_ids)
                | SessionLink.target_session_id.in_(session_ids)
            )
            .order_by(SessionLink.created_at)
        )
        .mappings()
        .all()
    )
    for r in rows:
        at = r["created_at"].isoformat()
        if r["target_session_id"] in out:
            out[r["target_session_id"]]["continued_from"] = {
                "session_id": r["source_session_id"],
                "runtime": r["source_runtime"],
                "at": at,
            }
        if r["source_session_id"] in out:
            out[r["source_session_id"]]["continued_in"].append(
                {"session_id": r["target_session_id"], "runtime": r["target_runtime"], "at": at}
            )
    return out


def _existing(store: Store, action_id: str) -> Any:
    with store.reader.connect() as conn:
        return conn.execute(select(SessionLink).where(SessionLink.action_id == action_id)).mappings().first()


def _place(store: Store, built: Built, rendered: Rendered, tmp: Path, action_id: str) -> dict[str, Any]:
    """Record the new session and its link, then move the file into the store, all under INGEST_LOCK so the
    watcher can't index the file as an unrelated session first. Returns the link."""
    from robin.discovery import INGEST_LOCK, RuntimeScan, _ingest_session
    from robin.workspace import tasks as ws

    view, source = built.view, built.view["source"]
    st = tmp.stat()  # a rename keeps size and mtime: these identify the placed file
    link = {
        "link_id": new_id(),
        "kind": "continued",
        "source_session_id": source["session_id"],
        "target_session_id": new_id(),
        "source_runtime": source["runtime"],
        "target_runtime": rendered.runtime,
        "source_path": str(built.source_path),
        "written_path": str(rendered.path),
        "written_size": st.st_size,
        "written_mtime_ns": st.st_mtime_ns,
        "preview_hash": view["preview_hash"],
        "manifest": json.dumps(
            {k: view[k] for k in ("messages", "dropped", "compaction", "estimate", "target")}
            | {"converter_version": CONVERTER_VERSION}
        ),
        "action_id": action_id,
        "created_at": utc_now(),
    }
    inherits = view["inherits"]

    def record(conn: Any) -> None:
        now = utc_now()
        conn.execute(
            insert(SessionRow).values(
                session_id=link["target_session_id"],
                host_id=store.host_id,
                runtime=rendered.runtime,
                native_session_id=rendered.native_id,
                mode="observed",
                execution_cwd=built.cwd,
                project_id=inherits["project_id"],
                current_task_id=inherits["task_id"],
                independence=inherits["independence"],
                skip_permissions=int(inherits["skip_permissions"]),
                created_at=now,
                updated_at=now,
            )
        )
        if inherits["task_id"]:
            role = conn.execute(
                select(TaskSession.role).where(
                    TaskSession.task_id == inherits["task_id"], TaskSession.session_id == source["session_id"]
                )
            ).scalar()
            conn.execute(
                insert(TaskSession).values(
                    task_id=inherits["task_id"],
                    session_id=link["target_session_id"],
                    role=role,
                    attached_at=now,
                )
            )
        conn.execute(insert(SessionLink).values(**link))
        conn.execute(
            insert(AuditEntry).values(
                audit_id=new_id(),
                at=now,
                actor="user",
                action="session.continue",
                affected_ids=json.dumps([source["session_id"], link["target_session_id"]]),
                outcome="ok",
                correlation_id=action_id,
            )
        )

    def undo(conn: Any) -> None:
        conn.execute(sql_delete(SessionLink).where(SessionLink.link_id == link["link_id"]))
        conn.execute(sql_delete(TaskSession).where(TaskSession.session_id == link["target_session_id"]))
        conn.execute(sql_delete(SessionRow).where(SessionRow.session_id == link["target_session_id"]))

    with INGEST_LOCK:
        store.writer.call(record)
        try:
            os.replace(tmp, rendered.path)
        except OSError:
            store.writer.call(undo)
            raise
        adapter = HISTORY[rendered.runtime]
        try:
            _ingest_session(
                store,
                adapter.describe(rendered.path),
                adapter.mapper,
                ws.project_paths(store),
                RuntimeScan(rendered.runtime, str(rendered.path.parent)),
            )
        except Exception:  # takeover's catch-up and the next scan read it again
            log.warning("continue: indexing %s failed", rendered.path, exc_info=True)
    return {k: link[k] for k in ("link_id", "source_session_id", "target_session_id", "written_path")}


_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


async def _retried(
    manager: Any, done: Any, source_id: str, target: str, text: str, action_id: str
) -> dict[str, Any]:
    """A request whose session was already written: write nothing again. If its takeover never got as far as
    an action (refused before dispatch, say a terminal looked busy), run the takeover now."""
    from robin.runtime.ledger import ActionConflict

    if done["source_session_id"] != source_id or done["target_runtime"] != target:
        raise ActionConflict("this action ID was used for a different request")
    action = await manager.get_action(action_id)
    if action is None:
        action = await manager.takeover(done["target_session_id"], text, action_id=action_id)
    link = {k: done[k] for k in ("link_id", "source_session_id", "target_session_id", "written_path")}
    return {"action": action, "session_id": done["target_session_id"], "link": link}


async def continue_(
    store: Store, manager: Any, source_id: str, target: str, text: str, preview_hash: str, action_id: str
) -> dict[str, Any]:
    from sqlalchemy.exc import IntegrityError

    from robin.runtime.ledger import ActionConflict

    if not text.strip():
        raise ContinueError("invalid_request", "the first message can't be empty")
    done = await asyncio.to_thread(_existing, store, action_id)
    if done is not None:
        return await _retried(manager, done, source_id, target, text, action_id)
    if await manager.get_action(action_id) is not None:
        raise ActionConflict("this action ID was used for a different request")
    async with _locks[source_id]:
        done = await asyncio.to_thread(_existing, store, action_id)  # a duplicate that waited on the lock
        if done is not None:
            return await _retried(manager, done, source_id, target, text, action_id)
        built = await asyncio.to_thread(build, store, source_id, target)
        refusal = built.view["refusal"]
        if refusal:
            raise ContinueError(refusal["code"], refusal["message"], REFUSAL_STATUS.get(refusal["code"], 422))
        if built.view["preview_hash"] != preview_hash:
            raise ContinueError(
                "preview_stale", "the session changed since the preview; preview it again", 409
            )
        assert built.transcript is not None and built.cwd is not None
        rendered = render(built.transcript, target, built.cwd)
        tmp = await asyncio.to_thread(stage, rendered)
        try:
            link = await asyncio.to_thread(_place, store, built, rendered, tmp, action_id)
        except IntegrityError as exc:  # the same action ID, sent for another source at the same moment
            raise ActionConflict("this action ID was used for a different request") from exc
        finally:
            if tmp.exists():
                tmp.unlink()
    action = await manager.takeover(link["target_session_id"], text, action_id=action_id)
    return {"action": action, "session_id": link["target_session_id"], "link": link}
