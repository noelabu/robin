import json
import os

import pytest
from sqlalchemy import select, update

from robin.events.ingest import read_events, read_payload
from robin.events.tailer import JsonlTailer, MappedRecord
from robin.store.models import Quarantine, SourceCheckpoint


def rec(i: int, text: str = "hi") -> bytes:
    return json.dumps({"i": i, "text": text}).encode() + b"\n"


def quarantined(store):
    with store.reader.connect() as c:
        return [dict(r) for r in c.execute(select(Quarantine).order_by(Quarantine.offset_start)).mappings()]


def generations(store):
    with store.reader.connect() as c:
        return [
            (r.generation, r.reason, r.state)
            for r in c.execute(select(SourceCheckpoint).order_by(SourceCheckpoint.generation))
        ]


def payloads(store):
    return [json.loads(read_payload(store, e.global_cursor)) for e in read_events(store)]


@pytest.fixture
def jsonl(tmp_path):
    return tmp_path / "session.jsonl"


@pytest.fixture
def tailer(store, session_id, jsonl):
    jsonl.write_bytes(b"")
    t = JsonlTailer(store, jsonl, session_id=session_id, runtime="claude")
    yield t
    t.close()


def test_reads_records_and_resumes_from_offset(store, jsonl, tailer):
    jsonl.write_bytes(rec(1) + rec(2))
    r = tailer.poll_once()
    assert (r.inserted, r.offset) == (2, len(rec(1) + rec(2)))
    with open(jsonl, "ab") as f:
        f.write(rec(3))
    assert tailer.poll_once().inserted == 1
    assert [p["i"] for p in payloads(store)] == [1, 2, 3]
    assert tailer.poll_once().inserted == 0


def test_partial_tail_not_emitted_then_completes(store, jsonl, tailer):
    full = rec(1)
    jsonl.write_bytes(rec(0) + full[:7])
    r = tailer.poll_once()
    assert r.inserted == 1 and r.pending_tail_bytes == 7 and r.offset == len(rec(0))
    with open(jsonl, "ab") as f:
        f.write(full[7:])
    assert tailer.poll_once().inserted == 1
    assert [p["i"] for p in payloads(store)] == [0, 1]


def test_crlf_stripped(store, jsonl, tailer):
    jsonl.write_bytes(rec(1).replace(b"\n", b"\r\n"))
    tailer.poll_once()
    assert read_payload(store, read_events(store)[0].global_cursor) == rec(1)[:-1]


def test_u2028_inside_string_is_one_record(store, jsonl, tailer):
    line = json.dumps({"text": "a b c"}, ensure_ascii=False).encode() + b"\n"
    jsonl.write_bytes(line)
    assert tailer.poll_once().inserted == 1
    assert payloads(store)[0]["text"] == "a b c"


def test_blank_lines_skipped_offset_advances(store, jsonl, tailer):
    jsonl.write_bytes(b"\n\n" + rec(1) + b"   \n")
    r = tailer.poll_once()
    assert r.inserted == 1 and r.offset == jsonl.stat().st_size


def test_identical_records_not_collapsed(store, jsonl, tailer):
    jsonl.write_bytes(rec(1) * 3)
    assert tailer.poll_once().inserted == 3


def test_malformed_json_quarantined_and_checkpoint_advanced(store, jsonl, tailer):
    jsonl.write_bytes(rec(1) + b'{"broken": \n' + rec(2))
    r = tailer.poll_once()
    assert (r.inserted, r.quarantined, r.offset) == (2, 1, jsonl.stat().st_size)
    (q,) = quarantined(store)
    assert q["reason"] == "invalid_json" and q["payload_blob"] is not None
    assert "broken" not in q["diagnostic"]  # no content in diagnostics


