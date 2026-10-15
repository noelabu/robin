"""The tools' side of Robin's agent API, for runtime hosts and Robin's MCP server (TR-066).

Reads ROBIN_SERVICE_URL and ROBIN_AGENT_TOKEN from the environment Robin launched the session with. Standard
library only. Every call returns text for the agent: the JSON result, or Robin's refusal in plain words.
"""

import json
import os
import urllib.error
import urllib.request
from typing import Any

TIMEOUT_S = 30

TOOLS: dict[str, dict[str, Any]] = {
    "task_get": {
        "description": "Get this session's Robin task: title, objective, instructions and expected outputs.",
        "schema": {"type": "object", "properties": {}},
    },
    "finding_list": {
        "description": "List the findings you can read: your own, and those the user shared with you.",
        "schema": {"type": "object", "properties": {}},
    },
    "finding_read": {
        "description": "Read one finding you can see (finding_list shows which).",
        "schema": {
            "type": "object",
            "properties": {"finding_id": {"type": "string"}},
            "required": ["finding_id"],
        },
    },
    "session_search": {
        "description": "Search the transcript ranges the user shared with you. Returns cursors and snippets.",
        "schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    },
    "session_read_excerpt": {
        "description": "Read messages from a transcript range the user shared with you (event cursors).",
        "schema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "after": {"type": "integer"},
                "through": {"type": "integer"},
            },
            "required": ["session_id", "after", "through"],
        },
    },
    "artifact_publish": {
        "description": "Save a written result (for example a synthesis) to the Robin task, listing the "
        "finding IDs it is based on as inputs. New claims need their own evidence.",
        "schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "text": {"type": "string"},
                "inputs": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["title", "text"],
        },
    },
}

_ROUTES = {
    "task_get": ("GET", "/agent/task"),
    "finding_list": ("GET", "/agent/findings"),
    "finding_read": ("GET", "/agent/findings/{finding_id}"),
    "session_search": ("POST", "/agent/search"),
    "session_read_excerpt": ("POST", "/agent/excerpt"),
    "artifact_publish": ("POST", "/agent/artifacts"),
}


def available() -> bool:
    return bool(os.environ.get("ROBIN_AGENT_TOKEN") and os.environ.get("ROBIN_SERVICE_URL"))


def call(tool: str, args: dict[str, Any]) -> str:
    if tool not in _ROUTES:
        return f"Robin has no tool {tool}."
    if not available():
        return "Robin's tools aren't available in this session."
    method, path = _ROUTES[tool]
    if "{finding_id}" in path:
        fid = str(args.get("finding_id", ""))
        if not fid or "/" in fid:
            return "finding_id is required."
        path = path.replace("{finding_id}", fid)
    body = None if method == "GET" else json.dumps(args).encode()
    req = urllib.request.Request(
        f"{os.environ['ROBIN_SERVICE_URL']}/api/v1{path}",
        data=body,
        method=method,
        headers={
            "authorization": f"Bearer {os.environ['ROBIN_AGENT_TOKEN']}",
            "content-type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:  # noqa: S310 - loopback only
            return resp.read().decode()
    except urllib.error.HTTPError as exc:
        try:
            message = json.loads(exc.read())["error"]["message"]
        except Exception:
            message = f"HTTP {exc.code}"
        return f"Robin refused: {message}"
    except OSError as exc:
        return f"Robin isn't reachable right now ({type(exc).__name__}); try again later."
