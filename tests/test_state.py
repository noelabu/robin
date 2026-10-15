from datetime import UTC, datetime

from sqlalchemy import select

from robin.events.ingest import ensure_session
from robin.runtime import state
from robin.store.models import Run
from robin.store.models import Session as SessionRow

T = datetime(2026, 10, 15, tzinfo=UTC)


def row(store, sid):
    with store.reader.connect() as c:
        return c.execute(select(SessionRow).where(SessionRow.session_id == sid)).mappings().one()


def feed(store, sid, *types):
    for i, t in enumerate(types, start=row(store, sid)["state_cursor"] + 1):
        store.writer.call(lambda c, i=i, t=t: state.apply(c, sid, i, T, {"type": t}))


def test_turn_lifecycle_and_runs(store):
    sid = ensure_session(store, runtime="fake", mode="managed")
    feed(store, sid, "host_spawned", "session_started")
    assert (row(store, sid)["runtime_state"], row(store, sid)["connection_state"]) == ("idle", "connected")
    feed(store, sid, "turn_started")
    assert row(store, sid)["runtime_state"] == "running" and row(store, sid)["active_run_id"]
    feed(store, sid, "approval_request")
    assert row(store, sid)["runtime_state"] == "waiting_for_input"
    feed(store, sid, "approval_answered", "turn_interrupted")
    r = row(store, sid)
    assert r["runtime_state"] == "idle" and r["active_run_id"] is None
    with store.reader.connect() as c:
        assert c.execute(select(Run.outcome)).scalar_one() == "interrupted"


def test_request_outside_a_turn_is_not_blocking(store):
    sid = ensure_session(store, runtime="fake", mode="managed")
    feed(store, sid, "session_started", "approval_request")
    assert row(store, sid)["runtime_state"] == "idle"


def test_replayed_events_do_not_reapply(store):
    sid = ensure_session(store, runtime="fake", mode="managed")
    store.writer.call(lambda c: state.apply(c, sid, 5, T, {"type": "turn_started"}))
    store.writer.call(lambda c: state.apply(c, sid, 5, T, {"type": "turn_started"}))
    with store.reader.connect() as c:
        assert len(c.execute(select(Run)).all()) == 1


def test_host_exit_stops_and_disconnects(store):
    sid = ensure_session(store, runtime="fake", mode="managed")
    feed(store, sid, "host_spawned", "session_started", "turn_started", "host_exited")
    r = row(store, sid)
    assert (r["runtime_state"], r["connection_state"]) == ("stopped", "disconnected")
