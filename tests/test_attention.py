import time
from datetime import UTC, datetime

import pytest

from robin.events.ingest import ensure_session
from robin.runtime import attention, ledger, state

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


QUESTIONS = {
    "questions": [
        {
            "question": "Which database?",
            "multiSelect": False,
            "options": [{"label": "SQLite"}, {"label": "Postgres"}],
        },
        {
            "question": "Which features?",
            "multiSelect": True,
            "options": [{"label": "Auth"}, {"label": "Billing"}],
        },
    ]
}


def test_a_question_in_a_managed_session_is_answered_in_robin(store, sid):
    feed(
        store,
        sid,
        {"type": "turn_started"},
        {"type": "question", "request_id": "q1", "tool": "AskUserQuestion", "input": QUESTIONS},
    )
    (item,) = attention.list_items(store)
    assert item["kind"] == "question" and item["title"] == "Which database?"
    assert item["allowed_responses"] == ["answer", "deny"] and item["detail"]["answer_in"] == "robin"
    assert [q["multi"] for q in item["detail"]["questions"]] == [False, True]
    with pytest.raises(attention.AttentionError) as exc:
        attention.claim(store, item["attention_id"], "act-allow-1", "allow")
    assert exc.value.code == "invalid_response"
    attention.claim(store, item["attention_id"], "act-answer-1", "answer")
    with pytest.raises(attention.AttentionError) as exc:
        attention.claim(store, item["attention_id"], "act-answer-2", "answer")
    assert exc.value.code == "not_answerable"  # one request, one response
    feed(
        store,
        sid,
        {"type": "approval_answered", "request_id": "q1", "allow": True, "answered": True},
        start=3,
    )
    assert attention.get(store, item["attention_id"])["resolution"] == "answered"


def test_answers_are_checked_against_the_questions():
    qs = attention._questions(QUESTIONS)
    assert attention.normalize_answers(
        qs, {"Which database?": "Postgres", "Which features?": ["Auth", "Billing"]}
    ) == {
        "Which database?": "Postgres",
        "Which features?": "Auth, Billing",
    }
    for bad in (
        {},
        {"Which color?": "red"},
        {"Which database?": ["SQLite", "Postgres"]},  # one answer only
        {"Which database?": "  "},
        {"Which database?": "x" * (attention.ANSWER_CHARS + 1)},
    ):
        with pytest.raises(attention.AttentionError) as exc:
            attention.normalize_answers(qs, bad)
        assert exc.value.code == "invalid_answers"


# -- questions a terminal session waits on through Robin's question hook ------------------------------------

ASK = {"tool_name": "AskUserQuestion", "tool_use_id": "toolu_9", "tool_input": QUESTIONS}
BOTH = {"Which database?": "Postgres", "Which features?": "Auth, Billing"}


def hook(store, sid, *payloads, start=1):
    for i, p in enumerate(payloads, start=start):
        store.writer.call(lambda c, i=i, p=p: attention.project_hook(c, sid, i, T, p))


def observed():
    return {**ASK, "hook_event_name": "PreToolUse"}


def asked(until):
    return {**ASK, "hook_event_name": attention.ASK_HOOK, "robin_until": until}


def waiting(store, sid, until=None):
    """One question the hook waits on until `until` (a minute from now by default)."""
    hook(store, sid, asked(time.time() + 60 if until is None else until))
    (item,) = attention.list_items(store, session_id=sid)
    return item["attention_id"]


def action_state(store, action_id):
    from robin.runtime import ledger

    with store.reader.connect() as c:
        return ledger.get(c, action_id).state


@pytest.mark.parametrize("hook_first", [False, True])
def test_the_question_hook_and_the_observer_end_with_one_answerable_question(store, sid, hook_first):
    events = [asked(2000.0), observed()] if hook_first else [observed(), asked(2000.0)]
    hook(store, sid, *events)
    (item,) = attention.list_items(store, session_id=sid)
    assert item["allowed_responses"] == ["answer", "deny", "terminal"]
    assert item["detail"]["answer_in"] == "hook" and item["detail"]["until"] == 2000.0
    assert item["title"] == "Which database?" and len(item["detail"]["questions"]) == 2


