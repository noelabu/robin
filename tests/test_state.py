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


def test_host_error_before_start_is_failed_not_stopped(store):
    sid = ensure_session(store, runtime="fake", mode="managed")
    feed(store, sid, "host_spawned", "host_error", "host_exited")
    r = row(store, sid)
    assert (r["runtime_state"], r["connection_state"]) == ("failed", "disconnected")


def test_host_error_mid_turn_ends_the_run_failed(store):
    sid = ensure_session(store, runtime="fake", mode="managed")
    feed(store, sid, "host_spawned", "session_started", "turn_started", "host_error", "session_stopping")
    feed(store, sid, "host_exited")
    assert row(store, sid)["runtime_state"] == "failed"
    with store.reader.connect() as c:
        assert c.execute(select(Run.outcome)).scalar_one() == "failed"


def test_native_session_id_captured_once_and_never_stolen(store):
    a = ensure_session(store, runtime="fake", mode="managed")
    b = ensure_session(store, runtime="fake", mode="managed")
    store.writer.call(
        lambda c: state.apply(c, a, 1, T, {"type": "session_started", "native_session_id": "n1"})
    )
    store.writer.call(
        lambda c: state.apply(c, b, 2, T, {"type": "session_started", "native_session_id": "n1"})
    )
    assert row(store, a)["native_session_id"] == "n1" and row(store, b)["native_session_id"] is None


def test_late_session_started_does_not_downgrade_a_running_turn(store):
    sid = ensure_session(store, runtime="fake", mode="managed")
    feed(store, sid, "turn_started", "session_started")
    assert row(store, sid)["runtime_state"] == "running"
