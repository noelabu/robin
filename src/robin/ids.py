"""Identifiers and UTC timestamps (TR-020)."""

import uuid
from datetime import UTC, datetime

# Fixed-width text form: sorts chronologically as a string.
_UTC_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


def new_id() -> str:
    return str(uuid.uuid4())


def uuid7() -> str:
    """A time-ordered UUID (RFC 9562 v7), the form Codex and Pi use for session IDs. Python 3.13 has no
    uuid.uuid7."""
    ms = int(datetime.now(UTC).timestamp() * 1000)
    b = bytearray(ms.to_bytes(6, "big") + uuid.uuid4().bytes[6:])
    b[6] = (b[6] & 0x0F) | 0x70
    b[8] = (b[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(b)))


def utc_now() -> datetime:
    return datetime.now(UTC)


def to_utc_text(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("naive datetime; Robin stores UTC-aware timestamps only")
    return value.astimezone(UTC).strftime(_UTC_FORMAT)


def from_utc_text(text: str) -> datetime:
    return datetime.strptime(text, _UTC_FORMAT).replace(tzinfo=UTC)