def test_the_question_hook_does_not_revive_a_question_whose_turn_ended(store, sid):
    hook(store, sid, observed(), {"hook_event_name": "Stop"}, asked(2000.0))
    assert attention.list_items(store, session_id=sid) == []
    (item,) = attention.list_items(store, states=("expired",), session_id=sid)
    assert item["allowed_responses"] == [] and item["detail"]["answer_in"] == "terminal"


def test_a_hook_question_is_answered_once_and_the_hook_receives_the_answers(store, sid):
    aid = waiting(store, sid)
    action = attention.reply_hook(store, aid, "act-hook-0001", "answer", BOTH)
    assert action.state == "submitted"
    item = attention.get(store, aid)
    assert item["state"] == "resolved" and item["resolution"] == "answered"
    assert attention.hook_outcome(store, aid) == {"status": "answered", "answers": BOTH}
    assert action_state(store, "act-hook-0001") == "acknowledged"
    with pytest.raises(attention.AttentionError) as exc:
        attention.reply_hook(store, aid, "act-hook-0002", "deny")
    assert exc.value.code == "not_answerable"


def test_a_declined_hook_question_tells_the_hook_why(store, sid):
    aid = waiting(store, sid)
    attention.reply_hook(store, aid, "act-hook-0003", "deny", message="ask me later")
    assert attention.get(store, aid)["resolution"] == "declined"
    assert attention.hook_outcome(store, aid) == {"status": "declined", "message": "ask me later"}


def test_past_its_deadline_the_question_goes_back_to_the_terminal(store, sid):
    aid = waiting(store, sid, until=1000.0)
    assert attention.hook_outcome(store, aid, now=999.0)["status"] == "pending"
    assert attention.hook_outcome(store, aid, now=1001.0) == {"status": "timeout"}
    item = attention.get(store, aid)
    assert item["state"] == "open" and item["allowed_responses"] == []
    assert item["detail"]["answer_in"] == "terminal"
    with pytest.raises(attention.AttentionError) as exc:
        attention.reply_hook(store, aid, "act-hook-0004", "answer", BOTH, now=999.0)
    assert exc.value.code == "answer_in_terminal"


def test_an_answer_after_the_deadline_is_refused_and_hands_the_question_to_the_terminal(store, sid):
    aid = waiting(store, sid, until=1000.0)
    with pytest.raises(attention.AttentionError) as exc:
        attention.reply_hook(store, aid, "act-hook-0005", "answer", BOTH, now=1000.0)
    assert exc.value.code == "answer_in_terminal"
    item = attention.get(store, aid)  # the hand-over sticks even though the reply raised
    assert item["allowed_responses"] == [] and item["detail"]["answer_in"] == "terminal"
    assert attention.hook_outcome(store, aid, now=1001.0) == {"status": "closed"}


def test_an_answer_just_before_the_deadline_still_reaches_the_hook(store, sid):
    aid = waiting(store, sid, until=1000.0)
    attention.reply_hook(store, aid, "act-hook-0006", "answer", BOTH, now=999.5)
    assert attention.hook_outcome(store, aid, now=1001.0) == {"status": "answered", "answers": BOTH}


def test_answer_in_terminal_keeps_the_question_open_until_the_terminal_answers_it(store, sid):
    aid = waiting(store, sid)
    attention.reply_hook(store, aid, "act-hook-0007", "terminal")
    item = attention.get(store, aid)
    assert item["state"] == "open" and item["allowed_responses"] == []
    assert item["detail"]["answer_in"] == "terminal"
    assert attention.hook_outcome(store, aid) == {"status": "terminal"}
    with pytest.raises(attention.AttentionError) as exc:
        attention.reply_hook(store, aid, "act-hook-0008", "answer", BOTH)
    assert exc.value.code == "answer_in_terminal"
    hook(store, sid, {**ASK, "hook_event_name": "PostToolUse"}, start=2)
    item = attention.get(store, aid)
    assert item["state"] == "resolved" and item["resolution"] == "answered"


