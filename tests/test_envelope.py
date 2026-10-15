from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from robin.events.envelope import EventFamily, EvidenceKind, NewEvent, family_of

BASE = dict(
    runtime="claude",
    session_id="s",
    source_id="file:/x",
    source_generation=0,
    source_seq=0,
    source_offset_end=10,
    event_type="tool.started",
    evidence_kind=EvidenceKind.REPORTED,
    payload=b"{}",
)


def test_evidence_kinds():
    assert {e.value for e in EvidenceKind} == {"reported", "observed", "inferred", "unknown"}


def test_family_known_prefix_and_unknown():
    assert family_of("tool.started") is EventFamily.TOOL
    assert family_of("control.acknowledged") is EventFamily.ATTENTION
    assert family_of("message.reasoning_delta") is EventFamily.MESSAGE  # unknown type, known prefix
    assert family_of("pi.queue_update") is EventFamily.UNKNOWN


def test_invalid_type_format_rejected():
    with pytest.raises(ValidationError):
        NewEvent(**{**BASE, "event_type": "ToolStarted"})


def test_naive_source_time_rejected():
    with pytest.raises(ValidationError):
        NewEvent(**{**BASE, "source_time": datetime(2026, 10, 15)})
    assert NewEvent(**{**BASE, "source_time": datetime(2026, 10, 15, tzinfo=UTC)}).source_time


def test_needs_native_id_or_byte_range():
    with pytest.raises(ValidationError):
        NewEvent(**{**BASE, "source_offset_end": None})
    assert NewEvent(**{**BASE, "source_offset_end": None, "source_event_id": "g:1"}).dedup_key == "n:g:1"
    assert NewEvent(**BASE).dedup_key == "r:0:0-10"


def test_task_id_carried():
    assert NewEvent(**{**BASE, "task_id": "t"}).task_id == "t"


def test_payload_not_in_repr():
    assert "secret" not in repr(NewEvent(**{**BASE, "payload": b'{"k":"secret"}'}))
