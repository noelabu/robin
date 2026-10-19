"""What the chat shows and what the user's input means, without the terminal UI, so it can be tested alone.

History comes from the session's conversation; after that, the stream says what changed: tool steps become
dim lines as they happen, and a new message re-reads the conversation, which has every reply in full.
"""

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

LineKind = Literal["user", "assistant", "tool", "notice", "error"]

TOOL_EVENTS = {"tool.started", "tool.error", "file.change", "session.plan_updated", "session.failed"}
MESSAGE_EVENTS = {"message.user", "message.completed"}
ATTENTION_EVENTS = {"session.turn_completed", "session.stopped", "session.failed"}


@dataclass(frozen=True)
class Line:
    kind: LineKind
    text: str


def history_lines(messages: list[dict[str, Any]]) -> list[Line]:
    out: list[Line] = []
    for m in messages:
        out += message_lines(m)
    return out


def message_lines(m: dict[str, Any]) -> list[Line]:
    if m.get("role") == "tools":
        return [Line("tool", str(item)) for item in m.get("items") or []]
    if m.get("role") in ("user", "assistant"):
        return [Line(m["role"], str(m.get("text") or ""))]
    return []


def _when(text: Any) -> datetime | None:
    try:
        when = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    return when if when.tzinfo else when.replace(tzinfo=UTC)


def after_link(messages: list[dict[str, Any]], link: dict[str, Any] | None) -> list[dict[str, Any]]:
    """A continued session's own messages: the ones before its link were imported from the session it
    continues (written with that session's times), which is shown above it."""
    at = _when((link or {}).get("at"))
    if at is None:
        return messages
    return [m for m in messages if (t := _when(m.get("at"))) is None or t > at]


def before_link(messages: list[dict[str, Any]], link: dict[str, Any] | None) -> list[dict[str, Any]]:
    """What a session had when another continued it; anything it did afterwards wasn't carried over."""
    at = _when((link or {}).get("at"))
    if at is None:
        return messages
    return [m for m in messages if (t := _when(m.get("at"))) is None or t <= at]


def parse_sse(lines: Iterable[str]) -> Iterator[tuple[str, int | None, dict[str, Any]]]:
    """Server-sent events as (kind, id, data). Comment lines are heartbeats."""
    kind, eid = "message", None
    data: list[str] = []
    for raw in lines:
        line = raw.rstrip("\r\n")
        if not line:
            if data:
                try:
                    yield kind, eid, json.loads("\n".join(data))
                except ValueError:
                    pass
            kind, eid, data = "message", None, []
        elif line.startswith(":"):
            continue
        else:
            name, _, value = line.partition(":")
            value = value.removeprefix(" ")
            if name == "event":
                kind = value
            elif name == "id":
                eid = int(value) if value.isdigit() else None
            elif name == "data":
                data.append(value)


def _main_thread(frame: dict[str, Any], session_id: str) -> dict[str, Any] | None:
    """The frame's event, if it belongs to this session's own thread (not a subagent's, not a repeat)."""
    from robin.activity import lane_of

    ev = frame.get("event") or {}
    if ev.get("session_id") != session_id or ev.get("duplicate_of"):
        return None
    if lane_of(str(ev.get("source_id") or "")):
        return None
    return dict(ev)


def frame_line(frame: dict[str, Any], session_id: str) -> Line | None:
    ev = _main_thread(frame, session_id)
    if ev is None or frame.get("noise") or ev.get("event_type") not in TOOL_EVENTS:
        return None
    preview = frame.get("preview")
    if not preview:
        return None
    return Line("error" if ev["event_type"] in ("tool.error", "session.failed") else "tool", str(preview))


def wants_conversation(frame: dict[str, Any], session_id: str) -> bool:
    ev = _main_thread(frame, session_id)
    return ev is not None and ev.get("event_type") in MESSAGE_EVENTS


def wants_attention(frame: dict[str, Any], session_id: str) -> bool:
    ev = (frame.get("event") or {}) if (frame.get("event") or {}).get("session_id") == session_id else {}
    kind = str(ev.get("event_type") or "")
    return kind.startswith(("attention.", "control.")) or kind in ATTENTION_EVENTS


