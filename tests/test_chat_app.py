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

    async def session(self):
        return self.s

    async def conversation(self):
        return list(self.messages), 40

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
