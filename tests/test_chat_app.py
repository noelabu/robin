"""The chat's terminal UI, driven with Textual's test pilot against a fake service."""

import asyncio
from typing import Any

import httpx
from rich.markdown import Markdown
from textual.widgets import Static

from robin.chat.api import ChatError
from robin.chat.app import ChatApp, ChatInput

SESSION = {
    "session_id": "s1",
    "runtime": "claude",
    "mode": "managed",
    "control_owner": "robin",
    "runtime_state": "idle",
    "execution_cwd": "/work/robin",
    "branch": "main",
    "skip_permissions": True,
    "controls": {},
}
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


class FakeApi:
    def __init__(self, session: dict[str, Any] | None = None):
        self.s = dict(session or SESSION)
        self.messages: list[dict[str, Any]] = [
            {"role": "user", "at": "1", "text": "Add a health endpoint"},
            {"role": "tools", "at": "2", "items": ["Edit app.py"]},
            {"role": "assistant", "at": "3", "text": "Added it."},
        ]
        self.items: list[dict[str, Any]] = []
        self.calls: list[tuple[Any, ...]] = []
        self.frames: asyncio.Queue[Any] = asyncio.Queue()
        self.streams: list[int] = []
        self.fail_act: ChatError | None = None
        self.others: dict[str, tuple[dict[str, Any], list[dict[str, Any]]]] = {}

    async def session(self, session_id=None):
        return self.others[session_id][0] if session_id else self.s

    async def conversation(self, session_id=None):
        return (list(self.others[session_id][1]), 0) if session_id else (list(self.messages), 40)

    async def attention(self):
        return list(self.items)

    async def act(self, kind, text=None):
        if self.fail_act:
            raise self.fail_act
        self.calls.append(("act", kind, text))
        if text:
            self.messages.append({"role": "user", "at": str(len(self.messages) + 10), "text": text})
        return {}

    async def respond(self, attention_id, decision, answers=None):
        self.calls.append(("respond", attention_id, decision, answers))
        self.items = []
        return {}

    async def stream(self, after):
        self.streams.append(after)
        yield "connected", None, {}
        while True:
            item = await self.frames.get()
            if isinstance(item, Exception):
                raise item
            yield item

    async def close(self):
        self.calls.append(("close",))


def texts(app: ChatApp) -> list[str]:
    """Each line's text; a reply is rendered as Markdown, so its source."""
    out = []
    for w in app.query("#log > Static"):
        c = w.content
        out.append(c.markup if isinstance(c, Markdown) else str(c))
    return out


async def send(pilot, text: str) -> None:
    box = pilot.app.query_one("#input", ChatInput)
    box.text = text
    box.focus()
    await pilot.press("enter")
    await pilot.pause()


async def settle(pilot, check, timeout: float = 3) -> None:
    for _ in range(int(timeout / 0.05)):
        if check():
            return
        await pilot.pause(0.05)
    assert check()


def frame(event_type: str, cursor: int, preview: str = "", session: dict[str, Any] | None = None):
    ev = {"session_id": "s1", "event_type": event_type, "source_id": "supervisor:s1"}
    return ("event", cursor, {"event": ev, "preview": preview, "noise": False, "session": session or SESSION})


async def test_history_then_live_steps_and_replies():
    api = FakeApi()
    app = ChatApp(api, "s1")
    async with app.run_test() as pilot:
        await settle(pilot, lambda: api.streams == [40])  # follows from the conversation's cursor
        shown = texts(app)
        assert "You  Add a health endpoint" in shown and "  Edit app.py" in shown and "Added it." in shown
        await settle(pilot, lambda: "● live" in str(app.query_one("#status", Static).content))
        status = str(app.query_one("#status", Static).content)
        assert "claude · robin @ main · idle · skip permissions · control: robin" in status
        await api.frames.put(frame("tool.started", 41, "Read tests/test_app.py"))
        await settle(pilot, lambda: "  Read tests/test_app.py" in texts(app))
        api.messages.append({"role": "assistant", "at": "50", "text": "Tests pass."})
        await api.frames.put(frame("message.completed", 42))
        await settle(pilot, lambda: "Tests pass." in texts(app))
        assert texts(app).count("Added it.") == 1  # nothing shown twice
        assert app.cursor == 42


