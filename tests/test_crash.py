"""Crash safety: events, quarantines and the checkpoint advance commit together, or not at all."""

import json
import subprocess
import sys
import textwrap

import pytest

import robin.events.ingest as ingest
from robin.events.ingest import read_checkpoint, read_events
from robin.events.tailer import JsonlTailer
from robin.store import open_store


def rec(i):
    return json.dumps({"i": i}).encode() + b"\n"


def test_crash_between_parse_and_commit_then_exactly_once(store, session_id, tmp_path, monkeypatch):
    path = tmp_path / "s.jsonl"
    path.write_bytes(rec(1) + rec(2))
    real = ingest.open_generation
    t = JsonlTailer(store, path, session_id=session_id, runtime="claude")
    t.poll_once()  # gen 0, both records committed
    with open(path, "ab") as f:
        f.write(rec(3) + rec(4))

    calls = {"n": 0}
    real_update = ingest.update

    def failing_update(*a, **kw):  # fail the checkpoint UPDATE inside the transaction
        calls["n"] += 1
        raise RuntimeError("simulated crash before the checkpoint update")

    monkeypatch.setattr(ingest, "update", failing_update)
    with pytest.raises(RuntimeError):
        t.poll_once()
    monkeypatch.setattr(ingest, "update", real_update)
    assert calls["n"] == 1 and len(read_events(store)) == 2  # nothing from the failed batch persisted
    r = t.poll_once()
    t.close()
    assert r.inserted == 2 and [json.loads(_payload(store, e))["i"] for e in read_events(store)] == [
        1,
        2,
        3,
        4,
    ]
    assert real is ingest.open_generation


def _payload(store, ev):
    from robin.events.ingest import read_payload

    return read_payload(store, ev.global_cursor)


CHILD = textwrap.dedent("""
    import os, sys
    from pathlib import Path
    import robin.events.ingest as ingest
    from robin.events.ingest import ensure_session
    from robin.events.tailer import JsonlTailer
    from robin.store import open_store

    data, path = Path(sys.argv[1]), Path(sys.argv[2])
    store = open_store(data)
    sid = ensure_session(store, runtime="claude", mode="observed", session_id="s1")
    JsonlTailer(store, path, session_id=sid, runtime="claude", batch_records=2).poll_once()
    with open(path, "ab") as f:
        f.write(b'{"i": 3}\\n{"i": 4}\\n')
    real = ingest.update
    def die(*a, **kw):  # inside the writer transaction, after the event INSERTs
        os._exit(1)
    ingest.update = die
    JsonlTailer(store, path, session_id=sid, runtime="claude", batch_records=2).poll_once()
""")


def test_kill_inside_transaction_subprocess(tmp_path):
    data, path = tmp_path / "data", tmp_path / "s.jsonl"
    path.write_bytes(rec(1) + rec(2))
    proc = subprocess.run(
        [sys.executable, "-c", CHILD, str(data), str(path)], capture_output=True, timeout=60
    )
    assert proc.returncode == 1, proc.stderr.decode()
    store = open_store(data)
    try:
        assert [e.source_seq for e in read_events(store)] == [0, len(rec(1))]  # no partial rows
        with store.reader.connect() as c:
            cp = read_checkpoint(c, "file:" + str(path.resolve()))
        assert cp is not None and cp.offset == len(rec(1) + rec(2))
        t = JsonlTailer(store, path, session_id="s1", runtime="claude")
        assert t.poll_once().inserted == 2
        t.close()
    finally:
        store.close()
