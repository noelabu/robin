"""The canonical transcript every reader produces and every writer consumes.

Blocks stay structured so the preview can count them; writers flatten tool calls, results and reasoning
into tagged text (`turns`), because a target runtime has none of the source's tools and no way to verify
another vendor's reasoning signatures.
"""

import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

Role = Literal["user", "assistant", "tool"]


@dataclass(frozen=True)
class Text:
    text: str


@dataclass(frozen=True)
class Thinking:
    text: str


@dataclass(frozen=True)
class ToolCall:
    call_id: str
    name: str
    args: Any  # decoded JSON when the source had JSON, else the raw string (Codex custom tools)


@dataclass(frozen=True)
class ToolResult:
    call_id: str
    content: str
    is_error: bool = False


@dataclass(frozen=True)
class AgentMessage:
    """Traffic between agents (Codex multi-agent), kept apart from the assistant's own replies."""

    author: str
    recipient: str
    text: str


Block = Text | Thinking | ToolCall | ToolResult | AgentMessage


@dataclass
class Msg:
    role: Role
    blocks: list[Block]
    ts: datetime | None = None


@dataclass
class Transcript:
    runtime: str
    native_id: str
    cwd: str | None
    model: str | None
    msgs: list[Msg]
    dropped: Counter[str] = field(default_factory=Counter)  # what didn't cross, by kind
    compacted: bool = False  # the source's own compaction summary stands in for older history

    def counts(self) -> dict[str, int]:
        c: Counter[str] = Counter()
        for m in self.msgs:
            if m.role != "tool":
                c[m.role] += 1
            for b in m.blocks:
                if isinstance(b, ToolCall):
                    c["tool_calls"] += 1
                elif isinstance(b, ToolResult):
                    c["tool_results"] += 1
                elif isinstance(b, Thinking):
                    c["thinking"] += 1
        c["total"] = len(self.msgs)
        return {k: c[k] for k in ("total", "user", "assistant", "tool_calls", "tool_results", "thinking")}


def tool_call_text(b: ToolCall) -> str:
    args = b.args if isinstance(b.args, str) else json.dumps(b.args, ensure_ascii=False)
    return f"[external_agent_tool_call: {b.name}]\ninput: {args}"


def tool_result_text(b: ToolResult) -> str:
    body = f"error: {b.content}" if b.is_error else b.content
    return f"[external_agent_tool_result]\n{body}\n[/external_agent_tool_result]"


def block_text(b: Block) -> str:
    match b:
        case Text():
            return b.text
        case Thinking():
            return f"[external_agent_reasoning]\n{b.text}\n[/external_agent_reasoning]"
        case ToolCall():
            return tool_call_text(b)
        case ToolResult():
            return tool_result_text(b)
        case AgentMessage():
            head = f"[external_agent_message from {b.author} to {b.recipient}]"
            return f"{head}\n{b.text}\n[/external_agent_message]"


def turns(msgs: list[Msg], source: str) -> list[tuple[str, str, datetime | None]]:
    """(role, text, time) per block, ready to write: tool calls and results go under the assistant, as in
    Codex's importer, so only the person's own words are user turns. The list starts with a user turn and
    ends with an assistant turn, so the target's next prompt follows a reply."""
    out: list[tuple[str, str, datetime | None]] = []
    for m in msgs:
        role = "user" if m.role == "user" else "assistant"
        for b in m.blocks:
            text = block_text(b)
            if text.strip():
                out.append((role, text, m.ts))
    if not out or out[0][0] != "user":
        out.insert(0, ("user", f"[Continued from an earlier {source} session]", out[0][2] if out else None))
    if out[-1][0] == "user":
        out.append(("assistant", "[The earlier session ended before replying to this message.]", out[-1][2]))
    return out


def alternating(items: list[tuple[str, str, datetime | None]]) -> list[tuple[str, str, datetime | None]]:
    """Consecutive turns of one role joined, for runtimes whose history must alternate."""
    out: list[tuple[str, str, datetime | None]] = []
    for role, text, ts in items:
        if out and out[-1][0] == role:
            out[-1] = (role, out[-1][1] + "\n\n" + text, out[-1][2])
        else:
            out.append((role, text, ts))
    return out
