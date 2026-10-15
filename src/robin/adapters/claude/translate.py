"""Claude Agent SDK messages -> Robin's host event vocabulary (robin.runtime.events).

Pure functions, no SDK import: messages are matched by class name and read by attribute, so a new SDK
message type degrades to a "claude_message" event instead of failing (FR-011, NFR-06).
"""

import dataclasses
from typing import Any

MAX_INLINE_TEXT = 64 * 1024


def _plain(obj: Any) -> Any:
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: _plain(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, dict):
        return {k: _plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_plain(v) for v in obj]
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return repr(obj)


def translate(message: Any, *, interrupted: bool = False) -> list[dict[str, Any]]:
    """One SDK message -> zero or more normalized host events. Every event keeps the native kind."""
    kind = type(message).__name__
    native = {"native_kind": kind}

    if kind == "SystemMessage":
        data = getattr(message, "data", {}) or {}
        if getattr(message, "subtype", None) == "init":
            return [
                {
                    "type": "session_started",
                    "native_session_id": data.get("session_id"),
                    "model": data.get("model"),
                    "permission_mode": data.get("permissionMode"),
                    "api_key_source": data.get("apiKeySource"),
                    "cwd": data.get("cwd"),
                    "claude_code_version": data.get("claude_code_version"),
                    "tools": data.get("tools"),
                    "mcp_servers": data.get("mcp_servers"),
                    **native,
                }
            ]
        return [{"type": "claude_system", "subtype": getattr(message, "subtype", None), **native}]

    if kind == "AssistantMessage":
        events: list[dict[str, Any]] = []
        mid = getattr(message, "message_id", None) or getattr(message, "uuid", None)
        for i, block in enumerate(getattr(message, "content", []) or []):
            bkind = type(block).__name__
            if bkind == "TextBlock":
                events.append(
                    {
                        "type": "message_completed",
                        "role": "assistant",
                        "text": block.text,
                        "native_event_id": f"{mid}:{i}" if mid else None,
                        **native,
                    }
                )
            elif bkind == "ToolUseBlock":
                events.append(
                    {
                        "type": "tool_started",
                        "tool_call_id": block.id,
                        "tool": block.name,
                        "input": _plain(block.input),
                        "native_event_id": block.id,
                        **native,
                    }
                )
            elif bkind == "ThinkingBlock":
                events.append({"type": "claude_thinking", **native})  # content not kept
        return events

    if kind == "UserMessage":
        content = getattr(message, "content", None)
        if isinstance(content, str):
            return [{"type": "user_message", "text": content, **native}]
        events = []
        for block in content or []:
            if type(block).__name__ == "ToolResultBlock":
                events.append(
                    {
                        "type": "tool_error" if block.is_error else "tool_result",
                        "tool_call_id": block.tool_use_id,
                        "output": _plain(block.content),
                        **native,
                    }
                )
        return events

    if kind == "ResultMessage":
        detail = {
            "native_session_id": getattr(message, "session_id", None),
            "subtype": getattr(message, "subtype", None),
            "terminal_reason": getattr(message, "terminal_reason", None),
            "duration_ms": getattr(message, "duration_ms", None),
            "num_turns": getattr(message, "num_turns", None),
            "cost_estimate_usd": getattr(message, "total_cost_usd", None),
            "result": getattr(message, "result", None),
            **native,
        }
        if interrupted:
            return [{"type": "turn_interrupted", **detail}]
        if getattr(message, "is_error", False):
            return [{"type": "turn_failed", **detail}]
        return [{"type": "turn_completed", **detail}]

    if kind == "RateLimitEvent":
        return [{"type": "rate_limit", "detail": _plain(message), **native}]

    return [{"type": "claude_message", **native}]
