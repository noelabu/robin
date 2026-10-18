"""Reading a native transcript into a canonical Transcript.

Like the runtime's own reload, a compacted session reads as its latest compaction summary plus the
messages that compaction kept, never the history the summary replaced. Everything that doesn't cross is
counted in `Transcript.dropped`, including content of kinds this reader doesn't know (`unknown_items`), so
a format change shows up in the preview instead of vanishing.
"""

import json
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from robin.adapters.history import HISTORY, parse_time
from robin.continuation.canonical import (
    AgentMessage,
    Block,
    Msg,
    Text,
    Thinking,
    ToolCall,
    ToolResult,
    Transcript,
)

INJECTED = ("<system-reminder>", "<system-notice>")  # Claude Code's own context, not the person's words
CODEX_INJECTED = ("<environment_context>", "<user_instructions>", "# AGENTS.md", "<permissions instructions>")


def records(path: Path) -> Iterator[dict[str, Any]]:
    with open(path, "rb") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except ValueError:
                continue  # a torn last line while the runtime writes, or a corrupt record
            if isinstance(rec, dict):
                yield rec


def flatten(content: Any, dropped: Counter[str]) -> str:
    """Text of a content value that is a string or a list of text/image parts."""
    if isinstance(content, str):
        return content
    parts = []
    for part in content if isinstance(content, list) else []:
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        if kind in ("text", "input_text", "output_text"):
            parts.append(str(part.get("text") or ""))
        elif kind in ("image", "input_image"):
            dropped["images"] += 1
            parts.append("[image]")
        elif kind == "tool_reference":  # Claude: a tool search result naming a tool
            parts.append(f"[tool reference: {part.get('tool_name')}]")
        elif kind == "encrypted_content":  # Codex: content only OpenAI can read
            dropped["encrypted_reasoning"] += 1
        else:
            dropped["unknown_items"] += 1
    return "\n".join(parts)


class Cut:
    """The latest compaction: messages standing in for older history, and where kept history resumes."""

    def __init__(self) -> None:
        self.prefix: list[Msg] | None = None
        self.start = 0

    def apply(self, msgs: list[Msg], dropped: Counter[str]) -> list[Msg]:
        if self.prefix is None:
            return msgs
        kept = msgs[self.start :]
        while kept and kept[0].role == "tool":  # a result whose call was compacted away
            kept.pop(0)
        dropped["before_compaction"] += len(msgs) - len(kept)
        return self.prefix + kept


def summary_msg(text: str, runtime: str, rec: dict[str, Any]) -> Msg:
    return Msg(
        "user",
        [Text(f"[Summary of the earlier conversation, compacted by {runtime}]\n\n{text}")],
        parse_time(rec.get("timestamp")),
    )


# -- Claude Code ------------------------------------------------------------------


def read_claude(path: Path) -> Transcript:
    dropped: Counter[str] = Counter()
    msgs: list[Msg] = []
    starts: dict[str, int] = {}  # record uuid -> index of its first message
    cut = Cut()
    native = cwd = model = None
    last_message_id = None
    for rec in records(path):
        native = native or rec.get("sessionId")
        cwd = cwd or rec.get("cwd")
        if rec.get("subtype") == "compact_boundary":
            meta = rec.get("compactMetadata") or {}
            head = ((meta.get("preservedMessages") or {}).get("uuids") or [None])[0] or (
                meta.get("preservedSegment") or {}
            ).get("headUuid")
            cut.prefix, cut.start = [], starts.get(str(head), len(msgs))
            continue
        message = rec.get("message")
        if rec.get("type") not in ("user", "assistant") or not isinstance(message, dict):
            continue
        if rec.get("isSidechain"):
            dropped["sidechain_records"] += 1
            continue
        if rec.get("isCompactSummary"):
            if cut.prefix is not None:
                cut.prefix = [summary_msg(flatten(message.get("content"), dropped), "claude", rec)]
            continue
        if rec.get("isMeta"):
            continue
        ts = parse_time(rec.get("timestamp"))
        if rec.get("uuid"):
            starts[str(rec["uuid"])] = len(msgs)
        content = message.get("content")
        if rec["type"] == "assistant":
            model = model or message.get("model")
            blocks = claude_assistant_blocks(content, dropped)
            if not blocks:
                continue
            # One API reply is written as several records sharing message.id: merge them back.
            if (
                message.get("id")
                and message["id"] == last_message_id
                and msgs
                and msgs[-1].role == "assistant"
            ):
                msgs[-1].blocks.extend(blocks)
            else:
                msgs.append(Msg("assistant", blocks, ts))
            last_message_id = message.get("id")
            continue
        last_message_id = None
        msgs.extend(claude_user_msgs(content, ts, dropped))
    return Transcript(
        "claude",
        str(native or path.stem),
        cwd,
        model,
        cut.apply(msgs, dropped),
        dropped,
        cut.prefix is not None,
    )