async def test_a_continued_session_shows_the_conversation_it_continues():
    api = FakeApi(
        {**SESSION, "runtime": "codex", "continued_from": {"session_id": "s0", "runtime": "claude"}}
    )
    api.others["s0"] = (
        {**SESSION, "session_id": "s0", "continued_from": None},
        [{"role": "user", "at": "0", "text": "Plan the resume feature"}],
    )
    api.messages = [{"role": "user", "at": "1", "text": "Continue from where the previous session left off."}]
    app = ChatApp(api, "s1")
    async with app.run_test() as pilot:
        await settle(pilot, lambda: len(texts(app)) == 3)
        assert texts(app) == [
            "You  Plan the resume feature",
            "── continued from claude in codex ──",
            "You  Continue from where the previous session left off.",
        ]


async def test_a_continued_session_shows_each_imported_message_once():
    # Claude and Pi targets hold the messages imported from their source, with the source's times.
    reasoning = "[external_agent_reasoning]\nCheck the plan\n[/external_agent_reasoning]"
    api = FakeApi(
        {
            **SESSION,
            "runtime": "pi",
            "continued_from": {"session_id": "s0", "runtime": "claude", "at": "2026-10-18T12:00:00+00:00"},
        }
    )
    api.others["s00"] = (
        {**SESSION, "session_id": "s00", "runtime": "codex", "continued_from": None},
        [
            {"role": "user", "at": "2026-10-18T09:00:00+00:00", "text": "Plan the resume feature"},
            {"role": "assistant", "at": "2026-10-18T09:01:00+00:00", "text": "Planned it."},
        ],
    )
    api.others["s0"] = (
        {
            **SESSION,
            "session_id": "s0",
            "continued_from": {"session_id": "s00", "runtime": "codex", "at": "2026-10-18T10:00:00+00:00"},
        },
        [
            {"role": "user", "at": "2026-10-18T09:00:00+00:00", "text": "Plan the resume feature"},
            {"role": "assistant", "at": "2026-10-18T09:01:00+00:00", "text": "Planned it."},
            {"role": "user", "at": "2026-10-18T10:05:00+00:00", "text": "Build it"},
            {"role": "assistant", "at": "2026-10-18T10:30:00+00:00", "text": "Built it."},
        ],
    )
    api.messages = [
        {"role": "user", "at": "2026-10-18T09:00:00+00:00", "text": "Plan the resume feature"},
        {"role": "assistant", "at": "2026-10-18T09:01:00+00:00", "text": reasoning + "\n\nPlanned it."},
        {"role": "user", "at": "2026-10-18T10:05:00+00:00", "text": "Build it"},
        {"role": "assistant", "at": "2026-10-18T10:30:00+00:00", "text": "Built it."},
        {"role": "user", "at": "2026-10-18T12:00:01+00:00", "text": "Now test it"},
    ]
    app = ChatApp(api, "s1")
    async with app.run_test() as pilot:
        await settle(pilot, lambda: api.streams == [40])
        expected = [
            "You  Plan the resume feature",
            "Planned it.",
            "── continued from codex in claude ──",
            "You  Build it",
            "Built it.",
            "── continued from claude in pi ──",
            "You  Now test it",
        ]
        assert texts(app) == expected
        api.messages.append({"role": "assistant", "at": "2026-10-18T12:01:00+00:00", "text": "Tests pass."})
        await api.frames.put(frame("message.completed", 41))
        await settle(pilot, lambda: "Tests pass." in texts(app))
        assert texts(app) == [*expected, "Tests pass."]


