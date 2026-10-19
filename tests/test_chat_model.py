"""The chat's reading of events and of the user's input, without the terminal UI."""

from robin.adapters.claude.host import question_result
from robin.chat import model
from robin.chat.model import ChatState, Line

QUESTION = {
    "attention_id": "a1",
    "kind": "question",
    "state": "open",
    "created_at": "2026-10-17T10:00:00",
    "allowed_responses": ["answer", "deny"],
    "detail": {
        "questions": [
            {
                "question": "Which database?",
                "multi": False,
                "options": [{"label": "SQLite"}, {"label": "Postgres"}],
            },
            {
                "question": "Which features?",
                "multi": True,
                "options": [{"label": "Auth"}, {"label": "Billing"}],
            },
        ]
    },
}
APPROVAL = {
    "attention_id": "a2",
    "kind": "approval",
    "state": "open",
    "created_at": "2026-10-17T09:00:00",
    "allowed_responses": ["allow", "deny"],
    "detail": {"tool": "Bash", "input": {"command": "make test"}},
}
MANAGED = {"session_id": "s1", "mode": "managed", "control_owner": "robin", "runtime": "claude"}


def frame(event_type, session_id="s1", **extra):
    ev = {"session_id": session_id, "event_type": event_type, "source_id": "supervisor:s1", **extra}
    return {"event": ev, "preview": "Read app.py", "noise": False}


def test_parse_sse_reads_frames_and_skips_heartbeats():
    lines = [
        ": heartbeat",
        "",
        "id: 7",
        "event: event",
        'data: {"a": 1}',
        "",
        "event: resync_required",
        "data: {}",
        "",
    ]
    assert list(model.parse_sse(lines)) == [("event", 7, {"a": 1}), ("resync_required", None, {})]


def test_only_this_sessions_own_tool_steps_become_lines():
    assert model.frame_line(frame("tool.started"), "s1") == Line("tool", "Read app.py")
    assert model.frame_line(frame("tool.error"), "s1") == Line("error", "Read app.py")
    assert model.frame_line(frame("tool.started", session_id="s2"), "s1") is None
    assert model.frame_line({**frame("tool.started"), "noise": True}, "s1") is None
    assert model.frame_line(frame("tool.started", duplicate_of=3), "s1") is None
    sub = frame("tool.started", source_id="file:/x/s1/subagents/agent-a.jsonl")
    assert model.frame_line(sub, "s1") is None  # a subagent's step stays on its card
    assert model.frame_line(frame("message.completed"), "s1") is None  # replies come from the conversation
    assert model.wants_conversation(frame("message.completed"), "s1")
    assert model.wants_attention(frame("attention.question"), "s1")
    assert not model.wants_attention(frame("attention.question", session_id="s2"), "s1")


def test_new_messages_adds_what_is_new_and_grows_the_last_reply():
    seen = [{"role": "user", "at": "1", "text": "hi"}, {"role": "assistant", "at": "2", "text": "Hel"}]
    fetched = [
        {"role": "user", "at": "1", "text": "hi"},
        {"role": "assistant", "at": "2", "text": "Hello"},
        {"role": "tools", "at": "3", "items": ["Read x"]},
        {"role": "user", "at": "4", "text": "more"},
    ]
    grown, added = model.new_messages(seen, fetched)
    assert grown == {"role": "assistant", "at": "2", "text": "Hello"}
    assert added == [{"role": "user", "at": "4", "text": "more"}]
    assert model.new_messages(fetched, fetched) == (None, [])
    # the window slid past what was shown: only what's newer
    assert model.new_messages([{"role": "user", "at": "0", "text": "old"}], fetched[3:]) == (
        None,
        fetched[3:],
    )


def test_parse_choice():
    single, multi = QUESTION["detail"]["questions"]
    assert model.parse_choice("2", single) == "Postgres"
    assert model.parse_choice("1,2", multi) == "Auth, Billing"
    assert model.parse_choice("2 1 2", multi) == "Billing, Auth"
    assert model.parse_choice("1,2", single) == "1,2"  # one pick only: taken as text
    assert model.parse_choice("9", single) == "9"
    assert model.parse_choice("  Redis please ", single) == "Redis please"


