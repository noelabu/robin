import pytest

from robin.events.envelope import EvidenceKind, NewEvent
from robin.events.ingest import (
    INLINE_LIMIT,
    CheckpointAdvance,
    CheckpointConflict,
    Duplicate,
    Inserted,
    ingest_batch,
    open_generation,
    read_events,
    read_payload,
)

SRC = "file:/tmp/t.jsonl"


def _source(store, generation_count=1):
    for i in range(generation_count):
        store.writer.call(
            lambda c, i=i: open_generation(
                c,
                source_id=SRC,
                kind="jsonl_file",
                runtime="claude",
                reason="initial" if i == 0 else "rotated",
            )
        )


def _ev(session_id, start, end, payload=b'{"a":1}', gen=0, **kw):
    return NewEvent(
        runtime="claude",
        session_id=session_id,
        source_id=SRC,
        source_generation=gen,
        source_seq=start,
        source_offset_end=end,
        event_type="unknown.raw",
        evidence_kind=EvidenceKind.UNKNOWN,
        payload=payload,
        **kw,
    )


def test_insert_then_duplicate_is_not_an_error(store, session_id):
    _source(store)
    first = ingest_batch(store, [_ev(session_id, 0, 8)], [], CheckpointAdvance(SRC, 0, 8, 0))
    again = ingest_batch(store, [_ev(session_id, 0, 8)], [], CheckpointAdvance(SRC, 0, 8, 8))
    assert isinstance(first[0], Inserted) and again == [Duplicate("r:0:0-8")]
    assert len(read_events(store)) == 1


def test_identical_messages_not_collapsed(store, session_id):
    _source(store)
    res = ingest_batch(
        store, [_ev(session_id, 0, 8), _ev(session_id, 8, 16)], [], CheckpointAdvance(SRC, 0, 16, 0)
    )
    assert all(isinstance(r, Inserted) for r in res)
    evs = read_events(store)
    assert len(evs) == 2 and evs[0].content_hash == evs[1].content_hash


def test_global_cursor_monotonic_commit_order(store, session_id):
    _source(store)
    ingest_batch(
        store, [_ev(session_id, i * 8, i * 8 + 8) for i in range(5)], [], CheckpointAdvance(SRC, 0, 40, 0)
    )
    cursors = [e.global_cursor for e in read_events(store)]
    assert cursors == sorted(cursors) and len(set(cursors)) == 5


@pytest.mark.parametrize("size,spilled", [(INLINE_LIMIT, False), (INLINE_LIMIT + 1, True)])
def test_large_payload_spills_to_blob(store, session_id, size, spilled):
    _source(store)
    payload = b'"' + b"x" * (size - 2) + b'"'
    ingest_batch(store, [_ev(session_id, 0, size, payload)], [], CheckpointAdvance(SRC, 0, size, 0))
    (ev,) = read_events(store)
    assert (ev.payload_ref is not None) is spilled
    assert read_payload(store, ev.global_cursor) == payload


def test_non_utf8_payload_goes_to_blob(store, session_id):
    _source(store)
    ingest_batch(store, [_ev(session_id, 0, 3, b"\xff\xfe\x00")], [], CheckpointAdvance(SRC, 0, 3, 0))
    (ev,) = read_events(store)
    assert ev.payload_ref and read_payload(store, ev.global_cursor) == b"\xff\xfe\x00"


def test_duplicate_candidate_marked_across_generations(store, session_id):
    _source(store, 2)
    ingest_batch(store, [_ev(session_id, 0, 8, gen=0)], [], CheckpointAdvance(SRC, 0, 8, None))
    res = ingest_batch(store, [_ev(session_id, 0, 8, gen=1)], [], CheckpointAdvance(SRC, 1, 8, 0))
    first = read_events(store)[0]
    assert isinstance(res[0], Inserted) and res[0].duplicate_of == first.global_cursor


def test_checkpoint_conflict_rejected_and_nothing_written(store, session_id):
    _source(store)
    with pytest.raises(CheckpointConflict):
        ingest_batch(store, [_ev(session_id, 0, 8)], [], CheckpointAdvance(SRC, 0, 8, expected_offset=5))
    assert read_events(store) == []
