from datetime import UTC

from sqlalchemy import select

from robin.events.ingest import Duplicate, Inserted, read_events, read_payload_json
from robin.events.supervisor_source import SupervisorIngestor, SupervisorRecord
from robin.store.models import Quarantine


def recs(session, gen, seqs, at=1792030771.5):
    return [
        SupervisorRecord(seq=s, gen=gen, session=session, at=at, event={"type": "tick", "n": s}) for s in seqs
    ]


def test_ingest_and_replay_idempotent(store, session_id):
    sup = SupervisorIngestor(store)
    first = sup.ingest(recs(session_id, "g1", [1, 2, 3]))
    assert all(isinstance(r, Inserted) for r in first)
    again = sup.ingest(recs(session_id, "g1", [1, 2, 3]))
    assert again == [Duplicate("n:g1:1"), Duplicate("n:g1:2"), Duplicate("n:g1:3")]
    assert len(read_events(store)) == 3 and sup.checkpoint("g1") == 3


def test_checkpoint_per_generation(store, session_id):
    sup = SupervisorIngestor(store)
    sup.ingest(recs(session_id, "g1", [1, 2]))
    sup.ingest(recs(session_id, "g2", [10, 11, 12]))
    sup.ingest(recs(session_id, "g1", [3]))  # an older generation arriving late
    assert (sup.checkpoint("g1"), sup.checkpoint("g2")) == (3, 12)


def test_payload_is_the_native_event(store, session_id):
    SupervisorIngestor(store).ingest(recs(session_id, "g1", [7]))
    (ev,) = read_events(store)
    assert read_payload_json(store, ev.global_cursor) == {"type": "tick", "n": 7}
    assert ev.source_event_id == "g1:7" and ev.source_id == "supervisor:g1"
    assert ev.source_time is not None and ev.source_time.tzinfo == UTC


def test_unknown_session_quarantined_stream_continues(store, session_id):
    sup = SupervisorIngestor(store)
    sup.ingest(recs("not-a-session", "g1", [1]) + recs(session_id, "g1", [2]))
    assert [e.source_event_id for e in read_events(store)] == ["g1:2"]
    with store.reader.connect() as c:
        reasons = c.execute(select(Quarantine.reason)).scalars().all()
    assert reasons == ["unknown_session"] and sup.checkpoint("g1") == 2


def test_seq_gap_records_gap_event(store, session_id):
    sup = SupervisorIngestor(store)
    sup.ingest(recs(session_id, "g1", [1, 2]))
    sup.ingest(recs(session_id, "g1", [5]))
    types = [(e.event_type, e.evidence_kind.value) for e in read_events(store)]
    assert ("connection.gap", "observed") in types
    gap = next(e for e in read_events(store) if e.event_type == "connection.gap")
    assert read_payload_json(store, gap.global_cursor) == {"after_seq": 2, "next_seq": 5}


def test_quarantined_record_does_not_create_false_gap(store, session_id):
    sup = SupervisorIngestor(store)
    sup.ingest(recs(session_id, "g1", [1, 2, 3, 4]))
    sup.ingest(recs("not-a-session", "g1", [5]) + recs(session_id, "g1", [6]))
    assert "connection.gap" not in [e.event_type for e in read_events(store)]


def test_gap_detected_when_stream_starts_at_seq_zero(store, session_id):
    sup = SupervisorIngestor(store)
    sup.ingest(recs(session_id, "g1", [0]))
    sup.ingest(recs(session_id, "g1", [3]))
    assert "connection.gap" in [e.event_type for e in read_events(store)]
