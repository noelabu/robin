"""Writing a canonical Transcript as a new session in a target runtime's own format.

`render` only builds the file's bytes and its path; `place` puts it in the store through a temporary
file and a rename, so a runtime's resume list never shows a half-written session. Every writer emits
text messages only (tool calls, results and reasoning are tagged text, see canonical.turns). The shapes
below are the minimum each runtime's resume accepted in supervisor/test_continue_live.py.
"""

import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from robin.continuation import paths
from robin.continuation.canonical import Transcript, alternating, turns
from robin.ids import utc_now, uuid7


@dataclass(frozen=True)
class Rendered:
    runtime: str
    native_id: str
    path: Path
    data: bytes


def jsonl(recs: list[dict[str, Any]]) -> bytes:
    return "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recs).encode()


def render_claude(t: Transcript, cwd: str, native_id: str, now: datetime) -> list[dict[str, Any]]:
    recs, parent = [], None
    for role, text, ts in alternating(turns(t.msgs, t.runtime)):
        rid = str(uuid.uuid4())
        recs.append(
            {
                "type": role,
                "message": {"role": role, "content": [{"type": "text", "text": text}]},
                "uuid": rid,
                "parentUuid": parent,
                "timestamp": paths.stamp(ts or now),
                "sessionId": native_id,
                "cwd": cwd,
                "isSidechain": False,
                "userType": "external",
            }
        )
        parent = rid
    return recs


def render_codex(t: Transcript, cwd: str, native_id: str, now: datetime) -> list[dict[str, Any]]:
    meta = {
        "id": native_id,
        "session_id": native_id,
        "timestamp": paths.stamp(now),
        "cwd": cwd,
        "originator": "robin",
        "cli_version": "robin",
        "source": "cli",
        "model_provider": "openai",
    }
    recs: list[dict[str, Any]] = [{"timestamp": paths.stamp(now), "type": "session_meta", "payload": meta}]
    for role, text, ts in turns(t.msgs, t.runtime):
        kind = "input_text" if role == "user" else "output_text"
        item = {"type": "message", "role": role, "content": [{"type": kind, "text": text}]}
        recs.append({"timestamp": paths.stamp(ts or now), "type": "response_item", "payload": item})
    for i, rec in enumerate(recs):
        rec["ordinal"] = i
    return recs


ZERO_USAGE = {
    "input": 0,
    "output": 0,
    "cacheRead": 0,
    "cacheWrite": 0,
    "totalTokens": 0,
    "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0},
}


def render_pi(t: Transcript, cwd: str, native_id: str, now: datetime) -> list[dict[str, Any]]:
    recs: list[dict[str, Any]] = [
        {"type": "session", "version": 3, "id": native_id, "timestamp": paths.stamp(now), "cwd": cwd}
    ]
    parent = None
    for role, text, ts in alternating(turns(t.msgs, t.runtime)):
        when = ts or now
        msg: dict[str, Any] = {
            "role": role,
            "content": [{"type": "text", "text": text}],
            "timestamp": int(when.timestamp() * 1000),
        }
        if role == "assistant":  # Pi's renderer reads these fields without checking for them
            msg.update(
                {
                    "api": "robin",
                    "provider": "robin",
                    "model": t.model or "imported",
                    "usage": ZERO_USAGE,
                    "stopReason": "stop",
                }
            )
        rid = os.urandom(4).hex()
        recs.append(
            {"type": "message", "id": rid, "parentId": parent, "timestamp": paths.stamp(when), "message": msg}
        )
        parent = rid
    return recs


RENDERERS = {"claude": render_claude, "codex": render_codex, "pi": render_pi}


def render(
    t: Transcript, target: str, cwd: str, native_id: str | None = None, now: datetime | None = None
) -> Rendered:
    now = now or utc_now()
    native_id = native_id or (str(uuid.uuid4()) if target == "claude" else uuid7())
    data = jsonl(RENDERERS[target](t, cwd, native_id, now))
    return Rendered(target, native_id, paths.target_path(target, native_id, cwd, now), data)


def stage(r: Rendered) -> Path:
    """Write the session beside its final path under a name no history glob matches; fsynced."""
    r.path.parent.mkdir(parents=True, exist_ok=True)
    tmp = r.path.parent / f".{r.path.name}.tmp-{os.urandom(3).hex()}"
    with open(tmp, "wb") as f:
        f.write(r.data)
        f.flush()
        os.fsync(f.fileno())
    return tmp