def claude_assistant_blocks(content: Any, dropped: Counter[str]) -> list[Block]:
    if isinstance(content, str):
        return [Text(content)] if content.strip() else []
    blocks: list[Block] = []
    for b in content if isinstance(content, list) else []:
        if not isinstance(b, dict):
            continue
        kind = b.get("type")
        if kind == "text":
            blocks.append(Text(str(b.get("text") or "")))
        elif kind == "thinking":
            if str(b.get("thinking") or "").strip():
                blocks.append(Thinking(str(b["thinking"])))
        elif kind == "redacted_thinking":
            dropped["redacted_thinking"] += 1
        elif kind == "tool_use":
            blocks.append(ToolCall(str(b.get("id") or ""), str(b.get("name") or ""), b.get("input")))
        elif kind == "image":
            dropped["images"] += 1
            blocks.append(Text("[image]"))
        else:
            dropped["unknown_items"] += 1
    return blocks


def claude_user_msgs(content: Any, ts: Any, dropped: Counter[str]) -> list[Msg]:
    """Claude keeps tool results inside user messages: split them into tool messages."""
    out: list[Msg] = []
    texts: list[Any] = []
    for b in content if isinstance(content, list) else []:
        if isinstance(b, dict) and b.get("type") == "tool_result":
            out.append(
                Msg(
                    "tool",
                    [
                        ToolResult(
                            str(b.get("tool_use_id") or ""),
                            flatten(b.get("content"), dropped),
                            bool(b.get("is_error")),
                        )
                    ],
                    ts,
                )
            )
        else:
            texts.append(b)
    text = content if isinstance(content, str) else flatten(texts, dropped)
    if text.strip().startswith(INJECTED):
        dropped["system_reminders"] += 1
    elif text.strip():
        out.insert(0, Msg("user", [Text(text)], ts))
    return out


# -- Codex ------------------------------------------------------------------------


def read_codex(path: Path) -> Transcript:
    dropped: Counter[str] = Counter()
    msgs: list[Msg] = []
    cut = Cut()
    native = cwd = model = None
    for rec in records(path):
        raw = rec.get("payload")
        payload: dict[str, Any] = raw if isinstance(raw, dict) else {}
        kind = rec.get("type")
        if kind == "session_meta":
            native = native or payload.get("id") or payload.get("session_id")
            cwd = cwd or payload.get("cwd")
        elif kind == "turn_context":
            model = model or payload.get("model")
        elif kind == "compacted":
            prefix: list[Msg] = []
            for item in payload.get("replacement_history") or []:
                if isinstance(item, dict):
                    codex_item(item, rec, prefix, Counter())
            if not prefix and payload.get("message"):
                prefix = [summary_msg(str(payload["message"]), "codex", rec)]
            cut.prefix, cut.start = prefix, len(msgs)
        elif kind == "response_item":
            codex_item(payload, rec, msgs, dropped)
    return Transcript(
        "codex",
        str(native or HISTORY["codex"].describe(path).native_id),
        cwd,
        model,
        cut.apply(msgs, dropped),
        dropped,
        cut.prefix is not None,
    )


def codex_output(output: Any, dropped: Counter[str]) -> str:
    if isinstance(output, dict):  # older rollouts: {"content": ..., "success": ...}
        output = output.get("content", "")
    return flatten(output, dropped) if isinstance(output, list) else str(output or "")