def test_mapper_error_quarantined(store, session_id, jsonl):
    jsonl.write_bytes(rec(1) + rec(2))

    def mapper(record, ctx):
        if record["i"] == 2:
            raise KeyError("boom")
        return MappedRecord(event_type="message.user")

    t = JsonlTailer(store, jsonl, session_id=session_id, runtime="claude", mapper=mapper)
    r = t.poll_once()
    t.close()
    assert (r.inserted, r.quarantined) == (1, 1)
    assert read_events(store)[0].event_type == "message.user"
    assert quarantined(store)[0]["reason"] == "mapper_error"


def test_mapper_none_skips_record(store, session_id, jsonl):
    jsonl.write_bytes(rec(1) + rec(2))
    t = JsonlTailer(
        store,
        jsonl,
        session_id=session_id,
        runtime="claude",
        mapper=lambda r, c: None if r["i"] == 1 else MappedRecord(),
    )
    r = t.poll_once()
    t.close()
    assert r.inserted == 1 and r.offset == jsonl.stat().st_size


def test_oversized_record_quarantined_with_bounded_buffer(store, session_id, jsonl):
    big = json.dumps({"blob": "x" * 5000}).encode() + b"\n"
    jsonl.write_bytes(rec(1) + big + rec(2))
    t = JsonlTailer(store, jsonl, session_id=session_id, runtime="claude", chunk=512, max_record=1024)
    r = t.poll_once()
    t.close()
    assert (r.inserted, r.quarantined, r.offset) == (2, 1, jsonl.stat().st_size)
    (q,) = quarantined(store)
    assert q["reason"] == "oversized" and q["payload_blob"] is None
    assert q["offset_end"] - q["offset_start"] == len(big)


def test_oversized_incomplete_tail_stays_pending(store, session_id, jsonl):
    jsonl.write_bytes(rec(1) + b'{"blob": "' + b"x" * 3000)
    t = JsonlTailer(store, jsonl, session_id=session_id, runtime="claude", chunk=512, max_record=1024)
    r = t.poll_once()
    assert r.inserted == 1 and r.offset == len(rec(1)) and r.pending_tail_bytes > 1024
    with open(jsonl, "ab") as f:
        f.write(b'"}\n' + rec(2))
    r = t.poll_once()
    t.close()
    assert (r.inserted, r.quarantined) == (1, 1)


def test_rotation_new_generation_keeps_history_and_drains_old(store, jsonl, tailer):
    jsonl.write_bytes(rec(1))
    tailer.poll_once()
    with open(jsonl, "ab") as f:  # appended after the last poll, before rotation
        f.write(rec(2) + b'{"half":')
    os.rename(jsonl, jsonl.with_suffix(".old"))
    jsonl.write_bytes(rec(3))
    r = tailer.poll_once()
    assert r.new_generations == ["rotated"] and r.generation == 1
    assert [p["i"] for p in payloads(store)] == [1, 2, 3]
    assert generations(store) == [(0, "initial", "superseded"), (1, "rotated", "active")]
    assert quarantined(store)[0]["reason"] == "incomplete_at_rotation"


def test_truncation_starts_new_generation(store, jsonl, tailer):
    jsonl.write_bytes(rec(1) + rec(2))
    tailer.poll_once()
    jsonl.write_bytes(rec(3))  # same inode, shorter
    r = tailer.poll_once()
    assert r.new_generations == ["truncated"] and r.inserted == 1
    assert [p["i"] for p in payloads(store)] == [1, 2, 3]


def test_rewrite_detected_by_anchor(store, jsonl, tailer):
    jsonl.write_bytes(rec(1, "aa") + rec(2))
    tailer.poll_once()
    with open(jsonl, "r+b") as f:  # same inode, same length, different bytes before the offset
        f.write(rec(1, "bb"))
        f.seek(0, os.SEEK_END)
        f.write(rec(3))
    r = tailer.poll_once()
    assert r.new_generations == ["rewritten"]
    assert {p["text"] for p in payloads(store)} >= {"aa", "bb"}