async def test_a_continued_session_whose_source_is_gone_keeps_what_it_imported():
    api = FakeApi(
        {
            **SESSION,
            "continued_from": {"session_id": "gone", "runtime": "pi", "at": "2026-10-18T12:00:00+00:00"},
        }
    )
    api.messages = [
        {"role": "user", "at": "2026-10-18T09:00:00+00:00", "text": "Plan the resume feature"},
        {"role": "user", "at": "2026-10-18T12:00:01+00:00", "text": "Now test it"},
    ]

    async def session(session_id=None):
        if session_id:
            raise ChatError("not_found", "no such session")
        return api.s

    api.session = session  # type: ignore[method-assign]
    app = ChatApp(api, "s1")
    async with app.run_test() as pilot:
        await settle(pilot, lambda: len(texts(app)) == 2)
        assert texts(app) == ["You  Plan the resume feature", "You  Now test it"]


async def test_a_continued_session_whose_source_shows_nothing_keeps_what_it_imported():
    api = FakeApi(
        {
            **SESSION,
            "continued_from": {"session_id": "s0", "runtime": "pi", "at": "2026-10-18T10:00:00+00:00"},
        }
    )
    api.others["s0"] = ({**SESSION, "session_id": "s0", "runtime": "pi", "continued_from": None}, [])
    api.messages = [
        {"role": "user", "at": "2026-10-18T09:00:00+00:00", "text": "Plan the resume feature"},
        {"role": "user", "at": "2026-10-18T10:05:00+00:00", "text": "Now test it"},
    ]
    app = ChatApp(api, "s1")
    async with app.run_test() as pilot:
        await settle(pilot, lambda: api.streams == [40])
        assert texts(app) == ["You  Plan the resume feature", "You  Now test it"]


async def test_an_older_source_that_shows_nothing_leaves_the_newer_one_whole():
    api = FakeApi(
        {
            **SESSION,
            "runtime": "pi",
            "continued_from": {"session_id": "s0", "runtime": "claude", "at": "2026-10-18T12:00:00+00:00"},
        }
    )
    api.others["s00"] = ({**SESSION, "session_id": "s00", "runtime": "codex", "continued_from": None}, [])
    api.others["s0"] = (
        {
            **SESSION,
            "session_id": "s0",
            "continued_from": {"session_id": "s00", "runtime": "codex", "at": "2026-10-18T10:00:00+00:00"},
        },
        [
            {"role": "user", "at": "2026-10-18T09:00:00+00:00", "text": "Plan the resume feature"},
            {"role": "user", "at": "2026-10-18T10:05:00+00:00", "text": "Build it"},
        ],
    )
    api.messages = [
        {"role": "user", "at": "2026-10-18T09:00:00+00:00", "text": "Plan the resume feature"},
        {"role": "user", "at": "2026-10-18T10:05:00+00:00", "text": "Build it"},
        {"role": "user", "at": "2026-10-18T12:00:01+00:00", "text": "Now test it"},
    ]
    app = ChatApp(api, "s1")
    async with app.run_test() as pilot:
        await settle(pilot, lambda: api.streams == [40])
        assert texts(app) == [
            "You  Plan the resume feature",
            "You  Build it",
            "── continued from claude in pi ──",
            "You  Now test it",
        ]


async def test_a_source_that_kept_going_shows_only_what_was_continued():
    api = FakeApi(
        {
            **SESSION,
            "runtime": "pi",
            "continued_from": {"session_id": "s0", "runtime": "claude", "at": "2026-10-18T12:00:00+00:00"},
        }
    )
    api.others["s00"] = (
        {**SESSION, "session_id": "s00", "runtime": "codex", "continued_from": None},
        [
            {"role": "user", "at": "2026-10-18T09:00:00+00:00", "text": "Plan the resume feature"},
            {"role": "user", "at": "2026-10-18T11:00:00+00:00", "text": "AFTER FORK in codex"},
        ],
    )
    api.others["s0"] = (
        {
            **SESSION,
            "session_id": "s0",
            "continued_from": {"session_id": "s00", "runtime": "codex", "at": "2026-10-18T10:00:00+00:00"},
        },
        [
            {"role": "user", "at": "2026-10-18T09:00:00+00:00", "text": "Plan the resume feature"},
            {"role": "user", "at": "2026-10-18T10:05:00+00:00", "text": "Build it"},
            {"role": "user", "at": "2026-10-18T13:00:00+00:00", "text": "AFTER FORK in claude"},
        ],
    )
    api.messages = [
        {"role": "user", "at": "2026-10-18T09:00:00+00:00", "text": "Plan the resume feature"},
        {"role": "user", "at": "2026-10-18T10:05:00+00:00", "text": "Build it"},
        {"role": "user", "at": "2026-10-18T12:00:01+00:00", "text": "Now test it"},
    ]
    app = ChatApp(api, "s1")
    async with app.run_test() as pilot:
        await settle(pilot, lambda: api.streams == [40])
        assert texts(app) == [
            "You  Plan the resume feature",
            "── continued from codex in claude ──",
            "You  Build it",
            "── continued from claude in pi ──",
            "You  Now test it",
        ]


