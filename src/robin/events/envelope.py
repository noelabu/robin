"""The normalized event envelope shared by live events and imported history (TR-030)."""

import hashlib
import json
import re
from datetime import datetime
from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from robin.ids import new_id

SCHEMA_VERSION = 1


class EvidenceKind(StrEnum):
    REPORTED = "reported"
    OBSERVED = "observed"
    INFERRED = "inferred"
    UNKNOWN = "unknown"


class EventFamily(StrEnum):
    SESSION = "session"
    MESSAGE = "message"
    TOOL = "tool"
    FILE = "file"
    ATTENTION = "attention"
    FINDING = "finding"
    CONNECTION = "connection"
    UNKNOWN = "unknown"


KNOWN_EVENT_TYPES: dict[str, EventFamily] = {
    **{
        f"session.{n}": EventFamily.SESSION
        for n in ("started", "idle", "failed", "stopped", "resumed", "turn_started", "turn_completed")
    },
    **{f"message.{n}": EventFamily.MESSAGE for n in ("user", "delta", "completed")},
    **{f"tool.{n}": EventFamily.TOOL for n in ("started", "output", "result", "error")},
    **{f"file.{n}": EventFamily.FILE for n in ("read", "write", "change")},
    **{
        t: EventFamily.ATTENTION
        for t in (
            "attention.question",
            "attention.approval_requested",
            "attention.expired",
            "control.submitted",
            "control.acknowledged",
            "control.rejected",
            "control.outcome_unknown",
        )
    },
    **{
        t: EventFamily.FINDING
        for t in ("finding.published", "transfer.prepared", "transfer.delivered", "artifact.created")
    },
    **{
        f"connection.{n}": EventFamily.CONNECTION
        for n in ("connected", "stale", "reconnecting", "recovered", "gap")
    },
    "unknown.raw": EventFamily.UNKNOWN,
}

EVENT_TYPE_PATTERN = re.compile(r"^[a-z_]+(\.[a-z_]+)+$")


def family_of(event_type: str) -> EventFamily:
    """Known types map explicitly; otherwise the prefix decides; unknown types are always accepted."""
    if event_type in KNOWN_EVENT_TYPES:
        return KNOWN_EVENT_TYPES[event_type]
    prefix = event_type.split(".", 1)[0]
    return EventFamily(prefix) if prefix in EventFamily._value2member_map_ else EventFamily.UNKNOWN


def canonical_json(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def content_hash(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _utc_aware(value: datetime | None) -> datetime | None:
    if value is not None and value.utcoffset() is None:
        raise ValueError("source_time must be timezone-aware")
    return value


class NewEvent(BaseModel):
    """An event before commit. The payload is the exact bytes Robin stores."""

    model_config = ConfigDict(frozen=True)

    event_id: str = Field(default_factory=new_id)
    schema_version: int = SCHEMA_VERSION
    runtime: str
    session_id: str
    native_session_id: str | None = None
    run_id: str | None = None
    task_id: str | None = None
    source_id: str
    source_generation: int
    source_event_id: str | None = None
    source_seq: int
    source_offset_end: int | None = None
    native_event_id: str | None = None
    source_time: datetime | None = None
    event_type: str
    evidence_kind: EvidenceKind
    payload: bytes = Field(repr=False)

    @field_validator("event_type")
    @classmethod
    def _type_format(cls, v: str) -> str:
        if not EVENT_TYPE_PATTERN.match(v):
            raise ValueError(f"event_type {v!r} must look like 'family.name'")
        return v

    @field_validator("source_time")
    @classmethod
    def _aware(cls, v: datetime | None) -> datetime | None:
        return _utc_aware(v)

    @model_validator(mode="after")
    def _identity(self) -> Self:
        if self.source_event_id is None and self.source_offset_end is None:
            raise ValueError(
                "an event needs a source_event_id or a byte range (source_seq..source_offset_end)"
            )
        return self

    @property
    def event_family(self) -> EventFamily:
        return family_of(self.event_type)

    @property
    def content_hash(self) -> str:
        return content_hash(self.payload)

    @property
    def dedup_key(self) -> str:
        """Contract: 'n:<source_event_id>' with a native identity, else 'r:<generation>:<start>-<end>'."""
        if self.source_event_id is not None:
            return f"n:{self.source_event_id}"
        return f"r:{self.source_generation}:{self.source_seq}-{self.source_offset_end}"


class EventEnvelope(BaseModel):
    """A committed event as read back."""

    model_config = ConfigDict(frozen=True)

    global_cursor: int
    event_id: str
    schema_version: int
    runtime: str
    session_id: str
    native_session_id: str | None
    run_id: str | None
    task_id: str | None = None
    source_id: str
    source_generation: int
    source_event_id: str | None
    source_seq: int
    source_offset_end: int | None
    native_event_id: str | None
    duplicate_of: int | None
    source_time: datetime | None
    ingested_at: datetime
    event_family: EventFamily
    event_type: str
    evidence_kind: EvidenceKind
    payload_ref: str | None  # "blob:sha256:<hex>" when the payload is in the blob store
    payload_size: int
    content_hash: str
