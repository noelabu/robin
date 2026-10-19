"""The terminal UI: a status bar, the conversation, what the agent is waiting on, and the input box.

Robin keeps control of the session the whole time; leaving the chat never stops it.
"""

import asyncio
from pathlib import Path
from typing import Any

import httpx
from rich.markdown import Markdown
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.message import Message
from textual.widgets import Static, TextArea

from robin.chat import model
from robin.chat.api import ChatApi, ChatError
from robin.chat.model import ChatState, Line

LOG_LIMIT = 1000  # lines kept on screen
EARLIER_LIMIT = 5  # sessions followed back through continued_from
STYLE = {"user": "bold #c9b6ff", "tool": "dim", "notice": "italic #8f86a8", "error": "#ff6b81"}


class ChatInput(TextArea):
    """Enter sends; Ctrl+J (or Shift+Enter, where the terminal reports it) starts a new line."""

    class Submitted(Message):
        def __init__(self, text: str):
            super().__init__()
            self.text = text

    async def _on_key(self, event: events.Key) -> None:
        if event.key == "enter":
            event.prevent_default()
            event.stop()
            self.post_message(self.Submitted(self.text))
            return
        if event.key in ("ctrl+j", "shift+enter"):
            event.prevent_default()
            event.stop()
            self.insert("\n")
            return
        await super()._on_key(event)