def test_rewritten_events_marked_duplicate_candidates(store, jsonl, tailer):
    jsonl.write_bytes(rec(1) + rec(2))
    tailer.poll_once()
    jsonl.write_bytes(rec(1))  # truncated, then the same first record again
    tailer.poll_once()
    evs = read_events(store)
    assert evs[-1].duplicate_of == evs[0].global_cursor


def test_reads_are_bounded_and_resume(store, session_id, jsonl):
    lines = b"".join(rec(i, "y" * 100) for i in range(200))
    jsonl.write_bytes(lines)
    t = JsonlTailer(store, jsonl, session_id=session_id, runtime="claude", chunk=256, batch_records=7)
    r = t.poll_once()
    t.close()
    assert r.inserted == 200 and r.offset == len(lines)
    assert [p["i"] for p in payloads(store)] == list(range(200))


def test_restart_resumes_from_checkpoint(store, session_id, jsonl):
    jsonl.write_bytes(rec(1) + rec(2))
    JsonlTailer(store, jsonl, session_id=session_id, runtime="claude").poll_once()
    with open(jsonl, "ab") as f:
        f.write(rec(3))
    t2 = JsonlTailer(store, jsonl, session_id=session_id, runtime="claude")
    r = t2.poll_once()
    t2.close()
    assert (r.inserted, r.duplicates) == (1, 0)



def bump_file_dev(store):
    store.writer.call(lambda c: c.execute(update(SourceCheckpoint).values(file_dev=SourceCheckpoint.file_dev + 1)))


def test_dev_change_alone_keeps_generation(store, session_id, jsonl):
    """APFS st_dev isn't stable across reboots: same inode, new dev must resume, not re-import."""
    jsonl.write_bytes(rec(1) + rec(2))
    JsonlTailer(store, jsonl, session_id=session_id, runtime="claude").poll_once()
    bump_file_dev(store)
    with open(jsonl, "ab") as f:
        f.write(rec(3))
    t2 = JsonlTailer(store, jsonl, session_id=session_id, runtime="claude")
    r = t2.poll_once()
    t2.close()
    assert r.new_generations == [] and r.inserted == 1
    assert generations(store) == [(0, "initial", "active")]
    with store.reader.connect() as c:
        assert c.execute(select(SourceCheckpoint.file_dev)).scalar() == jsonl.stat().st_dev


def test_rotation_after_dev_change_drains_old(store, jsonl, tailer):
    jsonl.write_bytes(rec(1))
    tailer.poll_once()
    with open(jsonl, "ab") as f:  # appended after the last poll, before rotation
        f.write(rec(2))
    bump_file_dev(store)
    os.rename(jsonl, jsonl.with_suffix(".old"))
    jsonl.write_bytes(rec(3))
    r = tailer.poll_once()
    assert r.new_generations == ["rotated"] and r.generation == 1
    assert [p["i"] for p in payloads(store)] == [1, 2, 3]


@pytest.mark.slow
def test_real_16_mib_limit(store, session_id, jsonl):
    """Corpus has valid 3.9 MiB records: they must ingest; only records over 16 MiB are quarantined."""
    ok = json.dumps({"blob": "x" * (4 << 20)}).encode() + b"\n"
    too_big = b'{"blob": "' + b"x" * (16 << 20) + b'"}\n'
    jsonl.write_bytes(ok + too_big + rec(1))
    t = JsonlTailer(store, jsonl, session_id=session_id, runtime="claude")
    r = t.poll_once()
    t.close()
    assert (r.inserted, r.quarantined, r.offset) == (2, 1, jsonl.stat().st_size)
    assert read_events(store)[0].payload_ref is not None  # 4 MiB payload spilled to the blob store


def test_polling_does_not_leak_file_descriptors(store, jsonl, tailer):
    jsonl.write_bytes(rec(1))
    tailer.poll_once()
    before = len(os.listdir("/dev/fd"))
    for _ in range(50):
        tailer.poll_once()
    assert len(os.listdir("/dev/fd")) <= before