def test_releasing_the_hook_hands_an_unanswered_question_back_and_repeats_harmlessly(store, sid):
    aid = waiting(store, sid)
    assert attention.release_hook(store, aid) == {"status": "closed"}
    assert attention.release_hook(store, aid) == {"status": "closed"}
    item = attention.get(store, aid)
    assert item["state"] == "open" and item["allowed_responses"] == []
    assert item["detail"]["answer_in"] == "terminal"


def test_an_answer_the_hook_never_passed_on_is_not_recorded_as_delivered(store, sid):
    aid = waiting(store, sid)
    attention.reply_hook(store, aid, "act-hook-0009", "answer", BOTH)
    assert attention.release_hook(store, aid) == {"status": "undelivered"}  # Esc came before the hook had it
    assert attention.get(store, aid)["resolution"] == "not delivered"
    with store.reader.connect() as c:
        action = ledger.get(c, "act-hook-0009")
    assert action is not None and (action.state, action.error_code) == ("failed", "not_delivered")


def test_releasing_after_the_hook_took_the_answer_keeps_it_delivered(store, sid):
    aid = waiting(store, sid)
    attention.reply_hook(store, aid, "act-hook-0011", "answer", BOTH)
    assert attention.hook_outcome(store, aid)["status"] == "answered"
    assert attention.release_hook(store, aid) == {"status": "closed"}
    assert attention.get(store, aid)["resolution"] == "answered"
    with store.reader.connect() as c:
        action = ledger.get(c, "act-hook-0011")
    assert action is not None and action.state == "acknowledged"


def test_the_deadline_is_checked_when_the_answer_is_written_not_when_it_was_queued(store, sid, monkeypatch):
    aid = waiting(store, sid)
    until = attention.get(store, aid)["detail"]["until"]
    clock = [until - 1]  # sent a second before the deadline...
    monkeypatch.setattr(attention.time, "time", lambda: clock[0])
    queued = store.writer.call

    def late(work):  # ...and written after it, once the writer got to it
        clock[0] = until + 1
        return queued(work)

    monkeypatch.setattr(store.writer, "call", late)
    with pytest.raises(attention.AttentionError) as exc:
        attention.reply_hook(store, aid, "act-hook-0012", "answer", BOTH)
    assert exc.value.code == "answer_in_terminal"


def test_a_hook_answer_must_answer_every_question(store, sid):
    aid = waiting(store, sid)
    with pytest.raises(attention.AttentionError) as exc:
        attention.reply_hook(store, aid, "act-hook-0010", "answer", {"Which database?": "Postgres"})
    assert exc.value.code == "invalid_answers" and exc.value.status == 422
    item = attention.get(store, aid)  # still waiting for a full answer
    assert item["state"] == "open" and item["allowed_responses"] == ["answer", "deny", "terminal"]
    assert attention.hook_outcome(store, aid)["status"] == "pending"


def test_a_question_in_a_managed_session_cannot_be_handed_to_a_terminal(store, sid):
    feed(
        store,
        sid,
        {"type": "turn_started"},
        {"type": "question", "request_id": "q1", "tool": "AskUserQuestion", "input": QUESTIONS},
    )
    (item,) = attention.list_items(store)
    with pytest.raises(attention.AttentionError) as exc:
        attention.claim(store, item["attention_id"], "act-term-0001", "terminal")
    assert exc.value.code == "invalid_response"


def test_the_question_wait_defaults_to_five_minutes_and_is_kept_in_range(store):
    assert attention.question_wait_s(store) == 300
    assert attention.set_question_wait_s(store, 9999) == 540
    assert attention.question_wait_s(store) == 540
    assert attention.set_question_wait_s(store, -5) == 0
    assert attention.question_wait_s(store) == 0