class ChatApp(App[None]):
    TITLE = "Robin"
    CSS = """
    Screen { background: #0e0b16; }
    #status { dock: top; height: 1; padding: 0 1; background: #1a1428; color: #c9b6ff; }
    #log { padding: 0 1; }
    #log > Static { margin: 0 0 0 0; }
    #log > .assistant { margin: 0 0 1 0; }
    #log > .user { margin: 1 0 0 0; }
    #bottom { dock: bottom; height: auto; }
    #pending { display: none; height: auto; margin: 0 1; padding: 0 1; border: round #e8b04b; }
    #pending { color: #f3e3c0; }
    #pending.show { display: block; }
    #input { height: auto; max-height: 8; border: round #3a2d57; }
    #input:focus { border: round #b48cff; }
    """
    BINDINGS = [
        Binding("ctrl+c", "quit", "Leave", priority=True),
        Binding("ctrl+d", "quit", "Leave", priority=True, show=False),
    ]

    def __init__(self, api: Any, session_id: str):
        super().__init__()
        self.api = api
        self.session_id = session_id
        self.state = ChatState(session={"session_id": session_id})
        self.seen: list[dict[str, Any]] = []
        self.cursor = 0
        self.link = "connecting"
        self.last_message: Static | None = None
        self.imported_until: dict[str, Any] | None = None
        self._conversation_due = False

    def compose(self) -> ComposeResult:
        yield Static(id="status")
        yield VerticalScroll(id="log")
        with Vertical(id="bottom"):  # docked together: two widgets docked to one edge overlap
            yield Static(id="pending")
            yield ChatInput(id="input", soft_wrap=True, show_line_numbers=False)

    async def on_mount(self) -> None:
        self.query_one("#input", ChatInput).focus()
        try:
            await self.reload()
        except ChatError as exc:
            self.write(Line("error", str(exc)))
        self.run_worker(self.follow(), exclusive=True, group="stream")

    async def on_unmount(self) -> None:
        await self.api.close()

    # -- what's shown ------------------------------------------------------------------------------

    def write(self, line: Line) -> Static:
        log = self.query_one("#log", VerticalScroll)
        if line.kind == "assistant":
            widget = Static(Markdown(line.text or " "), classes="assistant")
        else:
            prefix = {"user": "You  ", "tool": "  ", "notice": "", "error": "✗ "}[line.kind]
            widget = Static(Text(prefix + line.text, style=STYLE[line.kind]), classes=line.kind)
        log.mount(widget)
        extra = len(log.children) - LOG_LIMIT
        for old in list(log.children)[: max(extra, 0)]:
            old.remove()
        log.scroll_end(animate=False)
        return widget

    def write_message(self, m: dict[str, Any]) -> None:
        for line in model.message_lines(m):
            widget = self.write(line)
            if line.kind in ("user", "assistant"):
                self.last_message = widget

    def show_status(self) -> None:
        self.query_one("#status", Static).update(model.status(self.state.session, self.link))
        s = self.state.session
        box = self.query_one("#input", ChatInput)
        if s.get("mode") == "observed":
            box.placeholder = "Message to take over this session… (Robin resumes it with your text)"
        elif s.get("runtime_state") == "running":
            box.placeholder = "Message (queued until the turn ends) · /interrupt to stop it"
        else:
            box.placeholder = "Message… · Enter sends · Ctrl+J new line · /help"

    def show_pending(self) -> None:
        panel = self.query_one("#pending", Static)
        text = model.pending_text(self.state)
        panel.update(text)
        panel.set_class(bool(text), "show")

    # -- reading -----------------------------------------------------------------------------------

    async def earlier(self) -> list[tuple[dict[str, Any], list[dict[str, Any]]]]:
        """The sessions this one was continued from, oldest first, each with its conversation up to the
        link that continued it. One that shows nothing hides the older ones too: the session after it
        keeps its own copy of that history."""
        chain: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
        link = self.state.session.get("continued_from")
        while link and len(chain) < EARLIER_LIMIT:
            try:
                source = await self.api.session(link["session_id"])
                messages, _ = await self.api.conversation(link["session_id"])
            except ChatError:
                break  # an archived or pruned source: show what's left
            chain.insert(0, (source, model.before_link(messages, link)))
            link = source.get("continued_from")
        out: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
        for source, messages in chain:
            if out:
                messages = model.after_link(messages, source.get("continued_from"))
            out = out + [(source, messages)] if messages else []
        return out

    async def reload(self) -> None:
        self.state.session = await self.api.session()
        earlier = await self.earlier()
        self.imported_until = self.state.session.get("continued_from") if earlier else None
        messages, self.cursor = await self.api.conversation()
        messages = model.after_link(messages, self.imported_until)
        await self.query_one("#log", VerticalScroll).remove_children()
        for i, (source, old) in enumerate(earlier):
            for m in old:
                self.write_message(m)
            into = earlier[i + 1][0] if i + 1 < len(earlier) else self.state.session
            self.write(Line("notice", f"── continued from {source['runtime']} in {into['runtime']} ──"))
        self.last_message = None  # only this session's own last message grows
        if not messages and not earlier:
            self.write(Line("notice", "No messages yet."))
        for m in messages:
            self.write_message(m)
        self.seen = messages
        self.show_status()
        await self.refresh_attention()

    async def refresh_conversation(self) -> None:
        self._conversation_due = False
        messages, _ = await self.api.conversation()
        messages = model.after_link(messages, self.imported_until)
        grown, added = model.new_messages(self.seen, messages)
        if grown is not None and self.last_message is not None:
            self.last_message.update(
                Markdown(grown["text"]) if grown["role"] == "assistant" else grown["text"]
            )
        for m in added:
            self.write_message(m)
        self.seen = messages

    def conversation_soon(self) -> None:
        """Replies often arrive in several events; read the conversation once they settle."""
        if not self._conversation_due:
            self._conversation_due = True
            self.set_timer(0.2, lambda: self.run_worker(self.refresh_conversation(), group="read"))

    async def refresh_attention(self, retries: int = 0) -> None:
        p = model.pending(await self.api.attention())
        if (p or {}).get("attention_id") != (self.state.pending or {}).get("attention_id"):
            self.state.pending, self.state.answers = p, {}
            self.show_pending()
        # An event can arrive just before its request is recorded: look again shortly.
        if p is None and retries:
            self.set_timer(0.25 * (4 - retries), lambda: self.run_worker(self.refresh_attention(retries - 1)))

    async def follow(self) -> None:
        delay = 0.5
        while True:
            try:
                async for kind, eid, data in self.api.stream(self.cursor):
                    if self.link != "live":
                        self.link, delay = "live", 0.5
                        self.show_status()
                    if kind == "resync_required":
                        await self.reload()
                        break
                    if kind != "event":
                        continue
                    if eid is not None:
                        self.cursor = eid
                    await self.on_frame(data)
            except (httpx.HTTPError, ChatError):
                pass
            self.link = f"disconnected · retrying in {delay:.0f}s"
            self.show_status()
            await asyncio.sleep(delay)
            delay = min(delay * 2, 5)

    async def on_frame(self, frame: dict[str, Any]) -> None:
        if (frame.get("event") or {}).get("session_id") != self.session_id:
            return
        if frame.get("session"):
            self.state.session = frame["session"]
            self.show_status()
        line = model.frame_line(frame, self.session_id)
        if line:
            self.write(line)
        if model.wants_conversation(frame, self.session_id):
            self.conversation_soon()
        if model.wants_attention(frame, self.session_id):
            kind = frame["event"].get("event_type", "")
            await self.refresh_attention(retries=3 if kind.startswith("attention.") else 0)

    # -- the user's input --------------------------------------------------------------------------

    async def on_chat_input_submitted(self, message: ChatInput.Submitted) -> None:
        box = self.query_one("#input", ChatInput)
        intent = model.route_input(message.text, self.state)
        p = self.state.pending
        try:
            match intent.kind:
                case "nothing":
                    return
                case "quit":
                    self.exit()
                    return
                case "help" | "error":
                    self.write(Line("notice" if intent.kind == "help" else "error", intent.text))
                    return
                case "interrupt" | "stop":
                    await self.api.act(intent.kind)
                    self.write(Line("notice", f"{intent.kind.capitalize()} sent."))
                case "message" | "takeover":
                    await self.api.act(intent.kind, intent.text)
                    await self.refresh_conversation()
                case "allow" | "deny" | "decline":
                    await self.api.respond(p["attention_id"], "allow" if intent.kind == "allow" else "deny")  # type: ignore[index]
                    self.write(
                        Line(
                            "notice",
                            {"allow": "Allowed.", "deny": "Denied.", "decline": "Declined."}[intent.kind],
                        )
                    )
                    self.state.pending, self.state.answers = None, {}
                case "next_answer":
                    self.state.answers = intent.answers
                case "submit_answers":
                    await self.api.respond(p["attention_id"], "answer", intent.answers)  # type: ignore[index]
                    self.write(
                        Line(
                            "notice",
                            "Answered: " + "; ".join(f"{k} → {v}" for k, v in intent.answers.items()),
                        )
                    )
                    self.state.pending, self.state.answers = None, {}
        except ChatError as exc:
            self.write(Line("error", str(exc)))
            return
        box.text = ""
        self.show_pending()


def run_chat(data_dir: Path, session_id: str) -> None:
    ChatApp(ChatApi.for_data_dir(data_dir, session_id), session_id).run()