def codex_item(p: dict[str, Any], rec: dict[str, Any], msgs: list[Msg], dropped: Counter[str]) -> None:
    ts = parse_time(rec.get("timestamp"))
    kind = p.get("type")
    if kind == "message":
        role = p.get("role")
        text = flatten(p.get("content"), dropped)
        if role == "user":
            if text.strip().startswith(CODEX_INJECTED):
                dropped["system_reminders"] += 1
            elif text.strip():
                msgs.append(Msg("user", [Text(text)], ts))
        elif role == "assistant":
            if text.strip():
                msgs.append(Msg("assistant", [Text(text)], ts))
        else:
            dropped["system_reminders"] += 1  # developer and system messages: the runtime's own instructions
    elif kind == "agent_message":
        text = flatten(p.get("content"), dropped)
        if text.strip():
            msgs.append(
                Msg(
                    "assistant",
                    [AgentMessage(str(p.get("author") or ""), str(p.get("recipient") or ""), text)],
                    ts,
                )
            )
    elif kind == "reasoning":
        text = "\n".join(
            str(s.get("text") or "") for s in p.get("summary") or [] if isinstance(s, dict)
        ).strip()
        if text:
            msgs.append(Msg("assistant", [Thinking(text)], ts))
        elif p.get("encrypted_content"):
            dropped["encrypted_reasoning"] += 1
    elif kind == "function_call":
        raw = str(p.get("arguments") or "")
        try:
            args: Any = json.loads(raw)
        except ValueError:
            args = raw
        msgs.append(
            Msg("assistant", [ToolCall(str(p.get("call_id") or ""), str(p.get("name") or ""), args)], ts)
        )
    elif kind == "custom_tool_call":
        msgs.append(
            Msg(
                "assistant",
                [ToolCall(str(p.get("call_id") or ""), str(p.get("name") or ""), str(p.get("input") or ""))],
                ts,
            )
        )
    elif kind in ("function_call_output", "custom_tool_call_output"):
        msgs.append(
            Msg("tool", [ToolResult(str(p.get("call_id") or ""), codex_output(p.get("output"), dropped))], ts)
        )
    else:
        dropped["unknown_items"] += 1


# -- Pi ---------------------------------------------------------------------------


def read_pi(path: Path) -> Transcript:
    dropped: Counter[str] = Counter()
    msgs: list[Msg] = []
    starts: dict[str, int] = {}  # entry id -> index of its first message
    cut = Cut()
    native = cwd = model = None
    for rec in records(path):
        kind = rec.get("type")
        if kind == "session":
            native, cwd = rec.get("id"), rec.get("cwd")
        elif kind == "model_change":
            model = rec.get("modelId") or rec.get("model") or model
        elif kind == "compaction":
            cut.prefix = [summary_msg(str(rec.get("summary") or ""), "pi", rec)]
            cut.start = starts.get(str(rec.get("firstKeptEntryId")), len(msgs))
        elif kind == "message" and isinstance(rec.get("message"), dict):
            if rec.get("id"):
                starts[str(rec["id"])] = len(msgs)
            pi_message(rec["message"], parse_time(rec.get("timestamp")), msgs, dropped)
            model = model or rec["message"].get("model")
    return Transcript(
        "pi",
        str(native or path.stem.split("_", 1)[-1]),
        cwd,
        model,
        cut.apply(msgs, dropped),
        dropped,
        cut.prefix is not None,
    )


def pi_message(m: dict[str, Any], ts: Any, msgs: list[Msg], dropped: Counter[str]) -> None:
    role = m.get("role")
    if role == "user":
        text = flatten(m.get("content"), dropped)
        if text.strip():
            msgs.append(Msg("user", [Text(text)], ts))
    elif role == "assistant":
        blocks: list[Block] = []
        content = m.get("content")
        for b in [{"type": "text", "text": content}] if isinstance(content, str) else content or []:
            if not isinstance(b, dict):
                continue
            kind = b.get("type")
            if kind == "text":
                blocks.append(Text(str(b.get("text") or "")))
            elif kind == "thinking":
                if str(b.get("thinking") or "").strip():
                    blocks.append(Thinking(str(b["thinking"])))
            elif kind == "toolCall":
                blocks.append(ToolCall(str(b.get("id") or ""), str(b.get("name") or ""), b.get("arguments")))
            elif kind == "image":
                dropped["images"] += 1
                blocks.append(Text("[image]"))
            else:
                dropped["unknown_items"] += 1
        if blocks:
            msgs.append(Msg("assistant", blocks, ts))
    elif role == "toolResult":
        result = ToolResult(
            str(m.get("toolCallId") or ""), flatten(m.get("content"), dropped), bool(m.get("isError"))
        )
        msgs.append(Msg("tool", [result], ts))
    elif role == "system":
        dropped["system_reminders"] += 1  # Pi's own system prompt, rebuilt by the target
    else:
        dropped["unknown_items"] += 1


READERS = {"claude": read_claude, "codex": read_codex, "pi": read_pi}


def read(runtime: str, path: Path) -> Transcript:
    return READERS[runtime](path)