def test_routing_input():
    st = ChatState(session=MANAGED)
    assert model.route_input("  ", st).kind == "nothing"
    assert model.route_input("/quit", st).kind == "quit"
    assert model.route_input("/interrupt", st).kind == "interrupt"
    assert model.route_input("/stop", st).kind == "stop"
    assert model.route_input("fix it", st) == model.Intent("message", "fix it")
    handed = ChatState(session={**MANAGED, "control_owner": "terminal"})
    assert model.route_input("fix it", handed).kind == "error"


def test_an_approval_takes_y_or_n():
    st = ChatState(session=MANAGED, pending=APPROVAL)
    assert model.route_input("y", st).kind == "allow"
    assert model.route_input("No", st).kind == "deny"
    assert model.route_input("maybe", st).kind == "error"
    assert "make test" in model.pending_text(st)


def test_a_question_is_answered_one_at_a_time():
    st = ChatState(session=MANAGED, pending=QUESTION)
    assert "Which database?" in model.pending_text(st) and "1. SQLite" in model.pending_text(st)
    first = model.route_input("2", st)
    assert first.kind == "next_answer" and first.answers == {"Which database?": "Postgres"}
    st.answers = first.answers
    assert "Which features?" in model.pending_text(st) and "Postgres" in model.pending_text(st)
    done = model.route_input("1,2", st)
    assert done.kind == "submit_answers"
    assert done.answers == {"Which database?": "Postgres", "Which features?": "Auth, Billing"}
    assert model.route_input("/decline", st).kind == "decline"


def test_a_question_asked_in_a_terminal_is_answered_there():
    terminal = {**QUESTION, "allowed_responses": [], "title": "Which database?"}
    st = ChatState(session=MANAGED, pending=terminal)
    assert "Answer it in the session's terminal" in model.pending_text(st)
    assert model.route_input("2", st) == model.Intent("message", "2")


def test_observed_sessions_take_over_or_say_why_not():
    observed = {"session_id": "s1", "mode": "observed", "controls": {"takeover": {"enabled": True}}}
    assert model.route_input("go", ChatState(session=observed)).kind == "takeover"
    locked = {**observed, "controls": {"takeover": {"enabled": False, "reason": "a terminal holds it"}}}
    intent = model.route_input("go", ChatState(session=locked))
    assert intent.kind == "error" and "a terminal holds it" in intent.text and "robin open s1" in intent.text


def test_pending_picks_the_oldest_open_request():
    assert model.pending([QUESTION, APPROVAL]) == APPROVAL
    assert model.pending([{**APPROVAL, "state": "resolved"}]) is None


def test_a_source_is_read_up_to_the_link_that_continued_it():
    messages = [
        {"role": "user", "at": "2026-10-18T09:00:00+00:00", "text": "before"},
        {"role": "user", "at": "2026-10-18T10:00:00+00:00", "text": "at"},
        {"role": "tools", "items": ["no time"]},
        {"role": "user", "at": "2026-10-18T11:00:00+00:00", "text": "after"},
    ]
    link = {"session_id": "s0", "at": "2026-10-18T10:00:00+00:00"}
    assert [m.get("text") for m in model.before_link(messages, link)] == ["before", "at", None]
    assert model.before_link(messages, {"session_id": "s0"}) == messages
    assert model.before_link(messages, None) == messages


def test_status_line():
    s = {
        **MANAGED,
        "execution_cwd": "/work/robin",
        "branch": "main",
        "runtime_state": "running",
        "skip_permissions": True,
    }
    assert model.status(s) == "claude · robin @ main · working · skip permissions · control: robin   ● live"
    assert model.status({**s, "mode": "observed"}, "disconnected").endswith("observed   ⚠ disconnected")


def test_claude_gets_its_own_input_back_with_the_answers():
    tool_input = {"questions": [{"question": "Which database?"}]}
    ok, result = question_result(tool_input, {"allow": True, "answers": {"Which database?": "Postgres"}})
    assert ok and result == {**tool_input, "answers": {"Which database?": "Postgres"}}
    assert question_result(tool_input, {"allow": True}) == (False, "The user didn't answer in Robin")
    assert question_result(tool_input, {"allow": False, "message": "declined"}) == (False, "declined")
    already = {**tool_input, "answers": {}}
    assert question_result(already, {"allow": True, "answers": {"Which database?": "x"}})[0] is False
