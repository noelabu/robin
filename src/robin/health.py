"""Health checks (TR-084): each part says whether it is ready, separately, and diagnostics stay local.

Service ready, database writable, index backlog (and any retryable indexing failure, reported once until it
changes), supervisor connection, adapter availability, event coverage gaps and stale session connections.
"""

import shutil
import time
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select

from robin import search
from robin.ids import utc_now
from robin.runtime.capabilities import RUNTIME_CAPABILITIES
from robin.store import Store
from robin.store.models import Event, Meta
from robin.store.models import Session as SessionRow

BINARIES = {"claude": None, "codex": "codex", "pi": "pi"}  # Claude runs through the SDK's bundled binary


def _writable(store: Store) -> tuple[bool, float]:
    started = time.monotonic()
    try:
        store.writer.call(lambda c: c.execute(select(Meta.key).limit(1)))
        return True, round((time.monotonic() - started) * 1000, 1)
    except Exception:
        return False, round((time.monotonic() - started) * 1000, 1)


def check(store: Store, *, supervisor_connected: bool, index_error: dict[str, Any] | None) -> dict[str, Any]:
    writable, write_ms = _writable(store)
    backlog = search.lag(store)
    since = utc_now() - timedelta(hours=24)
    with store.reader.connect() as conn:
        gaps = conn.execute(
            select(func.count(func.distinct(Event.session_id))).where(
                Event.event_type == "connection.gap", Event.ingested_at >= since
            )
        ).scalar_one()
        stale = conn.execute(
            select(func.count())
            .select_from(SessionRow)
            .where(
                SessionRow.mode == "managed",
                SessionRow.archived == 0,
                SessionRow.connection_state.in_(("stale", "reconnecting")),
            )
        ).scalar_one()
    adapters: dict[str, dict[str, Any]] = {}
    for name, caps in RUNTIME_CAPABILITIES.items():
        if name == "fake":
            continue
        binary = BINARIES.get(name)
        present = binary is None or shutil.which(binary) is not None
        adapters[name] = {
            "level": caps.level,
            "available": present,
            "reason": None if present else f"`{binary}` isn't on PATH",
        }
    parts: dict[str, dict[str, Any]] = {
        "service": {"ok": True},
        "database": {"ok": writable, "write_ms": write_ms},
        "index": {"ok": index_error is None, "backlog_events": backlog, "error": index_error},
        "supervisor": {"ok": supervisor_connected},
        "adapters": {"ok": any(a["available"] for a in adapters.values()), "runtimes": adapters},
        "coverage": {"ok": gaps == 0, "sessions_with_gaps_24h": gaps},
        "sessions": {"ok": stale == 0, "stale_connections": stale},
    }
    return {"ready": writable, "ok": all(p["ok"] for p in parts.values()), "checks": parts}
