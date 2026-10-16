"""Context loops (FR-025, TR-065): does an artifact come, directly or through its inputs, from a session?

An agent that is handed its own output, or a synthesis built on it, can mistake it for independent
confirmation. Lineage edges record every artifact's inputs, so the check walks them back to the start.
"""

from typing import Any

from sqlalchemy import Connection, select

from robin.store.models import Artifact, LineageEdge

MAX_NODES = 2_000  # a lineage this large is far beyond real use; stop walking rather than spin


def own_output(conn: Connection, artifact_id: str, version: int, session_id: str) -> dict[str, Any] | None:
    """The first artifact in this one's lineage (itself included) that `session_id` wrote, or None."""
    seen: set[tuple[str, int]] = set()
    frontier = [(artifact_id, version)]
    while frontier and len(seen) < MAX_NODES:
        node = frontier.pop()
        if node in seen:
            continue
        seen.add(node)
        a = conn.execute(
            select(Artifact.kind, Artifact.author_session_id).where(Artifact.artifact_id == node[0])
        ).first()
        if a is not None and a.author_session_id == session_id:
            return {"artifact_id": node[0], "kind": a.kind, "direct": node[0] == artifact_id}
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
    return f"built on the recipient's own {found['kind']} {found['artifact_id'][:8]}"
