import pytest

from robin.events.ingest import ensure_session
from robin.runtime import ledger
from robin.runtime.ledger import ActionConflict


@pytest.fixture
def sid(store):
    return ensure_session(store, runtime="fake", mode="managed")


def create(store, sid, action_id="a1", payload=None, action_type="message"):
    return store.writer.call(
        lambda c: ledger.create(
            c, action_id=action_id, session_id=sid, action_type=action_type, payload=payload or {"text": "hi"}
        )
    )


def test_identical_repeat_returns_stored_action(store, sid):
    first, created = create(store, sid)
    again, created_again = create(store, sid)
    assert created and not created_again and again.request_hash == first.request_hash


def test_same_id_different_payload_conflicts(store, sid):
    create(store, sid)
    with pytest.raises(ActionConflict):
        create(store, sid, payload={"text": "something else"})


def test_transitions_only_move_forward(store, sid):
    create(store, sid)
    t = lambda to, **kw: store.writer.call(lambda c: ledger.transition(c, "a1", to, **kw))  # noqa: E731
    assert t("submitted").state == "submitted"
    assert t("requested").state == "submitted"  # not allowed: stays
    assert t("acknowledged", native_correlation="g:5").state == "acknowledged"
    assert t("failed").state == "acknowledged"  # terminal


def test_recover_marks_in_flight_outcome_unknown_and_late_ack_resolves(store, sid):
    create(store, sid, "a1")
    create(store, sid, "a2")
    store.writer.call(lambda c: ledger.transition(c, "a2", "submitted"))
    recovered = store.writer.call(ledger.recover_in_flight)
    assert sorted(recovered) == ["a1", "a2"]
    a2 = store.writer.call(lambda c: ledger.get(c, "a2"))
    assert a2.state == "outcome_unknown" and a2.error_code == "service_restarted"
    assert store.writer.call(lambda c: ledger.transition(c, "a2", "acknowledged")).state == "acknowledged"
