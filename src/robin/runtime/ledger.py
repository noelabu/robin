"""The control-action ledger (TR-041–043).

An action is persisted before it is dispatched. A client action ID reused for the identical request
returns the stored action; reused for a different request it is a conflict. Acknowledgement means the
runtime accepted the action, not that any coding work finished. After a crash, actions that may or may
not have reached the runtime become outcome_unknown and are never resent automatically.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, insert, select, update

from robin.ids import utc_now
from robin.store.models import ControlAction

ACTION_TYPES = ("launch", "message", "interrupt", "approval", "stop", "handoff", "reclaim", "resume")
TERMINAL = {"acknowledged", "failed", "cancelled"}
# Allowed transitions. outcome_unknown can still resolve when a native acknowledgement arrives late.
TRANSITIONS: dict[str, set[str]] = {
    # A host acknowledgement can arrive before the supervisor's reply: it implies submission.
    "requested": {"submitted", "acknowledged", "failed", "cancelled", "outcome_unknown"},
    "submitted": {"acknowledged", "failed", "outcome_unknown"},
    "outcome_unknown": {"acknowledged", "failed"},
    "acknowledged": set(),
    "failed": set(),
    "cancelled": set(),
}


class ActionConflict(RuntimeError):
    """The action ID was already used for a different request (HTTP 409)."""


class ActionRejected(RuntimeError):
    """A precondition failed before dispatch: unsupported, stale, or not connected (HTTP 409/422)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Action:
    action_id: str
    session_id: str
    action_type: str
    state: str
    request_hash: str
    payload: dict[str, Any]
    error_code: str | None
    error_message: str | None
    native_correlation: str | None
    requested_at: datetime
    updated_at: datetime


def request_hash(session_id: str, action_type: str, payload: dict[str, Any]) -> str:
    body = json.dumps(
        {"session": session_id, "type": action_type, "payload": payload},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(body.encode()).hexdigest()


def _row_to_action(r: Any) -> Action:
    return Action(
        r["action_id"],
        r["session_id"],
        r["action_type"],
        r["state"],
        r["request_hash"],
        json.loads(r["payload_inline"] or "{}"),
        r["error_code"],
        r["error_message"],
        r["native_correlation"],
        r["requested_at"],
        r["updated_at"],
    )


def get(conn: Connection, action_id: str) -> Action | None:
    row = conn.execute(select(ControlAction).where(ControlAction.action_id == action_id)).mappings().first()
    return _row_to_action(row) if row else None


def create(
    conn: Connection,
    *,
    action_id: str,
    session_id: str,
    action_type: str,
    payload: dict[str, Any],
    expected_generation: int | None = None,
) -> tuple[Action, bool]:
    """Persist a new action, or return the stored one for an identical repeat. (action, created)."""
    if action_type not in ACTION_TYPES:
        raise ValueError(f"unknown action type {action_type!r}")
    digest = request_hash(session_id, action_type, payload)
    existing = get(conn, action_id)
    if existing is not None:
        if existing.request_hash != digest:
            raise ActionConflict(f"action id {action_id} was already used for a different request")
        return existing, False
    now = utc_now()
    conn.execute(
        insert(ControlAction).values(
            action_id=action_id,
            session_id=session_id,
            action_type=action_type,
            request_hash=digest,
            expected_generation=expected_generation,
            payload_inline=json.dumps(payload, ensure_ascii=False),
            state="requested",
            requested_at=now,
            updated_at=now,
        )
    )
    action = get(conn, action_id)
    assert action is not None
    return action, True


def transition(
    conn: Connection,
    action_id: str,
    to: str,
    *,
    error_code: str | None = None,
    error_message: str | None = None,
    native_correlation: str | None = None,
) -> Action | None:
    """Move an action forward if the transition is allowed; otherwise leave it as is."""
    current = get(conn, action_id)
    if current is None or to not in TRANSITIONS[current.state]:
        return current
    now = utc_now()
    values: dict[str, Any] = {"state": to, "updated_at": now}
    if to == "submitted":
        values["submitted_at"] = now
    if to == "acknowledged":
        values["acknowledged_at"] = now
    if to in TERMINAL:
        values["completed_at"] = now
    if error_code is not None:
        values["error_code"], values["error_message"] = error_code, error_message
    if native_correlation is not None:
        values["native_correlation"] = native_correlation
    conn.execute(update(ControlAction).where(ControlAction.action_id == action_id).values(**values))
    return get(conn, action_id)


def recover_in_flight(conn: Connection) -> list[str]:
    """After a service crash: anything that may have reached the runtime is outcome_unknown (TR-043)."""
    ids = list(
        conn.execute(
            select(ControlAction.action_id).where(ControlAction.state.in_(("requested", "submitted")))
        ).scalars()
    )
    for action_id in ids:
        transition(
            conn,
            action_id,
            "outcome_unknown",
            error_code="service_restarted",
            error_message="the service stopped before this action's outcome was recorded",
        )
    return ids
