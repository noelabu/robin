from datetime import UTC, datetime

import pytest

from robin.events.ingest import ensure_session
from robin.runtime import attention, state

T = datetime(2026, 10, 15, tzinfo=UTC)


def feed(store, sid, *events, start=1):
    for i, ev in enumerate(events, start=start):

        def work(c, i=i, ev=ev):
            state.apply(c, sid, i, T, ev)
            attention.project(c, sid, i, T, ev)

        store.writer.call(work)


@pytest.fixture
def sid(store):
    # attention items reference events by cursor; give them real event rows to point at
    from robin.events.envelope import EvidenceKind, NewEvent
    from robin.events.ingest import CheckpointAdvance, ingest_batch, open_generation

    s = ensure_session(store, runtime="fake", mode="managed")
    store.writer.call(
        lambda c: open_generation(c, source_id="t", kind="supervisor", runtime="fake", reason="initial")
    )
    ingest_batch(
        store,
        [
            NewEvent(
                runtime="fake",
                session_id=s,
                source_id="t",
                source_generation=0,
                source_event_id=f"e{i}",
                source_seq=i,
                event_type="unknown.raw",
                evidence_kind=EvidenceKind.UNKNOWN,
                payload=b"{}",
            )
            for i in range(1, 20)
        ],
        [],
        CheckpointAdvance("t", 0, 19, None),
    )
    return s


def test_approval_lifecycle(store, sid):
    feed(
        store,
        sid,
        {"type": "turn_started"},
        {"type": "approval_request", "request_id": "r1", "tool": "Bash", "input": {"command": "make test"}},
    )
    (item,) = attention.list_items(store)
    assert item["kind"] == "approval" and item["title"] == "Bash needs approval"
    assert item["detail"]["input"] == {"command": "make test"} and item["allowed_responses"] == [
        "allow",
        "deny",
    ]
    feed(store, sid, {"type": "approval_answered", "request_id": "r1", "allow": True}, start=3)
    assert attention.list_items(store) == []
    assert attention.list_items(store, states=("resolved",))[0]["resolution"] == "allowed"


def test_requests_expire_when_the_run_ends_and_cannot_be_answered(store, sid):
    feed(
        store,
        sid,
        {"type": "turn_started"},
        {"type": "approval_request", "request_id": "r1", "tool": "Write"},
        {"type": "turn_interrupted"},
    )
    (item,) = attention.list_items(store, states=("expired",))
    with pytest.raises(attention.AttentionError, match="expired"):
        attention.claim(store, item["attention_id"], "act-12345678")


def test_claim_is_once(store, sid):
    feed(
        store,
        sid,
        {"type": "turn_started"},
        {"type": "approval_request", "request_id": "r1", "tool": "Write"},
    )
    (item,) = attention.list_items(store)
    attention.claim(store, item["attention_id"], "act-aaaaaaaa")
    with pytest.raises(attention.AttentionError):
        attention.claim(store, item["attention_id"], "act-bbbbbbbb")
    attention.release(store, item["attention_id"], "act-aaaaaaaa")  # the response never left Robin
    attention.claim(store, item["attention_id"], "act-cccccccc")


def test_dismiss_is_audited_and_never_answers(store, sid):
    feed(
        store,
        sid,
        {"type": "turn_started"},
        {"type": "approval_request", "request_id": "r1", "tool": "Write"},
    )
    (item,) = attention.list_items(store)
    assert attention.set_notice(store, item["attention_id"], "dismissed")["state"] == "dismissed"
    from sqlalchemy import select

    from robin.store.models import AuditEntry
    from robin.store.models import Session as SessionRow

    with store.reader.connect() as c:
        assert c.execute(select(AuditEntry.action)).scalar() == "attention.dismissed"
        assert (
            c.execute(select(SessionRow.runtime_state).where(SessionRow.session_id == sid)).scalar()
            == "waiting_for_input"
        )  # the agent is still waiting


def test_failures_create_one_item_and_reopen(store, sid):
    feed(store, sid, {"type": "turn_started"}, {"type": "turn_failed", "reason": "boom"})
    (item,) = attention.list_items(store)
    assert item["kind"] == "failure" and item["detail"]["reason"] == "boom"
    attention.set_notice(store, item["attention_id"], "acknowledged")
    feed(store, sid, {"type": "turn_started"}, {"type": "turn_failed", "reason": "again"}, start=3)
    (again,) = attention.list_items(store)
    assert again["attention_id"] == item["attention_id"] and again["state"] == "open"
    assert again["detail"]["reason"] == "again"
