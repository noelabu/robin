"""Context loops (FR-025, TR-065): does an artifact come, directly or through its inputs, from a session?

An agent that is handed its own output, or a synthesis built on it, can mistake it for independent
confirmation. Lineage edges record every artifact's inputs, so the check walks them back to the start.
A session continued from another inherits that one's work up to the continuation, so it counts as its own.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import Connection, select

from robin.store.models import Artifact, LineageEdge, SessionLink

MAX_NODES = 2_000  # a lineage this large is far beyond real use; stop walking rather than spin


def _continues(conn: Connection, session_id: str) -> dict[str, datetime]:
    """The sessions `session_id` continues from, each with the time up to which its work was inherited."""
    inherited: dict[str, datetime] = {}
    current, cutoff = session_id, None
    while True:
        link = conn.execute(
            select(SessionLink.source_session_id, SessionLink.created_at).where(
                SessionLink.target_session_id == current
            )
        ).first()
        if link is None or link.source_session_id == session_id or link.source_session_id in inherited:
            return inherited
        cutoff = link.created_at if cutoff is None else min(cutoff, link.created_at)
        current = link.source_session_id
        inherited[current] = cutoff


def own_output(conn: Connection, artifact_id: str, version: int, session_id: str) -> dict[str, Any] | None:
    """The first artifact in this one's lineage (itself included) that `session_id` wrote, or inherited
    from a session it continues, or None."""
    inherited = _continues(conn, session_id)
    seen: set[tuple[str, int]] = set()
    frontier = [(artifact_id, version)]
    while frontier and len(seen) < MAX_NODES:
        node = frontier.pop()
        if node in seen:
            continue
        seen.add(node)
        a = conn.execute(
            select(Artifact.kind, Artifact.author_session_id, Artifact.created_at).where(
                Artifact.artifact_id == node[0]
            )
        ).first()
        if a is not None and a.author_session_id == session_id:
            return {"artifact_id": node[0], "kind": a.kind, "direct": node[0] == artifact_id}
        if (
            a is not None
            and a.author_session_id in inherited
            and a.created_at <= inherited[a.author_session_id]
        ):
            return {"artifact_id": node[0], "kind": a.kind, "direct": False, "itself": node[0] == artifact_id}
        frontier += [
            (p.parent_artifact_id, p.parent_version)
            for p in conn.execute(
                select(LineageEdge.parent_artifact_id, LineageEdge.parent_version).where(
                    LineageEdge.child_artifact_id == node[0], LineageEdge.child_version == node[1]
                )
            )
        ]
    return None


def reason(found: dict[str, Any]) -> str:
    if found["direct"]:
        return "the recipient wrote this"
    if found.get("itself"):
        return f"the recipient's own {found['kind']}, written in the session it continues"
    return f"built on the recipient's own {found['kind']} {found['artifact_id'][:8]}"