async def test_typing_sends_a_message_and_leaving_never_stops_the_session():
    api = FakeApi()
    app = ChatApp(api, "s1")
    async with app.run_test() as pilot:
        await send(pilot, "hello there")
        assert ("act", "message", "hello there") in api.calls
        await settle(pilot, lambda: "You  hello there" in texts(app))
        assert app.query_one("#input", ChatInput).text == ""
        await pilot.press("ctrl+c")
    assert not any(c[:2] == ("act", "stop") for c in api.calls)


async def test_an_approval_is_answered_with_y():
    api = FakeApi()
    api.items = [
        {
            "attention_id": "a2",
            "kind": "approval",
            "state": "open",
            "created_at": "1",
            "allowed_responses": ["allow", "deny"],
            "detail": {"tool": "Bash", "input": {"command": "make test"}},
        }
    ]
    app = ChatApp(api, "s1")
    async with app.run_test() as pilot:
        await settle(pilot, lambda: "make test" in str(app.query_one("#pending", Static).content))
        await pilot.pause()
        box, panel = app.query_one("#input").region, app.query_one("#pending").region
        assert box.height and not box.overlaps(panel)  # the panel never covers what you type
        await send(pilot, "y")
        assert ("respond", "a2", "allow", None) in api.calls
        assert not app.query_one("#pending").has_class("show")


async def test_a_question_is_answered_in_two_steps():
    api = FakeApi()
    app = ChatApp(api, "s1")
    async with app.run_test() as pilot:
        api.items = [QUESTION]
        await api.frames.put(frame("attention.question", 41))
        await settle(pilot, lambda: "Which database?" in str(app.query_one("#pending", Static).content))
        await send(pilot, "2")
        assert "Which features?" in str(app.query_one("#pending", Static).content)
        await send(pilot, "1,2")
        answers = {"Which database?": "Postgres", "Which features?": "Auth, Billing"}
        assert ("respond", "a1", "answer", answers) in api.calls


async def test_an_observed_session_is_taken_over_and_errors_show_inline():
    observed = {**SESSION, "mode": "observed", "controls": {"takeover": {"enabled": True}}}
    api = FakeApi(observed)
    api.fail_act = ChatError("terminal_still_open", "close it in your terminal first")
    app = ChatApp(api, "s1")
    async with app.run_test() as pilot:
        await send(pilot, "carry on")
        await settle(pilot, lambda: any("terminal_still_open" in t for t in texts(app)))
        assert app.query_one("#input", ChatInput).text == "carry on"  # kept to send again
        api.fail_act = None
        await send(pilot, "carry on")
        assert ("act", "takeover", "carry on") in api.calls


async def test_a_dropped_stream_reconnects_from_the_last_cursor():
    api = FakeApi()
    app = ChatApp(api, "s1")
    async with app.run_test() as pilot:
        await api.frames.put(frame("tool.started", 45, "Read a"))
        await settle(pilot, lambda: app.cursor == 45)
        await api.frames.put(httpx.ReadError("gone"))
        await settle(pilot, lambda: "disconnected" in str(app.query_one("#status", Static).content))
        await settle(pilot, lambda: api.streams == [40, 45])
