"""Identifiers and UTC timestamps (TR-020)."""

import uuid
from datetime import UTC, datetime

# Fixed-width text form: sorts chronologically as a string.
_UTC_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


def new_id() -> str:
    return str(uuid.uuid4())


def utc_now() -> datetime:
    return datetime.now(UTC)


def to_utc_text(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("naive datetime; Robin stores UTC-aware timestamps only")
    return value.astimezone(UTC).strftime(_UTC_FORMAT)


def from_utc_text(text: str) -> datetime:
    return datetime.strptime(text, _UTC_FORMAT).replace(tzinfo=UTC)
