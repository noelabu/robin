"""Pi RPC events -> Robin's host event vocabulary (robin.runtime.events).

Pure functions over Pi's JSONL records (docs/json.md in the Pi package). Unknown records degrade to
"pi_event" with the native type kept.
"""

from typing import Any

PREVIEW = 64 * 1024


def _text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    return "".join(
        c.get("text", "") for c in content or [] if isinstance(c, dict) and c.get("type") == "text"
    )


def translate(record: dict[str, Any]) -> list[dict[str, Any]]:
    kind = record.get("type")
    native = {"native_type": kind}
    if kind == "agent_start":
        return [{"type": "turn_started", **native}]
    if kind == "agent_settled":
        return [{"type": "turn_interrupted" if record.get("aborted") else "turn_completed", **native}]
    if kind == "message_update":
        ev = record.get("assistantMessageEvent") or {}
        if ev.get("type") == "text_delta":
            return [{"type": "message_delta", "text": ev.get("delta"), **native}]
        return []
    if kind == "message_end":
        message = record.get("message") or {}
        role = message.get("role")
        if role == "assistant":
            return [
                {
                    "type": "message_completed",
                    "role": "assistant",
                    "text": _text(message),
                    "native_event_id": str(message.get("timestamp")) if message.get("timestamp") else None,
                    **native,
                }
            ]
        if role == "user":
            return [{"type": "user_message", "text": _text(message), **native}]
        return []
    if kind == "tool_execution_start":
        return [
            {
                "type": "tool_started",
                "tool_call_id": record.get("toolCallId"),
                "tool": record.get("toolName"),
                "input": record.get("args"),
                "native_event_id": record.get("toolCallId"),
                **native,
            }
        ]
    if kind == "tool_execution_end":
        result = record.get("result") or {}
        text = _text(result) if isinstance(result, dict) else str(result)
        return [
            {
                "type": "tool_error" if record.get("isError") else "tool_result",
                "tool_call_id": record.get("toolCallId"),
                "tool": record.get("toolName"),
                "output": text[:PREVIEW],
                "duration_ms": record.get("durationMs"),
                **native,
            }
        ]
    if kind in ("message_start", "turn_start", "turn_end", "agent_end", "tool_execution_update"):
        return []  # covered by the events above
    return [{"type": "pi_event", **native}]
