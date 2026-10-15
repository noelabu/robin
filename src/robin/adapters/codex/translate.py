"""Codex app-server notifications -> Robin's host event vocabulary (robin.runtime.events).

Pure functions over JSON-RPC notification dicts. Unknown methods degrade to "codex_notification" with the
method name kept, so a protocol change affects only what Robin can show, never the session (NFR-06).
"""

from typing import Any

from robin.findings import schema

PREVIEW = 64 * 1024


def _turn(params: dict[str, Any]) -> dict[str, Any]:
    t = params.get("turn")
    return t if isinstance(t, dict) else {}


def translate(method: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    native = {"native_method": method}
    if method == "thread/started":
        thread = params.get("thread") or {}
        return [{"type": "codex_thread_started", "native_session_id": thread.get("id"), **native}]

    if method == "turn/started":
        return [
            {
                "type": "turn_started",
                "native_turn_id": _turn(params).get("id") or params.get("turnId"),
                **native,
            }
        ]

    if method == "turn/completed":
        turn = _turn(params)
        status = turn.get("status")
        kind = {"interrupted": "turn_interrupted", "failed": "turn_failed"}.get(str(status), "turn_completed")
        return [
            {
                "type": kind,
                "native_turn_id": turn.get("id"),
                "status": status,
                "reason": (turn.get("error") or {}).get("message")
                if isinstance(turn.get("error"), dict)
                else None,
                **native,
            }
        ]

    if method == "item/agentMessage/delta":
        return [
            {
                "type": "message_delta",
                "text": params.get("delta"),
                "native_event_id": params.get("itemId"),
                **native,
            }
        ]

    if method in ("item/started", "item/completed"):
        return _item(method == "item/completed", params.get("item") or {}, native)

    return [{"type": "codex_notification", **native}]


def _item(completed: bool, item: dict[str, Any], native: dict[str, Any]) -> list[dict[str, Any]]:
    kind, item_id = item.get("type"), item.get("id")
    base = {"native_event_id": item_id, "native_item_type": kind, **native}
    if kind == "agentMessage":
        if not completed:
            return []
        out = [
            {
                "type": "message_completed",
                "role": "assistant",
                "text": item.get("text"),
                "phase": item.get("phase"),
                **base,
            }
        ]
        if item.get("phase") in (None, "final_answer"):  # message path: blocks in the final answer
            for i, payload in enumerate(schema.parse_message_blocks(str(item.get("text") or ""))):
                out.append(schema.submission_event("message", payload, f"{item_id}:{i}"))
        return out
    if kind == "userMessage":
        if not completed:
            return []
        text = " ".join(c.get("text", "") for c in item.get("content") or [] if isinstance(c, dict))
        return [{"type": "user_message", "text": text, **base}]
    if kind == "commandExecution":
        if not completed:
            return [
                {
                    "type": "tool_started",
                    "tool_call_id": item_id,
                    "tool": "shell",
                    "input": {"command": item.get("command")},
                    **base,
                }
            ]
        failed = item.get("status") not in (None, "completed") or (item.get("exitCode") not in (None, 0))
        return [
            {
                "type": "tool_error" if failed else "tool_result",
                "tool_call_id": item_id,
                "tool": "shell",
                "exit_code": item.get("exitCode"),
                "output": str(item.get("aggregatedOutput") or "")[:PREVIEW],
                **base,
            }
        ]
    if kind == "fileChange":
        if not completed:
            return []
        paths = [c.get("path") for c in item.get("changes") or [] if isinstance(c, dict)]
        return [{"type": "file_changed", "paths": paths, "status": item.get("status"), **base}]
    if kind == "mcpToolCall":
        tool = f"mcp:{item.get('server')}/{item.get('tool')}"
        if not completed:
            return [
                {
                    "type": "tool_started",
                    "tool_call_id": item_id,
                    "tool": tool,
                    "input": item.get("arguments"),
                    **base,
                }
            ]
        failed = item.get("status") != "completed"
        out = [
            {
                "type": "tool_error" if failed else "tool_result",
                "tool_call_id": item_id,
                "tool": tool,
                "arguments": item.get("arguments"),
                "status": item.get("status"),
                **base,
            }
        ]
        if not failed and item.get("server") == "robin" and item.get("tool") == schema.TOOL_NAME:
            out.append(schema.submission_event("tool", item.get("arguments"), str(item_id)))  # tool path
        return out
    if kind == "reasoning":
        return [{"type": "codex_reasoning", **base}] if completed else []  # content not kept
    return [{"type": "codex_item", **base}] if completed else []