def new_messages(
    seen: list[dict[str, Any]], fetched: list[dict[str, Any]]
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """What a re-read conversation adds to what's shown: the last message again if its text grew (one reply
    reported in parts), then every message after it. Tool groups are left out; the stream showed those."""
    talk = [m for m in fetched if m.get("role") != "tools"]
    shown = [m for m in seen if m.get("role") != "tools"]
    if not shown:
        return None, talk
    last = shown[-1]
    for i in range(len(talk) - 1, -1, -1):
        m = talk[i]
        if m.get("role") == last.get("role") and m.get("at") == last.get("at"):
            grown = m if m.get("text") != last.get("text") else None
            return grown, talk[i + 1 :]
    return None, [m for m in talk if (m.get("at") or "") > (last.get("at") or "")]


def pending(items: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The oldest open approval or question: the one the agent is waiting on."""
    open_ = [
        i
        for i in items
        if i.get("kind") in ("approval", "question") and i.get("state") in ("open", "acknowledged")
    ]
    return min(open_, key=lambda i: i.get("created_at") or "") if open_ else None


def answerable(item: dict[str, Any] | None) -> bool:
    return bool(item and item.get("kind") == "question" and "answer" in (item.get("allowed_responses") or []))


STATE_WORDS = {
    "running": "working",
    "waiting_for_input": "waiting for you",
    "idle": "idle",
    "failed": "failed",
    "stopped": "stopped",
    "unknown": "unknown",
}


def status(session: dict[str, Any], link: str = "live") -> str:
    where = (session.get("execution_cwd") or "").rstrip("/").rsplit("/", 1)[-1] or "no folder"
    if session.get("branch"):
        where += f" @ {session['branch']}"
    parts = [
        session.get("runtime") or "?",
        where,
        STATE_WORDS.get(session.get("runtime_state") or "", "unknown"),
    ]
    if session.get("skip_permissions"):
        parts.append("skip permissions")
    owner = session.get("control_owner")
    parts.append("observed" if session.get("mode") == "observed" else f"control: {owner or 'robin'}")
    return " · ".join(parts) + f"   {'● live' if link == 'live' else '⚠ ' + link}"


def parse_choice(text: str, question: dict[str, Any]) -> str:
    """A number picks an option (several, comma- or space-separated, for a multi-select question); anything
    else is the user's own answer."""
    labels = [o.get("label") or "" for o in question.get("options") or []]
    tokens = text.replace(",", " ").split()
    if tokens and all(t.isdigit() and 1 <= int(t) <= len(labels) for t in tokens):
        if len(tokens) == 1 or question.get("multi"):
            return ", ".join(dict.fromkeys(labels[int(t) - 1] for t in tokens))
    return text.strip()


@dataclass
class ChatState:
    session: dict[str, Any]
    pending: dict[str, Any] | None = None
    answers: dict[str, str] = field(default_factory=dict)  # a question's answers so far, in order

    def question(self) -> dict[str, Any] | None:
        """The question being answered now, if a question is pending."""
        if not answerable(self.pending):
            return None
        qs = (self.pending or {}).get("detail", {}).get("questions") or []
        return qs[len(self.answers)] if len(self.answers) < len(qs) else None


@dataclass(frozen=True)
class Intent:
    kind: Literal[
        "quit",
        "help",
        "interrupt",
        "stop",
        "message",
        "takeover",
        "allow",
        "deny",
        "next_answer",
        "submit_answers",
        "decline",
        "error",
        "nothing",
    ]
    text: str = ""
    answers: dict[str, str] = field(default_factory=dict)


HELP = (
    "Enter sends · Ctrl+J new line · /interrupt ends the running turn · /stop ends the session · "
    "/quit or Ctrl+C leaves (the session keeps running)"
)


def route_input(text: str, st: ChatState) -> Intent:
    t = text.strip()
    if not t:
        return Intent("nothing")
    if t in ("/quit", "/exit"):
        return Intent("quit")
    if t == "/help":
        return Intent("help", HELP)
    if t in ("/interrupt", "/stop"):
        return Intent(t[1:])  # type: ignore[arg-type]
    p = st.pending
    if p and p.get("kind") == "approval":
        if t.lower() in ("y", "yes"):
            return Intent("allow")
        if t.lower() in ("n", "no"):
            return Intent("deny")
        return Intent("error", "Answer y to allow or n to deny")
    if answerable(p):
        if t == "/decline":
            return Intent("decline")
        q = st.question()
        if q is not None:
            answers = {**st.answers, q["question"]: parse_choice(t, q)}
            qs = (p or {}).get("detail", {}).get("questions") or []
            return Intent("submit_answers" if len(answers) == len(qs) else "next_answer", answers=answers)
    s = st.session
    if s.get("mode") == "managed":
        if s.get("control_owner") == "terminal":
            return Intent(
                "error", f"Handed off to a terminal. Take it back with robin reclaim {s['session_id']}"
            )
        return Intent("message", t)
    takeover = (s.get("controls") or {}).get("takeover") or {}
    if takeover.get("enabled"):
        return Intent("takeover", t)
    reason = takeover.get("reason") or "Robin didn't start this session"
    return Intent("error", f"Read-only here: {reason}. Continue it with robin open {s['session_id']}")


def pending_text(st: ChatState) -> str:
    """The panel above the input while the agent waits on the user."""
    p = st.pending
    if not p:
        return ""
    detail = p.get("detail") or {}
    if p.get("kind") == "approval":
        what = detail.get("input")
        if isinstance(what, dict):  # a shell command reads best as itself
            what = what.get("command") or json.dumps(what, ensure_ascii=False)
        return f"Approval needed · {detail.get('tool') or 'a tool'}\n{what or ''}\ny allow · n deny"
    if not answerable(p):
        return f"Claude asks: {p.get('title')}\nAnswer it in the session's terminal."
    qs = detail.get("questions") or []
    q = st.question()
    if q is None:
        return ""
    n = len(st.answers) + 1
    opts = "    ".join(f"{i}. {o.get('label')}" for i, o in enumerate(q.get("options") or [], 1))
    pick = "pick several: 1,3" if q.get("multi") else "pick one"
    done = "".join(f"\nAnswered: {k} → {v}" for k, v in st.answers.items())
    return (
        f"Question {n} of {len(qs)} · Claude asks\n{q['question']}   ({pick})\n{opts}{done}\n"
        "number(s) to pick · any other text is your own answer · /decline"
    )
